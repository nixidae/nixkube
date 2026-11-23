"""pynixd's build-pod mode on a live node. nixidae/nixidae#1560.

The manifest turns the mode on. This asks what that has to mean, and what
turning it off means:

  - the PriorityClass is there, below every ordinary pod
  - each node holds the seccomp profile under the kubelet's directory, the
    content pynixd ships
  - a pod that names the profile starts: the runtime loads it from nixkube's
    subdirectory
  - with the mode off, the node DaemonSet rolls and each node removes the
    directory, and the same pod no longer starts (the negative control)
  - with the mode on again, the profile is back
  - a CSI volume that asks for a farm (`presentation: farm`) arrives as one
    read-only bind per closure path, which the build pods take their inputs
    from; the same volume without the attribute has no binds (the negative
    control); a `hostUsers: false` pod's farm is reported, not asserted, as
    the runtime's idmap of each bind is unmeasured; the node keeps no farm
    mount after the pods go
  - central builds a derivation that asks for it in a pynixd build pod, as
    uid 1000: the pod's `/nix` is a farm of one path central made for the
    build, its pynixd reads the farm as its lower store, and it copies none
    of its input in (`../build-pod-store.nix`)

Through kr8s, from the host, at `<out>/access/kubeconfig`, which the cluster
phase's `bring_up` writes.
"""

import json
import re

import anyio
import kr8s.asyncio
from kr8s.asyncio.objects import DaemonSet, Pod
from nixkube_uml import NAMESPACE, NODE_CONTAINER, PriorityClass, Settings, cluster, logs as pod_logs, raw
from vivarium_k8s import until
from vivarium_runner import Machine, MachineError, Machines

SECCOMP_DIR = "/var/lib/kubelet/seccomp"
ROLL_TIMEOUT = 5 * 60
POD_TIMEOUT = 5 * 60
# The reasons a container that never starts waits with.
NOT_STARTED = frozenset({"CreateContainerError", "CreateContainerConfigError", "RunContainerError", "StartError"})


async def _profile_pod(api: kr8s.asyncio.Api, cp: Machine, settings: Settings) -> tuple[Pod, str]:
    """The probe's CSI container alone, running hello under the build pods' profile, until it ends."""
    job = json.loads(await anyio.Path(settings["probeRo"]).read_text())
    spec = job["spec"]["template"]["spec"]
    spec["restartPolicy"] = "Never"
    spec["containers"] = [c for c in spec["containers"] if c["name"] == "csi"]
    spec["containers"][0]["securityContext"] = {
        "seccompProfile": {"type": "Localhost", "localhostProfile": settings["buildPodSeccompProfile"]}
    }
    pod = Pod(
        {"metadata": {"generateName": "buildpods-seccomp-", "namespace": NAMESPACE}, "spec": spec},
        api=api,
    )
    await pod.create()

    async def ended() -> tuple[bool, str]:
        await pod.refresh()
        statuses = raw(pod).get("status", {}).get("containerStatuses", [])
        states = [s.get("state", {}) for s in statuses]
        done = bool(states) and all(
            "terminated" in state or state.get("waiting", {}).get("reason") in NOT_STARTED for state in states
        )
        return done, json.dumps(states)

    await until(f"pod {pod.name}", ended, POD_TIMEOUT, cp)
    said = await pod_logs(pod, "csi") if _ran(pod) else ""
    await pod.delete()
    return pod, said


FARM_MOUNT = "/mnt/csi"
FARM_POINT = re.compile(r"/volumes/[^/]+/nix/store(/|$)")
WROTE = "nixkube-farm-wrote"


