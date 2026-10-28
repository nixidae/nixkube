# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import stat
from pathlib import Path

import pytest
from appstarter import setup


def _fake_nss(tmp_path: Path) -> Path:
    etc = tmp_path / "nss" / "etc"
    etc.mkdir(parents=True)
    for name in ("passwd", "group", "nsswitch.conf"):
        (etc / name).write_text(f"{name} from the image\n")
    return tmp_path / "nss"


def test_nix_gets_a_sticky_tmp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("FAKE_NSS", raising=False)
    setup.prepare(tmp_path)
    assert stat.S_IMODE((tmp_path / "tmp").stat().st_mode) == 0o1777
    assert stat.S_IMODE((tmp_path / "var" / "tmp").stat().st_mode) == 0o1777


def test_the_user_database_comes_from_the_image(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FAKE_NSS", str(_fake_nss(tmp_path)))
    setup.prepare(tmp_path)
    assert (tmp_path / "etc" / "passwd").read_text() == "passwd from the image\n"


def test_a_mounted_file_survives(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A node that mounts its own /etc/passwd keeps it."""
    monkeypatch.setenv("FAKE_NSS", str(_fake_nss(tmp_path)))
    (tmp_path / "etc").mkdir()
    (tmp_path / "etc" / "passwd").write_text("mine\n")
    setup.prepare(tmp_path)
    assert (tmp_path / "etc" / "passwd").read_text() == "mine\n"


def test_no_fake_nss_leaves_etc_alone(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("FAKE_NSS", raising=False)
    setup.prepare(tmp_path)
    assert not (tmp_path / "etc").exists()
