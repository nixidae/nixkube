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
  - a writable farm (`storeVolume`) puts the pod's output on its emptyDir:
    kubelet counts the output there and no input, and the emptyDir's path
    holds no mount
  - central builds a derivation that asks for it in a pynixd build pod, as
    uid 1000: the pod's `/nix` is a writable farm of one path central made for
    the build, its pynixd uses it as its own store, and it copies none of its
    input in (`../build-pod-store.nix`); the build pod names central's pod as
    its owner, from the downward API
  - with every node labelled as one that cannot sandbox, central's build
    goes to the unsandboxed template: no sandbox, no job key beside the
    build, and a process the build leaves does not hold the build open
  - with every node labelled so, and `nixkube/build-runner` as the NRI
    plugin set it, central's build goes to the runner's template: its runner
    container is the builder, in Nix's sandbox as the NRI plugin makes it

Through kr8s, from the host, at `<out>/access/kubeconfig`, which the cluster
phase's `bring_up` writes.
"""

import json
import re
from typing import Any

import anyio
import kr8s.asyncio
from kr8s.asyncio.objects import DaemonSet, Node, Pod
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
# The tmpfs farm's store, `<volume>/nix/store`, and the writable farm's store disk, `<volume>/nix`.
FARM_POINT = re.compile(r"/nix-csi/volumes/[^/]+/nix(/store)?$")
WROTE = "nixkube-farm-wrote"
OUTPUT_BYTES = 4 << 20
STATS_TIMEOUT = 3 * 60


async def _check_store_disk(api: kr8s.asyncio.Api, cp: Machine, settings: Settings) -> None:
    """A writable farm (`storeVolume`): kubelet counts the pod's output on its emptyDir, and no input.

    kubelet's walk crosses no other st_dev, and an input bind keeps the node store's, so an input bind at the
    emptyDir's own path would count. Its volume stats refresh once a minute (`--volume-stats-agg-period`)."""
    job = json.loads(await anyio.Path(settings["probeRo"]).read_text())
    spec = job["spec"]["template"]["spec"]
    spec["restartPolicy"] = "Never"
    spec["nodeSelector"] = {"kubernetes.io/hostname": cp.name}
    (container,) = (c for c in spec["containers"] if c["name"] == "csi")
    spec["containers"] = [container]
    container["securityContext"] = {"runAsUser": 1000, "runAsGroup": 1000}
    (volume,) = spec["volumes"]
    volume["csi"]["readOnly"] = False
    volume["csi"]["volumeAttributes"] |= {"presentation": "farm", "storeVolume": "disk"}
    spec["volumes"].append({"name": "disk", "emptyDir": {}})
    container["volumeMounts"].append({"name": "disk", "mountPath": "/disk", "readOnly": True})
    hello = settings["workloadStorePath"].removeprefix("/nix/store/")
    bin_ = f"{settings['busybox']}/bin"
    container["command"] = [
        f"{bin_}/sh",
        "-c",
        f"{FARM_MOUNT}/store/{hello}/bin/hello;"
        f" {bin_}/dd if=/dev/zero of={FARM_MOUNT}/store/output bs=1M count={OUTPUT_BYTES >> 20} && echo {WROTE};"
        f" {bin_}/touch {FARM_MOUNT}/store/{hello}/w 2>/dev/null && echo {WROTE}-bind; exec {bin_}/sleep 600",
    ]
    pod = Pod({"metadata": {"generateName": "buildpods-disk-", "namespace": NAMESPACE}, "spec": spec}, api=api)
    await pod.create()

    async def wrote() -> tuple[bool, str]:
        await pod.refresh()
        said = await pod_logs(pod, "csi") if raw(pod).get("status", {}).get("phase") == "Running" else ""
        return WROTE in said, said or json.dumps(raw(pod).get("status", {}))

    said = await until(f"pod {pod.name} writes its output", wrote, POD_TIMEOUT, cp)
    if "Hello, world!" not in said or f"{WROTE}-bind" in said:
        raise MachineError(f"[cp] a writable farm did not run hello, or an input took a write:\n{said}")

    async def counted() -> tuple[bool, int]:
        async with api.call_api("GET", url=f"nodes/{cp.name}/proxy/stats/summary") as response:
            # kubelet's stats summary, `pkg/kubelet/apis/stats/v1alpha1`: only the fields read here.
            pods: list[dict[str, Any]] = response.json().get("pods", [])
        volumes: list[dict[str, Any]] = next((p.get("volume", []) for p in pods if p["podRef"]["name"] == pod.name), [])
        used: int = next((v.get("usedBytes", 0) for v in volumes if v["name"] == "disk"), 0)
        return used >= OUTPUT_BYTES, used

    used = await until("kubelet counts the output on the emptyDir", counted, STATS_TIMEOUT, cp)
    uid = raw(pod)["metadata"]["uid"]
    disk = f"/var/lib/kubelet/pods/{uid}/volumes/kubernetes.io~empty-dir/disk"
    leaked = int((await cp.succeed(f"grep -c ' {disk}/' /proc/self/mountinfo || true")).stdout.strip() or 0)
    roots = f"{settings['workloadStorePath']} {settings['busybox']}"
    closure = int((await cp.succeed(f"du -scb $(nix-store --query --requisites {roots}) | tail -1")).stdout.split()[0])
    await pod.delete()
    # Nix's database holds an 8 MiB `reserved` file (measured, tests/nixos/csi-farm-disk.nix).
    if leaked or used >= OUTPUT_BYTES + (16 << 20):
        raise MachineError(f"[cp] the emptyDir counts {used} bytes and holds {leaked} mounts; the closure is {closure}")
    print(
        f"[nixkube] kubelet counts {used} bytes on a writable farm's emptyDir for a {OUTPUT_BYTES}-byte output;"
        f" the closure is {closure} bytes, and the emptyDir's path holds no mount",
        flush=True,
    )


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

    await _check_store_disk(api, cp, settings)

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
BUILD_PODS = "pynixd.nixidae.org/build=true"
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


