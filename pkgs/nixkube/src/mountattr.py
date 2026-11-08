# SPDX-License-Identifier: Apache-2.0

"""mount_setattr(2): the only way to make a mount subtree read-only.

`MS_BIND | MS_REMOUNT | MS_RDONLY` changes the top mount and nothing under
it. Measured on Linux 6.18 against a directory holding three bind mounts: the
submounts stayed writable, and a write through one reaches the host store by
way of the shared inode. `mount_setattr` with `AT_RECURSIVE` covers the
subtree (Linux 5.12).
"""

from __future__ import annotations

import ctypes
import ctypes.util
import errno
import os
from dataclasses import dataclass
from pathlib import Path

# Introduced via the common syscall table, so x86_64 and aarch64 share them.
_NR_OPEN_TREE = 428
_NR_MOVE_MOUNT = 429
_NR_MOUNT_SETATTR = 442

MOUNT_ATTR_RDONLY = 0x1
MOUNT_ATTR_IDMAP = 0x100000
AT_RECURSIVE = 0x8000
AT_EMPTY_PATH = 0x1000
AT_FDCWD = -100
OPEN_TREE_CLONE = 1
MOVE_MOUNT_F_EMPTY_PATH = 0x4

_libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
_libc.syscall.restype = ctypes.c_long


class MountAttr(ctypes.Structure):
    """struct mount_attr, the argument to mount_setattr(2)."""

    _fields_ = [
        ("attr_set", ctypes.c_uint64),
        ("attr_clr", ctypes.c_uint64),
        ("propagation", ctypes.c_uint64),
        ("userns_fd", ctypes.c_uint64),
    ]


class MountSetattrUnsupported(OSError):
    """The kernel has no mount_setattr. Raised only for ENOSYS."""


def set_readonly(path: Path | str, recursive: bool) -> None:
    """Make the mount at `path` read-only, and everything under it if asked.

    Raises MountSetattrUnsupported on a kernel before 5.12, so a caller with a
    fallback can tell that case from a real failure. Every other errno is an
    ordinary OSError.
    """
    attr = MountAttr(attr_set=MOUNT_ATTR_RDONLY, attr_clr=0, propagation=0, userns_fd=0)
    ret = _libc.syscall(
        ctypes.c_long(_NR_MOUNT_SETATTR),
        ctypes.c_int(AT_FDCWD),
        ctypes.c_char_p(os.fsencode(path)),
        ctypes.c_uint(AT_RECURSIVE if recursive else 0),
        ctypes.byref(attr),
        ctypes.c_size_t(ctypes.sizeof(attr)),
    )
    if ret == 0:
        return

    err = ctypes.get_errno()
    message = f"mount_setattr({path!s}, recursive={recursive}): {os.strerror(err)}"
    if err == errno.ENOSYS:
        raise MountSetattrUnsupported(err, message)
    raise OSError(err, message)


@dataclass(frozen=True)
class UserNamespace:
    """A container's user namespace, as a read-write /nix needs it.

    `path` is its `ns/user` file. `uid` and `gid` are the host ids its root
    maps to: an upper directory owned by them is writable by the container's
    root, and an idmapped lower layer shows the store's root-owned files as
    the container's root."""

    path: Path
    uid: int
    gid: int


def _root_of(id_map: str) -> int | None:
    for line in id_map.splitlines():
        inside, outside, _count = (int(field) for field in line.split())
        if inside == 0:
            return outside
    return None


def user_namespace_of(proc: str, pid: int) -> UserNamespace | None:
    """`pid`'s user namespace, or None when it is the caller's own, as for a
    pod without `hostUsers: false`, or when it maps no root."""
    path = Path(f"{proc}/{pid}/ns/user")
    theirs, ours = path.stat(), Path("/proc/self/ns/user").stat()
    if (theirs.st_dev, theirs.st_ino) == (ours.st_dev, ours.st_ino):
        return None
    uid = _root_of(Path(f"{proc}/{pid}/uid_map").read_text())
    gid = _root_of(Path(f"{proc}/{pid}/gid_map").read_text())
    if uid is None or gid is None:
        return None
    return UserNamespace(path=path, uid=uid, gid=gid)


def _check(ret: int, what: str) -> None:
    if ret < 0:
        err = ctypes.get_errno()
        raise OSError(err, f"{what}: {os.strerror(err)}")


def bind_idmapped(source: Path, target: Path, userns: UserNamespace) -> None:
    """Bind `source` at `target`, idmapped to `userns`: a file the host's
    root owns shows as the container's root's. The kernel idmaps only a
    detached clone, and refuses an overlay (measured on 7.2), so `source`
    must be a filesystem that allows it, such as EROFS or ext4."""
    fd = _libc.syscall(
        ctypes.c_long(_NR_OPEN_TREE),
        ctypes.c_int(AT_FDCWD),
        ctypes.c_char_p(os.fsencode(source)),
        ctypes.c_uint(OPEN_TREE_CLONE | os.O_CLOEXEC),
    )
    _check(fd, f"open_tree({source})")
    try:
        ns_fd = os.open(userns.path, os.O_RDONLY | os.O_CLOEXEC)
        try:
            attr = MountAttr(attr_set=MOUNT_ATTR_IDMAP, attr_clr=0, propagation=0, userns_fd=ns_fd)
            ret = _libc.syscall(
                ctypes.c_long(_NR_MOUNT_SETATTR),
                ctypes.c_int(fd),
                ctypes.c_char_p(b""),
                ctypes.c_uint(AT_EMPTY_PATH),
                ctypes.byref(attr),
                ctypes.c_size_t(ctypes.sizeof(attr)),
            )
            _check(ret, f"mount_setattr({source}, MOUNT_ATTR_IDMAP)")
        finally:
            os.close(ns_fd)
        target.mkdir(parents=True, exist_ok=True)
        ret = _libc.syscall(
            ctypes.c_long(_NR_MOVE_MOUNT),
            ctypes.c_int(fd),
            ctypes.c_char_p(b""),
            ctypes.c_int(AT_FDCWD),
            ctypes.c_char_p(os.fsencode(target)),
            ctypes.c_uint(MOVE_MOUNT_F_EMPTY_PATH),
        )
        _check(ret, f"move_mount({source} -> {target})")
    finally:
        os.close(fd)


def own_upper(upper: Path, userns: UserNamespace) -> None:
    """Give an overlay's upper and work directories to the container's root.

    Not idmapped: an idmapped upper makes the kernel mount the overlay
    read-only, "failed to create directory work/work (errno: 75)", measured
    on 7.2. A plain upper owned by the mapped root is writable from inside,
    and a copied-up file lands in it owned by that root."""
    for name in ("upper", "work"):
        os.chown(upper / name, userns.uid, userns.gid)


def own_tree(root: Path, userns: UserNamespace) -> None:
    """Give every file under `root` to the container's root. Only for a tree
    whose files are its own: a hardlink shares its owner with the node's
    store."""
    os.lchown(root, userns.uid, userns.gid)
    for parent, dirs, files in os.walk(root):
        for name in dirs + files:
            os.lchown(os.path.join(parent, name), userns.uid, userns.gid)
