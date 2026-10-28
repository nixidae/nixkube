# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import json
from pathlib import Path

import pytest
from appstarter import config


def test_a_plain_store_path_passes_through() -> None:
    assert config.store_path_for(" /nix/store/aaa-nodeEnv ") == "/nix/store/aaa-nodeEnv"


def test_the_system_picks_one_out_of_the_map(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, "nix_system", lambda: "aarch64-linux")
    spec = json.dumps({"x86_64-linux": "/nix/store/x", "aarch64-linux": "/nix/store/a"})
    assert config.store_path_for(spec) == "/nix/store/a"


def test_a_map_without_this_system_is_an_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, "nix_system", lambda: "riscv64-linux")
    with pytest.raises(KeyError, match="riscv64-linux"):
        config.store_path_for(json.dumps({"x86_64-linux": "/nix/store/x"}))


def test_the_role_picks_one_of_the_images_fallbacks(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APPSTARTER_ROLE", "node")
    monkeypatch.setenv("APPSTARTER_FALLBACK_NODE", "/nix/store/n")
    monkeypatch.setenv("APPSTARTER_FALLBACK_CACHE", "/nix/store/c")
    assert config.fallback() == "/nix/store/n"


def test_an_explicit_fallback_wins_over_the_role(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APPSTARTER_ROLE", "node")
    monkeypatch.setenv("APPSTARTER_FALLBACK_NODE", "/nix/store/n")
    monkeypatch.setenv("APPSTARTER_FALLBACK", "/nix/store/explicit")
    assert config.fallback() == "/nix/store/explicit"


@pytest.mark.parametrize(
    "environment",
    [
        {},
        {"APPSTARTER_ROLE": "node"},
        {"APPSTARTER_FALLBACK_NODE": "/nix/store/n"},
        {"APPSTARTER_ROLE": "builder", "APPSTARTER_FALLBACK_NODE": "/nix/store/n"},
    ],
)
def test_no_fallback_is_none_rather_than_a_guess(monkeypatch: pytest.MonkeyPatch, environment: dict[str, str]) -> None:
    """`init` fails loudly instead. A wrong environment starts a wrong node."""
    for name in ("APPSTARTER_FALLBACK", "APPSTARTER_ROLE", "APPSTARTER_FALLBACK_NODE"):
        monkeypatch.delenv(name, raising=False)
    for name, value in environment.items():
        monkeypatch.setenv(name, value)
    assert config.fallback() is None


def test_state_survives_a_round_trip(tmp_path: Path) -> None:
    config.State(wanted="/nix/store/w", running="/nix/store/w").write(tmp_path)
    assert config.State.read(tmp_path) == config.State("/nix/store/w", "/nix/store/w")


def test_state_is_degraded_when_the_two_differ(tmp_path: Path) -> None:
    config.State(wanted="/nix/store/w", running="/nix/store/f").write(tmp_path)
    state = config.State.read(tmp_path)
    assert state is not None
    assert state.degraded


@pytest.mark.parametrize("content", ["", "not json", "{}", '{"wanted": "/nix/store/w"}'])
def test_unreadable_state_is_none_rather_than_an_error(tmp_path: Path, content: str) -> None:
    """`run` starts what the store holds either way. Only the report is lost."""
    path = tmp_path / config.STATE_PATH
    path.parent.mkdir(parents=True)
    path.write_text(content)
    assert config.State.read(tmp_path) is None


def test_missing_state_is_none(tmp_path: Path) -> None:
    assert config.State.read(tmp_path) is None
