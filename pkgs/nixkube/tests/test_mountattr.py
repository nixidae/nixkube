# SPDX-License-Identifier: Apache-2.0

import ctypes
import errno
import os
from collections.abc import Iterator
from pathlib import Path

import pytest

from src import mountattr
from src.mountattr import UserNamespace, own_tree, user_namespace_of


class FakeSyscall:
    """open_tree answers a real fd; mount_setattr answers `setattr_errno`, or 0 when None."""

    def __init__(self, setattr_errno: int | None) -> None:
        self.setattr_errno = setattr_errno

    def __call__(self, number: ctypes.c_long, *_args: object) -> int:
        if number.value == mountattr._NR_OPEN_TREE:
            return os.open(os.devnull, os.O_RDONLY)
        if number.value == mountattr._NR_MOUNT_SETATTR:
            if self.setattr_errno is None:
                return 0
            ctypes.set_errno(self.setattr_errno)
            return -1
        raise AssertionError(f"unexpected syscall {number.value}")


@pytest.fixture
def fresh_probe() -> Iterator[None]:
    mountattr.available.cache_clear()
    yield
    mountattr.available.cache_clear()


@pytest.mark.usefixtures("fresh_probe")
@pytest.mark.parametrize(
    ("setattr_errno", "expected"),
    [(None, True), (errno.ENOSYS, False), (errno.EPERM, False)],
)
def test_probe_answers_what_mount_setattr_answers(
    monkeypatch: pytest.MonkeyPatch, setattr_errno: int | None, expected: bool
) -> None:
    """A 5.2 to 5.11 kernel has open_tree and answers ENOSYS for mount_setattr. nixidae/nixidae#1608."""
    monkeypatch.setattr(mountattr._libc, "syscall", FakeSyscall(setattr_errno))
    assert mountattr.available() is expected


def fake_proc(tmp_path: Path, uid_map: str, gid_map: str) -> Path:
    """A proc directory for pid 7 whose `ns/user` is not the caller's."""
    pid = tmp_path / "7"
    (pid / "ns").mkdir(parents=True)
    (pid / "ns" / "user").touch()
    (pid / "uid_map").write_text(uid_map)
    (pid / "gid_map").write_text(gid_map)
    return tmp_path


def test_own_namespace_is_none() -> None:
    assert user_namespace_of("/proc", os.getpid()) is None


def test_mapped_root_from_the_maps(tmp_path: Path) -> None:
    proc = fake_proc(tmp_path, "         0  514064384      65536\n", "0 514064385 65536\n")
    assert user_namespace_of(str(proc), 7) == UserNamespace(
        path=proc / "7" / "ns" / "user", uid=514064384, gid=514064385
    )


def test_root_found_on_a_later_line(tmp_path: Path) -> None:
    proc = fake_proc(tmp_path, "1000 100000 1\n0 200000 1000\n", "0 200000 65536\n")
    found = user_namespace_of(str(proc), 7)
    assert found is not None
    assert found.uid == 200000


def test_no_root_mapped_is_none(tmp_path: Path) -> None:
    proc = fake_proc(tmp_path, "1000 100000 1\n", "0 200000 65536\n")
    assert user_namespace_of(str(proc), 7) is None


def test_own_tree_reaches_every_file_and_not_through_links(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "nix"
    (root / "var" / "nix" / "db").mkdir(parents=True)
    (root / "var" / "nix" / "db" / "db.sqlite").touch()
    (root / "store").mkdir()
    (root / "var" / "result").symlink_to(tmp_path)
    seen: list[str] = []

    def record(path: str | Path, uid: int, gid: int) -> None:
        seen.append(os.path.relpath(path, root))

    monkeypatch.setattr(os, "lchown", record)
    own_tree(root, UserNamespace(path=Path("/"), uid=1, gid=2))
    assert sorted(seen) == [".", "store", "var", "var/nix", "var/nix/db", "var/nix/db/db.sqlite", "var/result"]
