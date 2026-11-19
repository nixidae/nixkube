# SPDX-License-Identifier: Apache-2.0

from pathlib import Path

import pytest

from src.nix_daemon import host_version_refusal, minor_version, prepare_socket


@pytest.mark.parametrize(
    ("text", "version"),
    [
        ("nix (Nix) 2.35.2\n", (2, 35)),
        ("2.35.3", (2, 35)),
        ("nix (Determinate Nix 3.8.1) 2.30.0", (3, 8)),
        ("no version here", None),
    ],
)
def test_minor_version(text: str, version: tuple[int, int] | None) -> None:
    assert minor_version(text) == version


@pytest.mark.parametrize(
    ("ours", "host", "refused"),
    [
        # sg1, 2026-11-19: nixkube 2.35.2 on hosts with 2.35.3.
        ("nix (Nix) 2.35.2", "2.35.3", False),
        ("nix (Nix) 2.35.9", "2.35.3", False),
        ("nix (Nix) 2.34.0", "2.35.3", False),
        ("nix (Nix) 2.36.0", "2.35.3", True),
        ("nix (Nix) 3.0.0", "2.35.3", True),
        # Nothing to compare: no refusal on a guess.
        ("nix (Nix) 2.36.0", "", False),
    ],
)
def test_only_a_newer_nix_than_the_hosts_is_refused(ours: str, host: str, refused: bool) -> None:
    assert (host_version_refusal(ours, host) is not None) is refused


def test_prepare_socket_makes_the_directory_and_drops_a_stale_socket(tmp_path: Path) -> None:
    socket = tmp_path / "nix-csi" / "daemon-socket" / "socket"
    prepare_socket(socket)
    assert socket.parent.is_dir()
    socket.write_text("stale")
    prepare_socket(socket)
    assert not socket.exists()
