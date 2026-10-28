"""What each RuntimeClass the node offers gets from nixkube. Issue #74.

A runtime that runs the container as a host process gets both mounts, as
runc does. A VM runtime gets both read-only, through virtio-fs. A sandboxed
one gets CSI, and NRI refuses it by name.
"""

from typing import Any

from nixkube_uml import NAMESPACE, READY_TIMEOUT, check_sandboxed, check_vm, probe
from vivarium_runner import Machine, MachineError, Machines
from vivarium_runner.cluster import kubectl, until

# The pid a sandboxed runtime reports is not the container's, so NRI cannot
# reach it. Measured for gVisor; see `check_sandboxed`.
SANDBOXED = {"runsc"}

# `nixkube.nri.vmRuntimeHandlers`' default.
VM = {"kata"}


async def test(vms: Machines) -> None:
    cp, settings = vms.cp, vms.settings
    for runtime in settings["runtimes"]:
        if runtime in SANDBOXED:
            await check_sandboxed(cp, settings, runtime)
        elif runtime in VM:
            await check_vm(cp, settings, runtime)
        else:
            await probe(cp, settings, "a clean start", runtime_class=runtime)
    # A pod in its own user namespace sees the node's files through an
    # idmapped mount, and nixkube's store is hardlinks and binds.
    if settings["hostStore"]:
        await check_user_namespace_refused(cp, settings)
        return
    if settings["cri"] != "containerd":
        await probe(cp, settings, "a pod with hostUsers false", host_users=False)
        return
    # containerd cannot start a user-namespaced container from an image with
    # zero layers, which the probe's scratch image is: Lillecarl/containerd#1,
    # vivarium's `containerd-zero-layers`. Asserted, so the day containerd
    # runs it this fails and the probe above takes over.
    try:
        await probe(cp, settings, "a pod with hostUsers false", host_users=False, wants=("ro",))
    except MachineError as error:
        if "StartError" not in str(error):
            raise
        await cp.succeed(
            "kubectl delete jobs --namespace nixkube --wait=true --selector app.kubernetes.io/component=uml-probe",
            timeout=300,
        )
        print(
            "[nixkube] hostUsers false on containerd: fails as Lillecarl/containerd#1 says",
            flush=True,
        )
        return
    raise MachineError(
        "[cp] containerd started a zero-layer pod with hostUsers false: Lillecarl/containerd#1 is"
        " fixed here, so drop this branch and probe as on CRI-O"
    )


async def check_user_namespace_refused(cp: Machine, settings: dict[str, Any]) -> None:
    """On a host-store node a pod with hostUsers false gets no CSI volume,
    and its publish says why: the kernel cannot idmap the composefs overlay,
    and the host's store has no hardlink tree. Issues #25 and #68."""
    created = await cp.succeed(
        f"jq '.spec.template.spec.hostUsers = false' {settings['probeRo']}"
        f" | kubectl create --namespace {NAMESPACE} --filename - --output name"
    )
    job = created.strip().splitlines()[-1].split("/")[-1]

    async def refused() -> tuple[bool, str]:
        events = await kubectl(
            cp,
            f"get events --namespace {NAMESPACE} --output"
            " custom-columns=OBJECT:.involvedObject.name,REASON:.reason,MESSAGE:.message"
            " --no-headers",
        )
        mine = [line for line in events.splitlines() if line.startswith(job)]
        said = any("hostUsers: false" in line for line in mine)
        return said, "\n".join(mine[-3:]) or f"no events for {job} yet"

    await until("the hostUsers false probe to be refused", refused, READY_TIMEOUT, cp)
    await cp.succeed(f"kubectl delete job {job} --namespace {NAMESPACE} --wait=true", timeout=300)
    print(
        "[nixkube] hostUsers false on a host-store node: refused, and says why",
        flush=True,
    )