async def _owners(api: kr8s.asyncio.Api, owners: dict[str, list[str]]) -> None:
    """The UIDs each build pod names as its owners: the downward API gives central its own."""
    async for _event, pod in api.watch("pods", namespace=NAMESPACE, label_selector=BUILD_PODS):
        owners[pod.name] = [ref["uid"] for ref in raw(pod)["metadata"].get("ownerReferences", [])]


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
    owners: dict[str, list[str]] = {}
    async with anyio.create_task_group() as watching:
        watching.start_soon(_owners, api, owners)
        said = await _central(central, "nix-build", "--no-out-link", "--expr", expr)
        watching.cancel_scope.cancel()
    print(f"[nixkube] central's farm build pod said:\n{said}", flush=True)
    uid = str(raw(central)["metadata"]["uid"])
    if not owners or any(found != [uid] for found in owners.values()):
        raise MachineError(f"[cp] the build pods name the owners {owners}, not pynixd-0's UID {uid}")
    print(f"[nixkube] pynixd-0 owns each build pod: {sorted(owners)}", flush=True)
    paths, moved = _field(said, "build_one_inputs", "paths"), _field(said, "build_one_inputs", "bytes")
    if paths < 1 or moved != 0:
        raise MachineError(f"[cp] the farm build pod copied {moved} bytes of {paths} inputs from central:\n{said}")
    roots = set(FARM_ROOT.findall(await pod_logs(central, "pynixd")))
    if len(roots) != 1:
        raise MachineError(f"[cp] central's log names {len(roots)} farm roots, not one: {sorted(roots)}")
    (root,) = roots
    closure = len((await _central(central, "nix-store", "--query", "--requisites", root)).split())
    if "lower_store_loaded" in said:
        raise MachineError(f"[cp] the build pod read a lower store, not the farm as its own store:\n{said}")
    print(f"[nixkube] central's build pod built in a farm of {root}: {closure} paths, 0 bytes copied", flush=True)


USERNS = "nixkube/userns"
BUILD_RUNNER = "nixkube/build-runner"
UNSANDBOXED_TIMEOUT = 3 * 60


async def _label_nodes(api: kr8s.asyncio.Api, labels: dict[str, str]) -> None:
    async for node in api.get("nodes"):
        if not isinstance(node, Node):
            raise MachineError(f"[cp] kr8s listed a node as {type(node).__name__}")
        await node.patch({"metadata": {"labels": labels}})


async def _label_userns(api: kr8s.asyncio.Api, value: str) -> None:
    await _label_nodes(api, {USERNS: value})


async def _selectors(api: kr8s.asyncio.Api, seen: dict[str, str | None], before: set[str]) -> None:
    """The `nixkube/userns` each new build pod selects; *before* are the pods an earlier build left."""
    async for _event, pod in api.watch("pods", namespace=NAMESPACE, label_selector=BUILD_PODS):
        if pod.name not in before:
            seen[pod.name] = raw(pod)["spec"].get("nodeSelector", {}).get(USERNS)


