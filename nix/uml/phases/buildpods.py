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
  - a pynixd build pod with a farm as its `/nix` builds from it, as uid
    1000: its pynixd reads the farm as its lower store
    (`../farm_build_pod.py`)

Through kr8s, from the host, at `<out>/access/kubeconfig`, which the cluster
phase's `bring_up` writes.
"""

import json
import re
from typing import Any

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


FARM_NIX_CONFIG = """experimental-features = nix-command
build-users-group =
substituters =
require-drop-supplementary-groups = false
"""
BUILT = "output says: built-from-the-farm"


async def _check_farm_build_pod(api: kr8s.asyncio.Api, cp: Machine, settings: Settings) -> None:
    """A pynixd build pod over a farm volume builds from it: no central, so nothing reaches it from elsewhere.

    The daemon container runs pynixd with the farm as its lower store (`farm_build_pod.py`); the sandbox
    container runs the build, under the build pods' seccomp profile. Both as uid 1000, with no capability.
    """
    root = settings["farmBuildPod"]
    python = f"{root}/venv/lib/venv/bin/python"
    job = json.loads(await anyio.Path(settings["probeRo"]).read_text())
    probe_spec = job["spec"]["template"]["spec"]
    (volume,) = probe_spec["volumes"]
    attributes = volume["csi"]["volumeAttributes"]
    (system,) = attributes
    volume["name"] = "nix"
    volume["csi"]["volumeAttributes"] = {system: root, "presentation": "farm"}
    (probe,) = (c for c in probe_spec["containers"] if c["name"] == "csi")
    unprivileged = {"allowPrivilegeEscalation": False, "capabilities": {"drop": ["ALL"]}}
    mounts = [{"name": "nix", "mountPath": "/nix", "readOnly": True}, {"name": "build", "mountPath": "/build"}]
    base = {"image": probe["image"], "imagePullPolicy": probe.get("imagePullPolicy", "Never"), "volumeMounts": mounts}
    spec = {
        "restartPolicy": "Never",
        "nodeSelector": {"kubernetes.io/hostname": cp.name},
        "securityContext": {
            "runAsUser": 1000,
            "runAsGroup": 1000,
            "runAsNonRoot": True,
            "seccompProfile": {"type": "RuntimeDefault"},
        },
        "containers": [
            {
                **base,
                "name": "pynixd",
                "command": [python, "-I", f"{root}/run.py", root],
                "env": [{"name": "NIX_CONFIG", "value": FARM_NIX_CONFIG}],
                "securityContext": unprivileged,
            },
            {
                **base,
                "name": "sandbox",
                "command": [python, "-I", "-m", "pynixd_store.builder.sibling", "run", "/build/sibling"],
                "securityContext": {
                    **unprivileged,
                    "seccompProfile": {"type": "Localhost", "localhostProfile": settings["buildPodSeccompProfile"]},
                },
            },
        ],
        "volumes": [volume, {"name": "build", "emptyDir": {}}],
    }
    pod = Pod({"metadata": {"generateName": "buildpods-farm-build-", "namespace": NAMESPACE}, "spec": spec}, api=api)
    await pod.create()

    async def ended() -> tuple[bool, str]:
        # The sandbox container serves for ever: the daemon container's end is the pod's answer.
        await pod.refresh()
        statuses = raw(pod).get("status", {}).get("containerStatuses", [])
        daemon: dict[str, Any] = next((s.get("state", {}) for s in statuses if s["name"] == "pynixd"), {})
        return "terminated" in daemon or daemon.get("waiting", {}).get("reason") in NOT_STARTED, json.dumps(statuses)

    await until(f"pod {pod.name}", ended, POD_TIMEOUT, cp)
    said = await pod_logs(pod, "pynixd")
    await pod.delete()
    print(f"[nixkube] the farm build pod said:\n{said}", flush=True)
    if BUILT not in said or "lower_store_loaded" not in said:
        raise MachineError(f"[cp] a pynixd build pod over a farm did not build from it:\n{said}")
    print("[nixkube] a pynixd build pod builds from its farm volume, as uid 1000", flush=True)


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

    priority = raw(await PriorityClass.get("pynixd-build", api=api))
    if priority.get("value") != -1 or priority.get("preemptionPolicy") != "Never":
        raise MachineError(f"[cp] the build pods' PriorityClass is not -1 and Never: {priority}")
    print("[nixkube] the PriorityClass pynixd-build is -1, Never", flush=True)

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
    await _check_farm_build_pod(api, cp, settings)
