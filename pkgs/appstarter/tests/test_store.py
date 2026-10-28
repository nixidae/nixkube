# SPDX-License-Identifier: Apache-2.0

import subprocess

import pytest
from appstarter import store


def _completed(returncode: int, stdout: str = "", stderr: str = ""):
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


def test_link_replaces_an_existing_pointer(tmp_path):
    link = tmp_path / "result"
    store.link("/nix/store/first", link)
    store.link("/nix/store/second", link)
    assert link.readlink().as_posix() == "/nix/store/second"


def test_link_leaves_no_staging_file_behind(tmp_path):
    link = tmp_path / "nested" / "result"
    store.link("/nix/store/first", link)
    assert [p.name for p in link.parent.iterdir()] == ["result"]


def test_verify_accepts_a_closure_that_is_on_disk(tmp_path, monkeypatch):
    (tmp_path / "nix" / "store").mkdir(parents=True)
    (tmp_path / "nix" / "store" / "aaa").write_text("")
    monkeypatch.setattr(store, "_run", lambda *a, **k: _completed(0, "/nix/store/aaa\n"))
    store.verify("/nix/store/aaa", tmp_path)


def test_verify_rejects_a_path_that_is_registered_and_absent(tmp_path, monkeypatch):
    """Issue #8. `nix path-info` answers from the database, so it passes."""
    monkeypatch.setattr(store, "_run", lambda *a, **k: _completed(0, "/nix/store/gone\n"))
    with pytest.raises(store.StoreError, match="absent"):
        store.verify("/nix/store/gone", tmp_path)


def test_verify_accepts_a_dangling_symlink(tmp_path, monkeypatch):
    """`nix-*-man` points at an absolute path that resolves only under /nix."""
    (tmp_path / "nix" / "store").mkdir(parents=True)
    (tmp_path / "nix" / "store" / "man").symlink_to("/nix/store/elsewhere")
    monkeypatch.setattr(store, "_run", lambda *a, **k: _completed(0, "/nix/store/man\n"))
    store.verify("/nix/store/man", tmp_path)


def test_build_reports_what_nix_said(tmp_path, monkeypatch):
    """`_run_streaming`, because a fetch shows its stderr as it arrives.

    The message still carries what nix said -- that is what a caller which
    never saw the log has to go on.
    """
    monkeypatch.setattr(
        store,
        "_run_streaming",
        lambda *a, **k: _completed(1, stderr="no substituter\n"),
    )
    with pytest.raises(store.StoreError, match="no substituter"):
        store.build("/nix/store/aaa", tmp_path, ["local"], tmp_path / "result")


def test_build_falls_back_when_nothing_answers_in_time(tmp_path, monkeypatch):
    """A fetch that never returns has to become a `StoreError`, not a hang.

    `seed.run` seeds from the image when `build` raises. With no bound on
    the subprocess there was nothing to raise, so an init container sat in
    `Init:0/1` for as long as anyone left it -- 22 minutes on nixlab2 --
    and the fallback that exists for exactly this never ran.
    """

    def never_returns(*_a, **_k):
        raise subprocess.TimeoutExpired(cmd="nix", timeout=store.BUILD_TIMEOUT)

    monkeypatch.setattr(store, "_run_streaming", never_returns)
    with pytest.raises(store.StoreError, match="nothing answered within"):
        store.build("/nix/store/aaa", tmp_path, ["local"], tmp_path / "result")


def test_copy_reads_and_writes_local_stores(tmp_path, monkeypatch):
    """Both ends are local, because `appstarter init` runs no Nix daemon.

    Without `--from`, `nix copy` reads the default store and goes through
    the daemon socket, which is not there. The fallback then dies on a
    missing socket -- the one path that exists to survive a store the node
    cannot reach.
    """
    seen: list[tuple[str, ...]] = []
    monkeypatch.setattr(store, "_run", lambda *a, **_k: seen.append(a) or _completed(0))

    store.copy("/nix/store/aaa", tmp_path, tmp_path / "result")

    args = seen[0]
    assert args[args.index("--from") + 1] == store.LOCAL_STORE
    assert args[args.index("--to") + 1] == str(tmp_path)


def test_copy_reports_what_nix_said(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "_run", lambda *a, **k: _completed(1, stderr="cannot connect\n"))
    with pytest.raises(store.StoreError, match="cannot connect"):
        store.copy("/nix/store/aaa", tmp_path, tmp_path / "result")


def test_ping_treats_a_timeout_as_no_answer(monkeypatch):
    def timeout(*_args, **_kwargs):
        raise subprocess.TimeoutExpired(cmd="nix", timeout=1)

    monkeypatch.setattr(store, "_run", timeout)
    assert store.ping("ssh-ng://nix@pynixd") is False