async def _check_unsandboxed_build(api: kr8s.asyncio.Api, settings: Settings) -> None:
    """With every node labelled as one that cannot sandbox, central takes the unsandboxed template.

    The build runs without Nix's sandbox, so its hostname is the pod's, not `localhost`. It runs in the sandbox
    container, which has no job key. It leaves a process that holds its terminal: the sandbox container kills
    it, or the build waits for the pod's deadline. The probe's label goes back after.
    """
    central = await Pod.get("pynixd-0", namespace=NAMESPACE, api=api)
    busybox = settings["busybox"]
    script = (
        f"{busybox}/bin/sleep 600 & echo host=$({busybox}/bin/hostname)"
        f" job=$(test -e /run/pynixd-job && echo yes || echo no) > $out"
    )
    expr = (
        f'derivation {{ name = "nixkube-unsandboxed"; system = builtins.currentSystem;'
        f' builder = "${{builtins.storePath "{busybox}"}}/bin/sh"; args = [ "-c" "{script}" ];'
        f' requiredSystemFeatures = [ "{FARM_FEATURE}" ]; }}'
    )
    seen: dict[str, str | None] = {}
    before = {
        pod.name
        async for pod in api.get("pods", namespace=NAMESPACE, label_selector=BUILD_PODS)
        if isinstance(pod, Pod)
    }
    # nixkube's NRI plugin labels its node: nothing in this test has written the label yet.
    found = {
        node.name: raw(node)["metadata"].get("labels", {}).get(BUILD_RUNNER)
        async for node in api.get("nodes")
        if isinstance(node, Node)
    }
    if set(found.values()) != {"true"}:
        raise MachineError(f"[cp] the NRI plugin did not label every node {BUILD_RUNNER}=true: {found}")
    # Not a node for the runner's template either, which central would take first.
    await _label_nodes(api, {USERNS: "false", BUILD_RUNNER: "false"})
    try:
        async with anyio.create_task_group() as watching:
            watching.start_soon(_selectors, api, seen, before)
            with anyio.fail_after(UNSANDBOXED_TIMEOUT):
                said = await _central(central, "nix-build", "--no-out-link", "--expr", expr)
            watching.cancel_scope.cancel()
    finally:
        await _label_nodes(api, {USERNS: "true", BUILD_RUNNER: "true"})
    if not seen or set(seen.values()) != {"false"}:
        raise MachineError(f"[cp] the build pods select {USERNS} {seen}, not only the unsandboxed template's false")
    out = next(line for line in reversed(said.splitlines()) if line.startswith("/nix/store/"))
    built = (await _central(central, "cat", out)).strip()
    host, job = (field.partition("=")[2] for field in built.split())
    if host == "localhost" or host not in seen or job != "no":
        raise MachineError(f"[cp] the unsandboxed build in {sorted(seen)} wrote {built!r}")
    print(f"[nixkube] the unsandboxed template built in {host}, beside no job key, and its leftover died", flush=True)


RUNNER_POD = "nixkube.nixidae.org/build-runner"
SANDBOX_ANNOTATION = "nixkube.nixidae.org/build-sandbox"
# What a build in Nix's sandbox sees, as the runner's builder writes it to `$out`.
RUNNER_EXPECTED = {
    "host": "localhost",
    "id": "1000:100",
    "groups": "100",
    "netdev": "3",
    "sh": "yes",
    "job": "no",
    "token": "no",
    "nixbld": "1",
    "cores": "1",
}


async def _runner_templates(api: kr8s.asyncio.Api, seen: dict[str, bool], before: set[str]) -> None:
    """Whether each new build pod is a runner build pod; *before* are the pods an earlier build left."""
    async for _event, pod in api.watch("pods", namespace=NAMESPACE, label_selector=BUILD_PODS):
        if pod.name not in before:
            seen[pod.name] = raw(pod)["metadata"].get("labels", {}).get(RUNNER_POD) == "true"


