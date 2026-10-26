"""A kubeadm cluster: the control plane `cp`, which runs the workloads, and `worker`."""

from vivarium_runner import Machines
from vivarium_runner.cluster import bring_up, kubectl

# The host phase rolls out to `worker`; nothing else runs there. The switch
# Jobs set `nodeName` and tolerate every taint, so this keeps out only what
# the scheduler places.
WORKER_TAINT = "nixkube.nixidae.org/host-only=:NoSchedule"


async def test(vms: Machines) -> None:
    print(
        f"[nixkube] kubernetes {vms.settings['kubernetesVersion']}, the node's store is the sandbox's",
        flush=True,
    )
    cp = await bring_up(vms, schedulable=True)
    await kubectl(cp, f"taint node worker {WORKER_TAINT}")