async def _farm_pod(api: kr8s.asyncio.Api, cp: Machine, settings: Settings, *, farm: bool, host_users: bool) -> str:
    """The probe's CSI container alone, on `cp`, saying what its volume holds: its log, or why it never ran."""
    job = json.loads(await anyio.Path(settings["probeRo"]).read_text())
    spec = job["spec"]["template"]["spec"]
    spec["restartPolicy"] = "Never"
    spec["nodeSelector"] = {"kubernetes.io/hostname": cp.name}
    if not host_users:
        spec["hostUsers"] = False
    (container,) = (c for c in spec["containers"] if c["name"] == "csi")
    spec["containers"] = [container]
    if farm:
        (volume,) = spec["volumes"]
        volume["csi"]["volumeAttributes"]["presentation"] = "farm"
    store = f"{FARM_MOUNT}/store" if farm else f"{FARM_MOUNT}/nix/store"
    hello = settings["workloadStorePath"].removeprefix("/nix/store/")
    bin_ = f"{settings['busybox']}/bin"
    container["command"] = [
        f"{bin_}/sh",
        "-c",
        f"{store}/{hello}/bin/hello; echo binds=$({bin_}/grep -c ' {store}/' /proc/self/mountinfo);"
        f" {bin_}/touch {store}/w 2>/dev/null && echo {WROTE}-store;"
        f" {bin_}/touch {store}/{hello}/w 2>/dev/null && echo {WROTE}-bind;"
        f" {bin_}/touch {FARM_MOUNT}/w 2>/dev/null && echo top-writable; true",
    ]
    pod = Pod({"metadata": {"generateName": "buildpods-farm-", "namespace": NAMESPACE}, "spec": spec}, api=api)
    await pod.create()

    async def ended() -> tuple[bool, str]:
        await pod.refresh()
        states = [s.get("state", {}) for s in raw(pod).get("status", {}).get("containerStatuses", [])]
        waiting = [s.get("waiting", {}).get("reason") for s in states]
        done = bool(states) and all("terminated" in s or w in NOT_STARTED for s, w in zip(states, waiting, strict=True))
        return done, json.dumps(raw(pod).get("status", {}))

    await until(f"pod {pod.name}", ended, POD_TIMEOUT, cp)
    said = await pod_logs(pod, "csi") if _ran(pod) else f"never ran: {_waiting(pod)}"
    await pod.delete()
    return said


async def _check_farm(api: kr8s.asyncio.Api, cp: Machine, settings: Settings) -> None:
    # The volume holds every store path the pod names: hello, and the busybox its command runs.
    roots = f"{settings['workloadStorePath']} {settings['busybox']}"
    closure = len(set((await cp.succeed(f"nix-store --query --requisites {roots}")).stdout.split()))
    said = await _farm_pod(api, cp, settings, farm=True, host_users=True)
    if "Hello, world!" not in said or f"binds={closure}" not in said or WROTE in said:
        raise MachineError(f"[cp] a farm volume is not {closure} read-only binds:\n{said}")
    print(f"[nixkube] a farm volume holds {closure} read-only binds, and runs hello", flush=True)
    # mount(2) ignores MS_RDONLY when it makes a bind, so the publish's top mount reaches kubelet writable, for every
    # bind volume; a volumeMount with readOnly closes it. Reported, as it is not the farm's.
    print(f"[nixkube] the farm target's top is writable without readOnly on the mount: {'top-writable' in said}")

    said = await _farm_pod(api, cp, settings, farm=False, host_users=True)
    if "Hello, world!" not in said or "binds=0" not in said:
        raise MachineError(f"[cp] the same volume without the attribute is not bind-free:\n{said}")
    print("[nixkube] without presentation: farm the volume has no binds (the control)", flush=True)

    said = await _farm_pod(api, cp, settings, farm=True, host_users=False)
    print(f"[nixkube] a hostUsers: false pod's farm: {' '.join(said.split())[:300]}", flush=True)

    async def detached() -> tuple[bool, str]:
        # The mount point, field 5: a composefs overlay names `<volume>/erofs::/nix/store` in its options.
        table = (await cp.succeed(f"crictl exec {NODE_CONTAINER} cat /proc/self/mountinfo")).stdout
        left = [line.split()[4] for line in table.splitlines() if FARM_POINT.search(line.split()[4])]
        return not left, f"farm mounts in the node container: {left[:5]}"

    await until("the node detaches every farm", detached, POD_TIMEOUT, cp)
    print("[nixkube] the node keeps no farm mount after the pods go", flush=True)