async def _check_runner_build(api: kr8s.asyncio.Api, settings: Settings) -> None:
    """With every node labelled as one where a user namespace fails, central takes the runner's template.

    The runner container is the builder, and nixkube's NRI plugin makes it Nix's sandbox: the host name
    `localhost`, loopback alone, uid 1000 and gid 100, Nix's `/etc/passwd`, the build shell at `/bin/sh`, no
    job key and no service account token. What the builder writes to stdout and stderr reaches the client in
    the order it wrote it.
    """
    central = await Pod.get("pynixd-0", namespace=NAMESPACE, api=api)
    busybox = settings["busybox"]
    b = f"{busybox}/bin"
    script = (
        f"echo to-stdout; echo to-stderr >&2; echo again-stdout;"
        f" echo host=$({b}/hostname) id=$({b}/id -u):$({b}/id -g) groups=$({b}/id -G | {b}/tr ' ' ,)"
        f" netdev=$({b}/wc -l < /proc/net/dev) sh=$(test -x /bin/sh && echo yes || echo no)"
        f" job=$(test -e /run/pynixd-job && echo yes || echo no)"
        f" token=$(test -e /var/run/secrets/kubernetes.io && echo yes || echo no)"
        f" nixbld=$({b}/grep -c nixbld /etc/passwd) cores=$NIX_BUILD_CORES > $out"
    )

    def expr(name: str, feature: str) -> str:
        return (
            f'derivation {{ name = "{name}"; system = builtins.currentSystem;'
            f' builder = "${{builtins.storePath "{busybox}"}}/bin/sh"; args = [ "-c" "{script}" ];'
            f" requiredSystemFeatures = [ {feature} ]; }}"
        )

    seen: dict[str, bool] = {}
    before = {
        pod.name
        async for pod in api.get("pods", namespace=NAMESPACE, label_selector=BUILD_PODS)
        if isinstance(pod, Pod)
    }
    await _label_userns(api, "false")
    try:
        async with anyio.create_task_group() as watching:
            watching.start_soon(_runner_templates, api, seen, before)
            with anyio.fail_after(UNSANDBOXED_TIMEOUT):
                said = await _central(
                    central, "nix-build", "--no-out-link", "--expr", expr("nixkube-runner", f'"{FARM_FEATURE}"')
                )
            watching.cancel_scope.cancel()
    finally:
        await _label_userns(api, "true")
    if not seen or not all(seen.values()):
        raise MachineError(f"[cp] the build pods {seen} are not all runner build pods")
    out = next(line for line in reversed(said.splitlines()) if line.startswith("/nix/store/"))
    built = dict(field.partition("=")[::2] for field in (await _central(central, "cat", out)).split())
    wrong = {name: built.get(name) for name, value in RUNNER_EXPECTED.items() if built.get(name) != value}
    if wrong:
        raise MachineError(f"[cp] the runner's build in {sorted(seen)} saw {built}, not {RUNNER_EXPECTED}: {wrong}")
    print(f"[nixkube] the runner built in Nix's sandbox: {built}", flush=True)
    # The runner's terminal keeps the order of stdout and stderr; two pipes did not (measured).
    lines = [line for line in said.splitlines(keepends=True) if "to-std" in line or "again-std" in line]
    if lines != ["to-stdout\n", "to-stderr\n", "again-stdout\n"]:
        raise MachineError(f"[cp] the client got the runner's output as {lines!r}:\n{said}")
    print(f"[nixkube] the client got the runner's output in order: {lines!r}", flush=True)
    # The pod's own store sends no probe build: the runner runs the job's alone.
    if "probe_crashed" in said:
        raise MachineError(f"[cp] the runner build pod's store probed through the runner:\n{said}")


async def _check_runner_gate(api: kr8s.asyncio.Api, cp: Machine) -> None:
    """A runner whose controller wrote none of its files does not start: the NRI plugin refuses to create it.

    The kubelet starts the runner even when the controller's postStart hook fails, and makes a directory for
    each subPath file that is missing. `_check_runner_build` is the control: there the files are there.
    """
    central = raw(await Pod.get("pynixd-0", namespace=NAMESPACE, api=api))
    image = central["spec"]["containers"][0]["image"]
    etc = [
        {"name": "build", "mountPath": f"/etc/{name}", "subPath": f"runner/etc/{name}"}
        for name in ("passwd", "group", "hosts", "nsswitch.conf")
    ]
    pod = Pod(
        {
            "metadata": {
                "generateName": "buildpods-runner-gate-",
                "namespace": NAMESPACE,
                "annotations": {SANDBOX_ANNOTATION: "runner"},
            },
            "spec": {
                "restartPolicy": "Never",
                "containers": [
                    {
                        "name": "runner",
                        "image": image,
                        "imagePullPolicy": "Never",
                        "command": ["/never-runs"],
                        "volumeMounts": etc,
                    }
                ],
                "volumes": [{"name": "build", "emptyDir": {}}],
            },
        },
        api=api,
    )
    await pod.create()

    async def ended() -> tuple[bool, str]:
        await pod.refresh()
        statuses = raw(pod).get("status", {}).get("containerStatuses", [])
        reasons = {s.get("state", {}).get("waiting", {}).get("reason") for s in statuses}
        return bool(reasons & NOT_STARTED) or _ran(pod), json.dumps(statuses)

    try:
        await until("the runner is created or refused", ended, POD_TIMEOUT, cp)
        statuses = raw(pod)["status"].get("containerStatuses", [])
        said = " ".join(s.get("state", {}).get("waiting", {}).get("message", "") for s in statuses)
        if _ran(pod) or "not the files" not in said:
            raise MachineError(f"[cp] a runner without its controller's files was not refused: {statuses}")
    finally:
        await pod.delete()
    print(f"[nixkube] a runner without its controller's files does not start: {said}", flush=True)


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
    await _check_unsandboxed_build(api, settings)
    await _check_runner_build(api, settings)
    await _check_runner_gate(api, cp)
