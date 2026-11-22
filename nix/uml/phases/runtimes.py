"""What each RuntimeClass the node offers gets from nixkube. Issue #74.

A runtime that runs the container as a host process gets both mounts, as
runc does. A VM runtime gets both read-only, through virtio-fs. A sandboxed
one gets CSI, and NRI refuses it by name.
"""

import kr8s.asyncio
from kr8s.asyncio.objects import Event
from nixkube_uml import (
    NAMESPACE,
    READY_TIMEOUT,
    Manifest,
    Settings,
    check_sandboxed,
    check_vm,
    cluster,
    created_job,
    delete,
    listed,
    probe,
    raw,
)
from vivarium_k8s import until
from vivarium_runner import Machine, Machines

# The pid a sandboxed runtime reports is not the container's, so NRI cannot
# reach it. Measured for gVisor; see `check_sandboxed`.
SANDBOXED = {"runsc"}

# `nixkube.nri.vmRuntimeHandlers`' default.
VM = {"kata"}


async def test(vms: Machines) -> None:
    cp, settings = vms.cp, vms.settings
    api = await cluster(vms)
    for runtime in settings["runtimes"]:
        if runtime in SANDBOXED:
            await check_sandboxed(api, cp, settings, runtime)
        elif runtime in VM:
            await check_vm(api, cp, settings, runtime)
        else:
            await probe(api, cp, settings, "a clean start", runtime_class=runtime)
    # A pod in its own user namespace sees the node's files through an
    # idmapped mount, and nixkube's store is hardlinks and binds.
    if settings["hostStore"]:
        await check_user_namespace_refused(api, cp, settings)
        return
    # The scratch image has one layer: containerd cannot start a user-namespaced
    # container from zero (Lillecarl/containerd#1, vivarium's `containerd-zero-layers`).
    await probe(api, cp, settings, "a pod with hostUsers false", host_users=False)


async def check_user_namespace_refused(api: kr8s.asyncio.Api, cp: Machine, settings: Settings) -> None:
    """On a host-store node a pod with hostUsers false gets no CSI volume,
    and its publish says why: the kernel cannot idmap the composefs overlay,
    and the host's store has no hardlink tree. Issues #25 and #68."""

    def edit(job: Manifest) -> None:
        job["spec"]["template"]["spec"]["hostUsers"] = False

    job = await created_job(api, settings["probeRo"], edit)

    async def refused() -> tuple[bool, str]:
        events = [raw(e) for e in await listed(Event, api, namespace=NAMESPACE)]
        # The Job's own events, and its pods': a pod's name starts with its Job's.
        mine = [
            f"{e['involvedObject']['name']} {e.get('reason')} {e.get('message', '')}"
            for e in events
            if e["involvedObject"].get("name", "").startswith(job.name)
        ]
        said = any("hostUsers: false" in line for line in mine)
        return said, "\n".join(mine[-3:]) or f"no events for {job.name} yet"

    await until("the hostUsers false probe to be refused", refused, READY_TIMEOUT, cp)
    await delete(job, cp)
    print(
        "[nixkube] hostUsers false on a host-store node: refused, and says why",
        flush=True,
    )
