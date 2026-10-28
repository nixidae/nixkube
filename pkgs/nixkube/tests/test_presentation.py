# SPDX-License-Identifier: Apache-2.0

import pytest

from src import presentation
from src.presentation import Presentation


@pytest.mark.parametrize(
    ("enabled", "kernel", "user_namespace", "host_store", "shown"),
    [
        (True, True, False, False, Presentation.COMPOSEFS),
        (True, True, False, True, Presentation.COMPOSEFS),
        # hostUsers: false: the runtime idmaps the volume, and an overlay
        # cannot be idmapped.
        (True, True, True, False, Presentation.HARDLINKS),
        (True, True, True, True, Presentation.REFUSED),
        # A kernel that failed the probe.
        (True, False, False, False, Presentation.HARDLINKS),
        (True, False, False, True, Presentation.REFUSED),
        (False, True, False, False, Presentation.HARDLINKS),
        (False, True, False, True, Presentation.REFUSED),
    ],
)
def test_csi(
    enabled: bool,
    kernel: bool,
    user_namespace: bool,
    host_store: bool,
    shown: Presentation,
) -> None:
    chosen = presentation.csi(
        enabled=enabled,
        kernel=kernel,
        user_namespace=user_namespace,
        host_store=host_store,
    )
    assert chosen is shown


@pytest.mark.parametrize(
    ("vm", "enabled", "kernel", "bind_farm", "host_store", "shown"),
    [
        (False, True, True, True, False, Presentation.COMPOSEFS),
        (False, True, True, False, True, Presentation.COMPOSEFS),
        (False, True, False, True, False, Presentation.FARM),
        (False, False, True, True, False, Presentation.FARM),
        (False, False, True, False, False, Presentation.HARDLINKS),
        # A link out of the host's store fails with EXDEV.
        (False, False, True, False, True, Presentation.FARM),
        # virtio-fs serves files, so a VM gets a hardlink tree or nothing.
        (True, True, True, True, False, Presentation.HARDLINKS),
        (True, True, True, True, True, Presentation.REFUSED),
    ],
)
def test_nri(
    vm: bool,
    enabled: bool,
    kernel: bool,
    bind_farm: bool,
    host_store: bool,
    shown: Presentation,
) -> None:
    chosen = presentation.nri(
        vm=vm,
        enabled=enabled,
        kernel=kernel,
        bind_farm=bind_farm,
        host_store=host_store,
    )
    assert chosen is shown


@pytest.mark.parametrize(
    ("fits", "host_store", "shown"),
    [
        (True, False, Presentation.FARM),
        (True, True, Presentation.FARM),
        (False, False, Presentation.HARDLINKS),
        (False, True, Presentation.REFUSED),
    ],
)
def test_farm_fallback(fits: bool, host_store: bool, shown: Presentation) -> None:
    assert presentation.farm_fallback(fits=fits, host_store=host_store) is shown


def test_the_chart_reaches_every_outcome() -> None:
    chart = presentation.chart()
    for shown in Presentation:
        assert f'label="{shown}"' in chart, shown
    assert chart.startswith("// Rendered by")


def test_a_question_that_changes_nothing_is_not_asked() -> None:
    def decide(*, a: bool, b: bool) -> Presentation:
        return Presentation.FARM if a else Presentation.HARDLINKS

    root = presentation.tree(decide, ["b", "a"], {})
    assert root == presentation.Question(
        "a",
        presentation.Leaf(Presentation.FARM),
        presentation.Leaf(Presentation.HARDLINKS),
    )
