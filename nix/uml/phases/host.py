"""A NixOSRollout switches two NixOS hosts through the controller (#359).

`hostNext` is each guest's own configuration plus one file, so the switch
changes that file and nothing the cluster runs on. With `maxUnavailable: 1`
the controller switches one node at a time, in name order: `cp`, then
`worker`.

`hostBroken` also stops cp's kubelet: the switch Job cannot report, nothing
can confirm or disarm, and only the host's armed timer brings it back. The
controller must then halt the rollout before it starts on `worker`, mark cp
Reverted and cordon it.
"""

import base64
import hashlib
import json

from nixkube_uml import READY_TIMEOUT
from vivarium_runner import Machine, MachineError, Machines
from vivarium_runner.cluster import get_json, kubectl, until

NAMESPACE = "nixkube"
ROLLOUT = "host"
REVERT_UNIT = "nixkube-revert"
MARKER = "/etc/nixkube-host-generation"
# Less than `nixosHost.revertDelay` in ../manifest.nix.
CONFIRM_TIMEOUT = 8
IN_FLIGHT = {"Switching", "Confirming", "Disarming"}


async def running(vm: Machine) -> str:
    return (await vm.succeed("readlink /run/current-system")).strip()


async def profile(vm: Machine) -> str:
    return (
        await vm.succeed("readlink --canonicalize /nix/var/nix/profiles/system")
    ).strip()


async def armed(vm: Machine) -> bool:
    rc, _ = await vm.execute(f"systemctl is-active --quiet {REVERT_UNIT}.timer")
    return rc == 0


async def apply(cp: Machine, obj: dict) -> None:
    encoded = base64.b64encode(json.dumps(obj).encode()).decode()
    await cp.succeed(f"echo {encoded} | base64 --decode | kubectl apply --filename -")


async def roll_to(cp: Machine, systems: dict[str, str]) -> tuple[dict, list[str]]:
    """Point the rollout at `systems` and wait until it settles.

    Returns the status and the order in which the nodes left Pending. Fails
    as soon as two nodes are in flight at once.
    """
    await apply(
        cp,
        {
            "apiVersion": "nixkube.nixidae.org/v1alpha1",
            "kind": "NixOSRollout",
            "metadata": {"name": ROLLOUT},
            "spec": {
                "nodeSelector": {
                    "matchExpressions": [
                        {
                            "key": "kubernetes.io/hostname",
                            "operator": "In",
                            "values": sorted(systems),
                        }
                    ]
                },
                "systems": systems,
                "maxUnavailable": 1,
                "confirmTimeout": CONFIRM_TIMEOUT,
            },
        },
    )
    generation = (await get_json(cp, f"get nixosrollout {ROLLOUT}"))["metadata"][
        "generation"
    ]
    started: list[str] = []

    async def settled() -> tuple[bool, str]:
        status = (await get_json(cp, f"get nixosrollout {ROLLOUT}")).get("status", {})
        if status.get("observedGeneration") != generation:
            return False, json.dumps(status)
        nodes = status.get("nodes", {})
        flying = sorted(n for n, e in nodes.items() if e.get("phase") in IN_FLIGHT)
        if len(flying) > 1:
            raise MachineError(f"[cp] {flying} are in flight at once: {status}")
        for node in flying:
            if node not in started:
                started.append(node)
        done = status.get("phase") == "Halted" or (
            status.get("phase") == "Complete"
            and all(nodes.get(n, {}).get("target") == t for n, t in systems.items())
        )
        return done, json.dumps(status)

    await until(f"rollout to {systems}", settled, READY_TIMEOUT, cp)
    return (await get_json(cp, f"get nixosrollout {ROLLOUT}"))["status"], started


async def switch_to(vms: dict[str, Machine], systems: dict[str, str]) -> None:
    status, started = await roll_to(vms["cp"], systems)
    if status["phase"] != "Complete":
        raise MachineError(f"[cp] the rollout to {systems} ended {status}")
    for name, toplevel in systems.items():
        vm = vms[name]
        if "leaseRenewTime" in status["nodes"][name]:
            raise MachineError(
                f"[{name}] a Done node keeps its confirm state: {status}"
            )
        if await running(vm) != toplevel or await profile(vm) != toplevel:
            raise MachineError(
                f"[{name}] the rollout is Complete and the host is not on {toplevel}"
            )
        if await armed(vm):
            raise MachineError(f"[{name}] the revert is still armed after the rollout")
    # A node the poll never caught in flight switched between two polls.
    if [n for n in sorted(systems) if n in started] != started:
        raise MachineError(f"[cp] the nodes switched in the order {started}")