BUILT = "output says: built-from-the-farm"
CENTRAL_NIX = ["env", "NIX_REMOTE=unix:///run/pynixd/pynixd.sock"]
FARM_FEATURE = "nixkube-podfarm"
FARM_ROOT = re.compile(r"/nix/store/[0-9a-z]{32}-pynixd-build-inputs")


def _field(log: str, event: str, name: str) -> int:
    """The integer *name* on the line of *event*, in either of structlog's renderings."""
    line = next((line for line in log.splitlines() if event in line), None)
    found = re.search(rf"\b{name}\W+(\d+)", line or "")
    if found is None:
        raise MachineError(f"[cp] no {event} line with {name} in:\n{log}")
    return int(found.group(1))


async def _central(central: Pod, *command: str) -> str:
    done = await central.exec([*CENTRAL_NIX, *command], container="pynixd", check=False)
    out, err = done.stdout.decode(), done.stderr.decode()
    if done.returncode != 0:
        raise MachineError(f"[cp] {' '.join(command)} in pynixd-0 exited {done.returncode}:\n{out}\n{err}")
    return out + err


async def _check_central_farm_build(api: kr8s.asyncio.Api, settings: Settings) -> None:
    """Central builds in a pod whose `/nix` is a farm of one path central made for the build (`../build-pod-store.nix`).

    The build's input is busybox. The pod copies no byte of it from central, and its lower store holds exactly
    the closure of the path central made.
    """
    central = await Pod.get("pynixd-0", namespace=NAMESPACE, api=api)
    busybox = settings["busybox"]
    await _central(central, "nix-store", "--realise", busybox)
    expr = (
        f'derivation {{ name = "nixkube-central-farm"; system = builtins.currentSystem;'
        f' builder = "${{builtins.storePath "{busybox}"}}/bin/sh";'
        f' args = [ "-c" "{busybox}/bin/echo {BUILT} > $out" ];'
        f' requiredSystemFeatures = [ "{FARM_FEATURE}" ]; }}'
    )
    said = await _central(central, "nix-build", "--no-out-link", "--expr", expr)
    print(f"[nixkube] central's farm build pod said:\n{said}", flush=True)
    paths, moved = _field(said, "build_one_inputs", "paths"), _field(said, "build_one_inputs", "bytes")
    if paths < 1 or moved != 0:
        raise MachineError(f"[cp] the farm build pod copied {moved} bytes of {paths} inputs from central:\n{said}")
    roots = set(FARM_ROOT.findall(await pod_logs(central, "pynixd")))
    if len(roots) != 1:
        raise MachineError(f"[cp] central's log names {len(roots)} farm roots, not one: {sorted(roots)}")
    (root,) = roots
    closure = len((await _central(central, "nix-store", "--query", "--requisites", root)).split())
    loaded = _field(said, "lower_store_loaded", "paths")
    if loaded != closure:
        raise MachineError(f"[cp] the pod's lower store holds {loaded} paths, the closure of {root} {closure}")
    print(f"[nixkube] central's build pod built from a farm of {root}: {closure} paths, 0 bytes copied", flush=True)


def _ran(pod: Pod) -> bool:
    return any("terminated" in s.get("state", {}) for s in raw(pod)["status"].get("containerStatuses", []))


def _waiting(pod: Pod) -> list[str]:
    return [s.get("state", {}).get("waiting", {}).get("reason", "") for s in raw(pod)["status"]["containerStatuses"]]


