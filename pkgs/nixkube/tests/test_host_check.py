# SPDX-License-Identifier: Apache-2.0

from pathlib import Path

import pytest

from src.host_check import HOST_LABEL, NIXOS, Mode, is_nixos, label_change


def test_the_marker_file_makes_a_host_nixos(tmp_path: Path) -> None:
    assert not is_nixos(tmp_path)
    (tmp_path / "etc").mkdir()
    (tmp_path / "etc/os-release").write_text("ID=nixos\n")
    # os-release alone is not the marker: a container image can carry one.
    assert not is_nixos(tmp_path)
    (tmp_path / "etc/NIXOS").touch()
    assert is_nixos(tmp_path)


@pytest.mark.parametrize(
    ("mode", "nixos", "change"),
    [
        (Mode.SEPARATE, False, None),
        (Mode.SEPARATE, True, {HOST_LABEL: NIXOS}),
        (Mode.HOST, True, None),
        (Mode.HOST, False, {HOST_LABEL: None}),
    ],
)
def test_each_pod_steps_aside_only_on_the_wrong_host(
    mode: Mode, nixos: bool, change: dict[str, str | None] | None
) -> None:
    assert label_change(mode, nixos) == change