async def test(vms: Machines) -> None:
    cp, settings = vms.cp, vms.settings
    hosts = {"cp": vms.cp, "worker": vms.worker}
    await kubectl(
        cp,
        f"rollout status --namespace {NAMESPACE} deployment/nixkube-nixos-rollout --timeout={READY_TIMEOUT}s",
    )
    first = {name: await running(vm) for name, vm in hosts.items()}
    after: dict[str, str] = settings["hostNext"]
    for name in hosts:
        if first[name] == after[name]:
            raise MachineError(f"[{name}] hostNext is the system the guest booted")

    await switch_to(hosts, after)
    for name, vm in hosts.items():
        marker = (await vm.succeed(f"cat {MARKER}")).strip()
        if marker != "next":
            raise MachineError(f"[{name}] {MARKER} says {marker!r} after the switch")
    print(f"[nixkube] the rollout switched both hosts: {after}", flush=True)

    await switch_to(hosts, first)
    for name, vm in hosts.items():
        rc, _ = await vm.execute(f"test -e {MARKER}")
        if rc == 0:
            raise MachineError(f"[{name}] {MARKER} survived the switch back")
    print(f"[nixkube] the rollout switched both hosts back: {first}", flush=True)

    await broken_halts(hosts, first, settings["hostBroken"], after["worker"])
    await kubectl(cp, "uncordon cp")
    await kubectl(cp, f"delete nixosrollout {ROLLOUT}")


async def broken_halts(
    hosts: dict[str, Machine], first: dict[str, str], broken: str, worker_next: str
) -> None:
    """cp's switch fails, so the rollout halts and never starts on worker."""
    cp, worker = hosts["cp"], hosts["worker"]
    # The host's timer fires `nixosHost.revertDelay` (12 s) after the switch.
    status, started = await roll_to(cp, {"cp": broken, "worker": worker_next})
    node = status["nodes"]["cp"]
    if status["phase"] != "Halted" or node.get("phase") != "Reverted":
        raise MachineError(f"[cp] the rollout to {broken} ended {status}")
    if node.get("failedSystem") != broken:
        raise MachineError(f"[cp] failedSystem is {node.get('failedSystem')}")
    if await running(cp) != first["cp"] or await profile(cp) != first["cp"]:
        raise MachineError(f"[cp] the host is not back on {first['cp']}")
    if await armed(cp):
        raise MachineError("[cp] the revert is still armed after the revert")
    spec = (await get_json(cp, "get node cp"))["spec"]
    if not spec.get("unschedulable"):
        raise MachineError("[cp] the reverted node is not cordoned")

    if "worker" in started or status["nodes"]["worker"].get("phase") != "Pending":
        raise MachineError(f"[worker] the halted rollout went on to worker: {status}")
    if await running(worker) != first["worker"]:
        raise MachineError("[worker] the host left its system under a halted rollout")

    jobs = await get_json(
        cp,
        f"get jobs --namespace {NAMESPACE} --selector nixkube.nixidae.org/action=switch",
    )
    # A succeeded switch Job means the timer reverted, not the Job's own
    # failure branch.
    switch = [
        j
        for j in jobs["items"]
        if any(
            e.get("value") == broken
            for e in j["spec"]["template"]["spec"]["containers"][0]["env"]
        )
    ]
    if not switch or not switch[0].get("status", {}).get("succeeded"):
        raise MachineError(
            f"[cp] the switch to {broken} did not succeed, so the timer went untested"
        )
    # `job_name` in src/nixos_rollout.py: one name per rollout, node, target
    # and generation.
    key = f"{ROLLOUT}\0worker\0{worker_next}\0{status['observedGeneration']}"
    late = f"nixos-switch-{hashlib.sha256(key.encode()).hexdigest()[:16]}"
    if any(j["metadata"]["name"] == late for j in jobs["items"]):
        raise MachineError(f"[worker] the halted rollout made switch Job {late}")
    print(
        f"[nixkube] the timer reverted cp from {broken}; the rollout halted before worker",
        flush=True,
    )
