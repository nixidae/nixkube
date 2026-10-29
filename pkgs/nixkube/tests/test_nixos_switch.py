# SPDX-License-Identifier: Apache-2.0

import json
import threading
from pathlib import Path

import pytest

from src.nixos_switch import (
    HOST_STORE,
    SwitchError,
    boot_parts_changed,
    copy_argv,
    enter_host,
    journal_lines,
    store_path,
)


def test_store_path_is_the_first_component_under_the_store() -> None:
    path = Path("/nix/store/abc-nixkube-nixos-switch/bin/nixkube-nixos-switch")
    assert store_path(path) == Path("/nix/store/abc-nixkube-nixos-switch")
    with pytest.raises(ValueError):
        store_path(Path("/nix/var/result/bin/nixkube-nixos-switch"))
    with pytest.raises(ValueError):
        store_path(Path("/nix/store"))


def test_copy_goes_to_the_hosts_daemon() -> None:
    argv = copy_argv("ssh-ng://nix@pynixd", ["/nix/store/abc-system"])
    assert argv[argv.index("--from") + 1] == "ssh-ng://nix@pynixd"
    assert argv[argv.index("--to") + 1] == HOST_STORE
    assert argv[-1] == "/nix/store/abc-system"


def _system(root: Path, **parts: str) -> Path:
    root.mkdir()
    for part, target in parts.items():
        (root / part).symlink_to(target)
    return root


def test_only_a_changed_kernel_initrd_or_modules_needs_a_boot(tmp_path: Path) -> None:
    booted = _system(tmp_path / "booted", kernel="k1", initrd="i1", **{"kernel-modules": "m1"})
    same = _system(tmp_path / "same", kernel="k1", initrd="i1", **{"kernel-modules": "m1"})
    initrd = _system(tmp_path / "initrd", kernel="k1", initrd="i2", **{"kernel-modules": "m1"})
    assert boot_parts_changed(booted, same) == []
    assert boot_parts_changed(booted, initrd) == ["initrd"]


def test_a_part_missing_on_both_sides_is_unchanged(tmp_path: Path) -> None:
    booted = _system(tmp_path / "booted", kernel="k1")
    target = _system(tmp_path / "target", kernel="k1")
    assert boot_parts_changed(booted, target) == []
    assert boot_parts_changed(booted, _system(tmp_path / "other", kernel="k1", initrd="i1")) == ["initrd"]


def _entry(cursor: str, message: object) -> bytes:
    return json.dumps({"__CURSOR": cursor, "MESSAGE": message}).encode()


def test_journal_lines_keeps_a_partial_line_for_the_next_read() -> None:
    whole = _entry("c1", "activating the configuration...")
    rest, lines = journal_lines(whole + b"\n" + _entry("c2", "x")[:10])
    assert lines == [("c1", "activating the configuration...")]
    assert rest == _entry("c2", "x")[:10]


def test_journal_lines_decodes_a_message_journald_sent_as_bytes() -> None:
    _, lines = journal_lines(_entry("c1", list(b"caf\xc3\xa9 \xff")) + b"\n")
    assert lines == [("c1", "café �")]


def test_journal_lines_takes_an_entry_with_no_message() -> None:
    _, lines = journal_lines(_entry("c1", None) + b"\n\n")
    assert lines == [("c1", "")]


def test_entering_the_host_refuses_a_second_thread() -> None:
    stop = threading.Event()
    thread = threading.Thread(target=stop.wait)
    thread.start()
    try:
        with pytest.raises(SwitchError, match="threads"):
            enter_host()
    finally:
        stop.set()
        thread.join()
