"""A kubeadm cluster: the control plane `cp`, which runs the workloads, and `worker`."""

from vivarium_runner import Machines
from vivarium_runner.cluster import KUBE_PROXY, bring_up, kubectl

# The host phase rolls out to `worker`; nothing else runs there. The switch
# Jobs set `nodeName` and tolerate every taint, so this keeps out only what
# the scheduler places.
WORKER_TAINT = "nixkube.nixidae.org/host-only=:NoSchedule"


async def test(vms: Machines) -> None:
    print(
        f"[nixkube] kubernetes {vms.settings['kubernetesVersion']}, the node's store is the sandbox's",
        flush=True,
    )
    # kube-proxy but not CoreDNS, matching `services.vivarium-k8s.skipAddons`.
    # Nothing resolves a name here, but everything in a pod reaches the API
    # server through the `kubernetes.default` ClusterIP, and kube-proxy is
    # what makes that address go anywhere.
    cp = await bring_up(vms, schedulable=True, addons=(KUBE_PROXY,))
    await kubectl(cp, f"taint node worker {WORKER_TAINT}")
