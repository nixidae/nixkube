"""nixkube's manifest applied, and its driver and pynixd up."""

from nixkube_uml import cluster, deploy, wait_for_driver, wait_for_pynixd
from vivarium_runner import Machines


async def test(vms: Machines) -> None:
    api = await cluster(vms)
    await deploy(api, vms.settings)
    await wait_for_driver(api, vms.cp, vms.settings)
    await wait_for_pynixd(api, vms.cp)
