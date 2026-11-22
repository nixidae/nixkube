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

Through kr8s, from the host, at `<out>/access/kubeconfig`, which the cluster
phase's `bring_up` writes.
"""

import json

import anyio
import kr8s.asyncio
from kr8s.asyncio.objects import DaemonSet, Pod
from nixkube_uml import NAMESPACE, PriorityClass, Settings, cluster, logs as pod_logs, raw
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
