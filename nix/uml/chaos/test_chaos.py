"""Break the driver every way there is; after each, a new pod must still start.

One test per scenario, in the order `SCENARIOS` lists them, against the one
cluster the earlier phases built. `-k` picks scenarios by name --
`nix run --file . internal.umlTest.driver -- --out ./o -- -k runtime` -- and so
does `NIXKUBE_UML_SCENARIOS`, which `nix/uml/default.nix` turns into `-k`.

Each scenario runs whether an earlier one failed or not, so one run says
which of the nine break the driver, not only the first.
"""

import pytest
from nixkube_uml import SCENARIOS, break_and_recover
from vivarium_runner import Machine, Machines


@pytest.mark.parametrize(
    "name",
    [scenario[0] for scenario in SCENARIOS],
    ids=[scenario[0].replace(" ", "-") for scenario in SCENARIOS],
)
async def test_a_pod_still_starts_after(cp: Machine, vms: Machines, name: str) -> None:
    await break_and_recover(cp, vms.settings, name)
