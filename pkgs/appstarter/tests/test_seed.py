# SPDX-License-Identifier: Apache-2.0

import logging

import pytest
from appstarter import config, seed, store


@pytest.fixture(autouse=True)
def _no_sleeping(monkeypatch):
    monkeypatch.setattr(seed.time, "sleep", lambda _seconds: None)


@pytest.fixture(autouse=True)
def _no_verification(monkeypatch):
    monkeypatch.setattr(store, "verify", lambda *_args: None)


def test_a_successful_fetch_records_no_skew(tmp_path, monkeypatch):
    monkeypatch.setenv("PYNIXD_ENABLED", "false")
    monkeypatch.setattr(store, "build", lambda *_args: None)

    assert seed.run("/nix/store/wanted", "/nix/store/image", tmp_path) == 0
    state = config.State.read(tmp_path)
    assert state == config.State("/nix/store/wanted", "/nix/store/wanted")
    assert not state.degraded


@pytest.mark.parametrize("fetch_works", [True, False])
def test_what_runs_is_rooted_by_a_link_the_host_can_resolve(tmp_path, monkeypatch, fetch_works):
    monkeypatch.setenv("PYNIXD_ENABLED", "false")
    monkeypatch.setenv("APPSTARTER_ROLE", "node")

    def refuse(*_args):
        raise store.StoreError("no substituter that can build it")

    monkeypatch.setattr(store, "build", (lambda *_args: None) if fetch_works else refuse)
    monkeypatch.setattr(store, "copy", lambda *_args: None)

    assert seed.run("/nix/store/wanted", "/nix/store/image", tmp_path) == 0
    root = tmp_path / config.ROOT_DIR / "node"
    # A symlink in the gcroots directory, straight to the store path: not a
    # path under this container's /nix-volume.
    expected = "/nix/store/wanted" if fetch_works else "/nix/store/image"
    assert root.is_symlink() and str(root.readlink()) == expected


def test_a_failed_fetch_seeds_from_the_image(tmp_path, monkeypatch):
    monkeypatch.setenv("PYNIXD_ENABLED", "false")
    copied: list[str] = []

    def refuse(*_args):
        raise store.StoreError("no substituter that can build it")

    monkeypatch.setattr(store, "build", refuse)
    monkeypatch.setattr(store, "copy", lambda target, *_args: copied.append(target))

    assert seed.run("/nix/store/wanted", "/nix/store/image", tmp_path) == 0
    assert copied == ["/nix/store/image"]
    state = config.State.read(tmp_path)
    assert state == config.State("/nix/store/wanted", "/nix/store/image")
    assert state.degraded


def test_a_failed_fetch_with_no_fallback_fails(tmp_path, monkeypatch):
    monkeypatch.setenv("PYNIXD_ENABLED", "false")

    def refuse(*_args):
        raise store.StoreError("no substituter that can build it")

    monkeypatch.setattr(store, "build", refuse)

    assert seed.run("/nix/store/wanted", None, tmp_path) == 1
    assert config.State.read(tmp_path) is None


def test_only_an_unreachable_pynixd_is_asked_twice(tmp_path, monkeypatch):
    """A pynixd that answered will not have the path a minute later."""
    monkeypatch.setenv("PYNIXD_ENABLED", "true")
    monkeypatch.setattr(store, "ping", lambda *_args, **_kwargs: True)
    attempts = 0

    def refuse(*_args):
        nonlocal attempts
        attempts += 1
        raise store.StoreError("pynixd does not have it")

    monkeypatch.setattr(store, "build", refuse)
    monkeypatch.setattr(store, "copy", lambda *_args: None)

    seed.run("/nix/store/wanted", "/nix/store/image", tmp_path)
    assert attempts == 1


def test_an_unreachable_pynixd_is_retried(tmp_path, monkeypatch):
    """Issue #27: pynixd may be starting elsewhere, waiting on this node."""
    monkeypatch.setenv("PYNIXD_ENABLED", "true")
    monkeypatch.setattr(store, "ping", lambda *_args, **_kwargs: False)
    attempts = 0

    def refuse(*_args):
        nonlocal attempts
        attempts += 1
        raise store.StoreError("no substituter that can build it")

    monkeypatch.setattr(store, "build", refuse)
    monkeypatch.setattr(store, "copy", lambda *_args: None)

    seed.run("/nix/store/wanted", "/nix/store/image", tmp_path)
    assert attempts == seed._MAX_ATTEMPTS


def _held(caplog) -> list[str]:
    """Every "store holds" line `seed.run` logged, rendered."""
    return [record.getMessage() for record in caplog.records if record.getMessage().startswith("store holds")]


def test_the_two_store_holds_lines_stay_distinguishable(tmp_path, monkeypatch, caplog):
    """`nix/uml/helpers/nixkube_uml.py` reads these out of the initContainer's log.

    Both outcomes log "store holds", so the successful line is a prefix of
    the degraded one. `FALLBACK_MARK` there is the only substring that tells
    them apart, and this repeats it: a reworded message fails here rather
    than quietly stopping that test from catching a fallback.
    """
    fallback_mark = ", and the deployment asks for "
    monkeypatch.setenv("PYNIXD_ENABLED", "false")
    monkeypatch.setattr(store, "copy", lambda *_args: None)
    caplog.set_level(logging.INFO, logger="appstarter.init")

    monkeypatch.setattr(store, "build", lambda *_args: None)
    seed.run("/nix/store/wanted", "/nix/store/image", tmp_path)
    assert _held(caplog) == ["store holds /nix/store/wanted"]

    caplog.clear()

    def refuse(*_args):
        raise store.StoreError("no substituter that can build it")

    monkeypatch.setattr(store, "build", refuse)
    seed.run("/nix/store/wanted", "/nix/store/image", tmp_path)
    assert _held(caplog) == [f"store holds /nix/store/image{fallback_mark}/nix/store/wanted"]


def test_a_retry_that_succeeds_stops_retrying(tmp_path, monkeypatch):
    monkeypatch.setenv("PYNIXD_ENABLED", "true")
    monkeypatch.setattr(store, "ping", lambda *_args, **_kwargs: False)
    attempts = 0

    def flaky(*_args):
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise store.StoreError("pynixd did not answer")

    monkeypatch.setattr(store, "build", flaky)

    assert seed.run("/nix/store/wanted", "/nix/store/image", tmp_path) == 0
    assert attempts == 3
    assert config.State.read(tmp_path).running == "/nix/store/wanted"