async def _holds(node: Machine, settings: Settings) -> None:
    path = f"{SECCOMP_DIR}/{settings['buildPodSeccompProfile']}"
    await node.succeed(f"cmp {path} {settings['buildPodSeccomp']}")
    print(f"[nixkube] {node.name} holds {path}", flush=True)


async def _set_mode(api: kr8s.asyncio.Api, cp: Machine, settings: Settings, *, enabled: bool) -> None:
    """Set `BUILD_PODS_ENABLED` on the node DaemonSet, and wait until every node runs the new pod."""
    daemonset = await DaemonSet.get(settings["nodeDaemonSet"], namespace=NAMESPACE, api=api)
    containers = raw(daemonset)["spec"]["template"]["spec"]["containers"]
    index = next(i for i, c in enumerate(containers) if c["name"] == "nix-node")
    env = next(i for i, e in enumerate(containers[index]["env"]) if e["name"] == "BUILD_PODS_ENABLED")
    path = f"/spec/template/spec/containers/{index}/env/{env}"
    # A JSON patch with a test: a merge patch would replace the whole container list.
    await daemonset.patch(
        [
            {"op": "test", "path": f"{path}/name", "value": "BUILD_PODS_ENABLED"},
            {"op": "replace", "path": f"{path}/value", "value": "true" if enabled else "false"},
        ],
        type="json",
    )

    async def rolled() -> tuple[bool, str]:
        await daemonset.refresh()
        generation = raw(daemonset)["metadata"]["generation"]
        status = raw(daemonset).get("status", {})
        want = status.get("desiredNumberScheduled", 0)
        done = (
            status.get("observedGeneration") == generation
            and want > 0
            and status.get("updatedNumberScheduled") == want
            and status.get("numberReady") == want
            and status.get("numberAvailable") == want
        )
        return done, json.dumps(status)

    await until(f"the node DaemonSet with BUILD_PODS_ENABLED={enabled}", rolled, ROLL_TIMEOUT, cp)


async def test(vms: Machines) -> None:
    cp, settings = vms.cp, vms.settings
    nodes = [vms.cp, vms.worker]
    api = await cluster(vms)

    for name, value, policy in (("pynixd-build", -2, "Never"), ("pynixd-build-nested", -1, "PreemptLowerPriority")):
        priority = raw(await PriorityClass.get(name, api=api))
        if priority.get("value") != value or priority.get("preemptionPolicy") != policy:
            raise MachineError(f"[cp] the PriorityClass {name} is not {value} and {policy}: {priority}")
        print(f"[nixkube] the PriorityClass {name} is {value}, {policy}", flush=True)

    for node in nodes:
        await _holds(node, settings)
    pod, logs = await _profile_pod(api, cp, settings)
    if "Hello, world!" not in logs:
        raise MachineError(
            f"[cp] a pod with the build pods' seccomp profile did not run hello: {_waiting(pod)}\n{logs}"
        )
    print("[nixkube] a pod with the build pods' seccomp profile runs", flush=True)

    await _set_mode(api, cp, settings, enabled=False)
    for node in nodes:

        async def gone(node: Machine = node) -> tuple[bool, str]:
            done = await node.execute(f"test -e {SECCOMP_DIR}/nixkube")
            return done.returncode != 0, f"{SECCOMP_DIR}/nixkube still on {node.name}"

        await until(f"{node.name} removes the profile", gone, ROLL_TIMEOUT, cp)
    print("[nixkube] with the mode off, every node removed the profile", flush=True)

    pod, logs = await _profile_pod(api, cp, settings)
    if _ran(pod):
        raise MachineError(f"[cp] a pod with a removed seccomp profile still ran:\n{logs}")
    print(f"[nixkube] without the profile the pod does not start: {_waiting(pod)}", flush=True)

    await _set_mode(api, cp, settings, enabled=True)
    for node in nodes:
        await _holds(node, settings)

    await _check_farm(api, cp, settings)
    await _check_central_farm_build(api, settings)
