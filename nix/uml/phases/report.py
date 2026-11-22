"""The namespace's state, for the reader of a failed run.

`always`, so it runs after any phase failed -- which is when it is read.
`until` already attaches kubelet, containerd, crictl and the pod logs to
the error it raises; what it cannot know about is this namespace.
"""

from nixkube_uml import cluster, report
from vivarium_runner import Machines


async def test(vms: Machines) -> None:
    await report(await cluster(vms), vms.cp)
