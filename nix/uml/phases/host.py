"""Jobs from the `nixkube-nixos-host` template switch the NixOS host (#359).

The phase plays the controller: it makes each Job, confirms a switch by a
kubelet Lease renewal after it, and disarms the revert. `hostNext` is this
guest's own configuration plus one file, so the switch changes that file and
nothing the cluster runs on. `hostBroken` also stops the kubelet: no pod can
run after the switch, and only the host's armed timer brings it back.
"""

import base64
import json
from datetime import UTC, datetime

from nixkube_uml import READY_TIMEOUT
from vivarium_runner import Machine, MachineError, Machines
from vivarium_runner.cluster import get_json, kubectl, until

NAMESPACE = "nixkube"
TEMPLATE = "nixkube-nixos-host"
REVERT_UNIT = "nixkube-revert"
MARKER = "/etc/nixkube-host-generation"
# Short, so the broken switch reverts within the phase; the good switches
# confirm and disarm well inside it.
REVERT_DELAY = 120

jobs = 0


async def running(cp: Machine) -> str:
    return (await cp.succeed("readlink /run/current-system")).strip()


async def profile(cp: Machine) -> str:
    return (
        await cp.succeed("readlink --canonicalize /nix/var/nix/profiles/system")
    ).strip()


async def armed(cp: Machine) -> bool:
    rc, _ = await cp.execute(f"systemctl is-active --quiet {REVERT_UNIT}.timer")
    return rc == 0


async def start_job(cp: Machine, action: str, toplevel: str = "") -> str:
    global jobs
    jobs += 1
    name = f"nixos-host-{action}-{jobs}"
    template = (
        await get_json(cp, f"get podtemplate --namespace {NAMESPACE} {TEMPLATE}")
    )["template"]
    template["spec"]["nodeName"] = "cp"
    (container,) = template["spec"]["containers"]
    values = {"ACTION": action, "TOPLEVEL": toplevel, "REVERT_DELAY": str(REVERT_DELAY)}
    container["env"] = [
        {"name": e["name"], "value": values.get(e["name"], e.get("value", ""))}
        for e in container["env"]
    ]
    job = {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "metadata": {"name": name, "namespace": NAMESPACE},
        "spec": {"backoffLimit": 0, "template": template},
    }
    encoded = base64.b64encode(json.dumps(job).encode()).decode()
    await cp.succeed(f"echo {encoded} | base64 --decode | kubectl create --filename -")
    return name


async def run_job(cp: Machine, action: str, toplevel: str = "") -> None:
    name = await start_job(cp, action, toplevel)

    async def finished() -> tuple[bool, str]:
        job = await get_json(cp, f"get job --namespace {NAMESPACE} {name}")
        status = job.get("status", {})
        if status.get("failed"):
            logs = await kubectl(cp, f"logs --namespace {NAMESPACE} job/{name}")
            raise MachineError(f"[cp] job {name} failed:\n{logs}")
        return bool(status.get("succeeded")), json.dumps(status)

    await until(f"job {name}", finished, READY_TIMEOUT, cp)


async def guest_now(cp: Machine) -> datetime:
    """The guest's clock, which the kubelet stamps its Lease with."""
    return datetime.fromtimestamp(float(await cp.succeed("date +%s.%N")), UTC)


async def confirm(cp: Machine, since: datetime) -> None:
    async def renewed() -> tuple[bool, str]:
        lease = await get_json(cp, "get lease --namespace kube-node-lease cp")
        renew = lease["spec"].get("renewTime")
        node = await get_json(cp, "get node cp")
        ready = any(
            c["type"] == "Ready" and c["status"] == "True"
            for c in node["status"]["conditions"]
        )
        done = renew is not None and datetime.fromisoformat(renew) > since and ready
        return done, f"lease renewed {renew}, Ready={ready}"

    await until("the kubelet to renew its lease", renewed, READY_TIMEOUT, cp)


async def switch_to(cp: Machine, toplevel: str) -> None:
    await run_job(cp, "switch", toplevel)
    if await running(cp) != toplevel or await profile(cp) != toplevel:
        raise MachineError(
            f"[cp] the switch job succeeded and the host is not on {toplevel}"
        )
    if not await armed(cp):
        raise MachineError("[cp] the switch did not arm the revert")
    await confirm(cp, await guest_now(cp))
    await run_job(cp, "disarm")
    if await armed(cp):
        raise MachineError("[cp] the revert is still armed after disarm")


async def test(vms: Machines) -> None:
    cp, settings = vms.cp, vms.settings
    first = await running(cp)
    after = settings["hostNext"]
    if first == after:
        raise MachineError(f"[cp] hostNext is the system the guest booted: {first}")

    await switch_to(cp, after)
    marker = (await cp.succeed(f"cat {MARKER}")).strip()
    if marker != "next":
        raise MachineError(f"[cp] {MARKER} says {marker!r} after the switch")
    print(f"[nixkube] the host switched to {after}", flush=True)

    await switch_to(cp, first)
    rc, _ = await cp.execute(f"test -e {MARKER}")
    if rc == 0:
        raise MachineError(f"[cp] {MARKER} survived the switch back")
    print(f"[nixkube] the host switched back to {first}", flush=True)

    await broken_is_reverted(cp, first, settings["hostBroken"])


async def broken_is_reverted(cp: Machine, first: str, broken: str) -> None:
    """Nothing confirms or disarms: the kubelet is down, so no pod can. The
    host's timer reverts on its own."""
    job = await start_job(cp, "switch", broken)

    async def reached() -> tuple[bool, str]:
        out = await cp.succeed("readlink /nix/var/nix/profiles/system-*-link")
        return broken in out.split(), "profile generations: " + " ".join(out.split())

    await until(f"the host to switch to {broken}", reached, READY_TIMEOUT, cp)

    async def reverted() -> tuple[bool, str]:
        now = await running(cp)
        kubelet, _ = await cp.execute("systemctl is-active --quiet kubelet")
        return (
            now == first and kubelet == 0 and not await armed(cp),
            f"running {now}, kubelet active={kubelet == 0}",
        )

    await until(f"the host to revert from {broken}", reverted, READY_TIMEOUT, cp)
    if await profile(cp) != first:
        raise MachineError(
            f"[cp] the system profile is {await profile(cp)} after the revert"
        )
    await confirm(cp, await guest_now(cp))

    # The kubelet reports the pod once it is back. A succeeded switch means
    # the timer reverted, not the Job's own failure branch.
    async def succeeded() -> tuple[bool, str]:
        status = (await get_json(cp, f"get job --namespace {NAMESPACE} {job}")).get(
            "status", {}
        )
        if status.get("failed"):
            raise MachineError(
                f"[cp] the switch to {broken} failed, so the timer went untested"
            )
        return bool(status.get("succeeded")), json.dumps(status)

    await until(f"job {job}", succeeded, READY_TIMEOUT, cp)
    print(f"[nixkube] the timer reverted the host from {broken} to {first}", flush=True)
