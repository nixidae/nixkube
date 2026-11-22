"""A kubeadm cluster: the control plane `cp`, which runs the workloads, and `worker`."""

from kr8s.asyncio.objects import Node
from nixkube_uml import cluster, raw
from vivarium_k8s import bring_up
from vivarium_runner import Machines

# The host phase rolls out to `worker`; nothing else runs there. The switch
# Jobs set `nodeName` and tolerate every taint, so this keeps out only what
# the scheduler places.
WORKER_TAINT = {"key": "nixkube.nixidae.org/host-only", "effect": "NoSchedule"}


async def test(vms: Machines) -> None:
    print(
        f"[nixkube] kubernetes {vms.settings['kubernetesVersion']}, the node's store is the sandbox's",
        flush=True,
    )
    await bring_up(vms, schedulable=True)
    worker = await Node.get("worker", api=await cluster(vms))
    # A merge patch replaces the whole list, so it carries the taints already there.
    taints = [*raw(worker)["spec"].get("taints", []), WORKER_TAINT]
    await worker.patch({"spec": {"taints": taints}}, type="merge")
