"""On a clean start: the workloads get their store paths, and unmount is clean."""

from nixkube_uml import (
    check_presentation,
    check_resident,
    check_unmount,
    check_workloads,
    cluster,
    probe,
)
from vivarium_runner import Machines


async def test(vms: Machines) -> None:
    cp, settings = vms.cp, vms.settings
    api = await cluster(vms)
    await check_workloads(api, cp)
    await probe(api, cp, settings, "a clean start")
    await check_resident(api, cp, settings, "a clean start")
    await check_presentation(cp, settings)
    await check_unmount(api, cp, settings)
