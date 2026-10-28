# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import os
from pathlib import Path

import pytest
from appstarter import config, start


@pytest.fixture
def seeded(tmp_path: Path) -> Path:
    """A store with a program in it, as `appstarter init` leaves one."""
    target = tmp_path / "nix" / "store" / "env"
    (target / "bin").mkdir(parents=True)
    (target / "bin" / "nixkube").write_text("#!/bin/sh\n")
    (tmp_path / config.RESULT_PATH).parent.mkdir(parents=True, exist_ok=True)
    (tmp_path / config.RESULT_PATH).symlink_to(target)
    return tmp_path


@pytest.fixture
def execv(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, list[str]]]:
    calls: list[tuple[str, list[str]]] = []
    monkeypatch.setattr(os, "execv", lambda path, argv: calls.append((path, argv)))
    return calls


def test_a_missing_program_fails_rather_than_execs(tmp_path: Path, execv: list[tuple[str, list[str]]]) -> None:
    assert start.run("nixkube", [], tmp_path) == 1
    assert execv == []


def test_the_two_versions_reach_the_application(
    seeded: Path, execv: list[tuple[str, list[str]]], monkeypatch: pytest.MonkeyPatch
) -> None:
    # `setenv` and not `delenv`: monkeypatch restores what it replaced, so
    # this is what keeps `start.run`'s writes out of the next test.
    monkeypatch.setenv("APPSTARTER_WANTED_STORE_PATH", "unset")
    monkeypatch.setenv("APPSTARTER_RUNNING_STORE_PATH", "unset")
    config.State(wanted="/nix/store/w", running="/nix/store/f").write(seeded)

    start.run("nixkube", ["--verbose"], seeded)

    assert os.environ["APPSTARTER_WANTED_STORE_PATH"] == "/nix/store/w"
    assert os.environ["APPSTARTER_RUNNING_STORE_PATH"] == "/nix/store/f"
    assert execv[0][1] == ["nixkube", "--verbose"]


def test_a_store_with_no_state_still_starts(seeded: Path, execv: list[tuple[str, list[str]]]) -> None:
    """The report is lost. The node is not."""
    start.run("nixkube", [], seeded)
    assert len(execv) == 1
