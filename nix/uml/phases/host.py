"""A NixOSRollout switches the NixOS host through the controller (#359).

`hostNext` is this guest's own configuration plus one file, so the switch
changes that file and nothing the cluster runs on. `hostBroken` also stops
the kubelet: the switch Job cannot report, nothing can confirm or disarm, and
only the host's armed timer brings it back. The controller must then halt
the rollout, mark the node Reverted and cordon it.
"""

import base64
import json

from nixkube_uml import READY_TIMEOUT
from vivarium_runner import Machine, MachineError, Machines
from vivarium_runner.cluster import get_json, kubectl, until

NAMESPACE = "nixkube"
ROLLOUT = "host"
REVERT_UNIT = "nixkube-revert"
MARKER = "/etc/nixkube-host-generation"
# Less than `nixosHost.revertDelay` in ../manifest.nix.
CONFIRM_TIMEOUT = 90


async def running(cp: Machine) -> str:
    return (await cp.succeed("readlink /run/current-system")).strip()


async def profile(cp: Machine) -> str:
    return (
        await cp.succeed("readlink --canonicalize /nix/var/nix/profiles/system")
    ).strip()


async def armed(cp: Machine) -> bool:
    rc, _ = await cp.execute(f"systemctl is-active --quiet {REVERT_UNIT}.timer")
    return rc == 0


async def apply(cp: Machine, obj: dict) -> None:
    encoded = base64.b64encode(json.dumps(obj).encode()).decode()
    await cp.succeed(f"echo {encoded} | base64 --decode | kubectl apply --filename -")


async def roll_to(cp: Machine, toplevel: str, timeout: int = READY_TIMEOUT) -> dict:
    """Point the rollout at `toplevel` and wait until it settles."""
    await apply(
        cp,
        {
            "apiVersion": "nixkube.nixidae.org/v1alpha1",
            "kind": "NixOSRollout",
            "metadata": {"name": ROLLOUT},
            "spec": {
                "nodeSelector": {"matchLabels": {"kubernetes.io/hostname": "cp"}},
                "systems": {"cp": toplevel},
                "confirmTimeout": CONFIRM_TIMEOUT,
            },
        },
    )
    generation = (await get_json(cp, f"get nixosrollout {ROLLOUT}"))["metadata"][
        "generation"
    ]

    async def settled() -> tuple[bool, str]:
        status = (await get_json(cp, f"get nixosrollout {ROLLOUT}")).get("status", {})
        node = status.get("nodes", {}).get("cp", {})
        done = status.get("observedGeneration") == generation and (
            status.get("phase") == "Halted"
            or (status.get("phase") == "Complete" and node.get("target") == toplevel)
        )
        return done, json.dumps(status)

    await until(f"rollout to {toplevel}", settled, timeout, cp)
    return (await get_json(cp, f"get nixosrollout {ROLLOUT}"))["status"]


async def switch_to(cp: Machine, toplevel: str) -> None:
    status = await roll_to(cp, toplevel)
    if status["phase"] != "Complete":
        raise MachineError(f"[cp] the rollout to {toplevel} ended {status}")
    if "leaseRenewTime" in status["nodes"]["cp"]:
        raise MachineError(f"[cp] a Done node keeps its confirm state: {status}")
    if await running(cp) != toplevel or await profile(cp) != toplevel:
        raise MachineError(
            f"[cp] the rollout is Complete and the host is not on {toplevel}"
        )
    if await armed(cp):
        raise MachineError("[cp] the revert is still armed after the rollout")


async def test(vms: Machines) -> None:
    cp, settings = vms.cp, vms.settings
    await kubectl(
        cp,
        f"rollout status --namespace {NAMESPACE} deployment/nixkube-nixos-rollout --timeout={READY_TIMEOUT}s",
    )
    first = await running(cp)
    after = settings["hostNext"]
    if first == after:
        raise MachineError(f"[cp] hostNext is the system the guest booted: {first}")

    await switch_to(cp, after)
    marker = (await cp.succeed(f"cat {MARKER}")).strip()
    if marker != "next":
        raise MachineError(f"[cp] {MARKER} says {marker!r} after the switch")
    print(f"[nixkube] the rollout switched the host to {after}", flush=True)

    await switch_to(cp, first)
    rc, _ = await cp.execute(f"test -e {MARKER}")
    if rc == 0:
        raise MachineError(f"[cp] {MARKER} survived the switch back")
    print(f"[nixkube] the rollout switched the host back to {first}", flush=True)

    await broken_is_reverted(cp, first, settings["hostBroken"])
    await kubectl(cp, "uncordon cp")
    await kubectl(cp, f"delete nixosrollout {ROLLOUT}")


async def broken_is_reverted(cp: Machine, first: str, broken: str) -> None:
    # The host's timer fires `nixosHost.revertDelay` (120 s) after the switch.
    status = await roll_to(cp, broken, READY_TIMEOUT + 120)
    node = status["nodes"]["cp"]
    if status["phase"] != "Halted" or node.get("phase") != "Reverted":
        raise MachineError(f"[cp] the rollout to {broken} ended {status}")
    if node.get("failedSystem") != broken:
        raise MachineError(f"[cp] failedSystem is {node.get('failedSystem')}")
    if await running(cp) != first or await profile(cp) != first:
        raise MachineError(f"[cp] the host is not back on {first}")
    if await armed(cp):
        raise MachineError("[cp] the revert is still armed after the revert")
    spec = (await get_json(cp, "get node cp"))["spec"]
    if not spec.get("unschedulable"):
        raise MachineError("[cp] the reverted node is not cordoned")

    # A succeeded switch Job means the timer reverted, not the Job's own
    # failure branch.
    jobs = await get_json(
        cp,
        f"get jobs --namespace {NAMESPACE} --selector nixkube.nixidae.org/action=switch",
    )
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
    print(
        f"[nixkube] the timer reverted the host from {broken}; the rollout halted",
        flush=True,
    )
