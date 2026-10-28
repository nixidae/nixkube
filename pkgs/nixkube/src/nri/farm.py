# SPDX-License-Identifier: Apache-2.0

"""A closure presented as one read-only bind mount per store path.

## Why not a hardlink tree

A hardlink tree copies structure: `hardlink_tree` links every *file*, and a
node's closure has 375,582 of them at 22us each. A farm binds every *path*,
2443 of them at 14us each, and leaves nothing on disk to collect.

## Why it is built here and not in the daemon

`/proc/sys/fs/mount-max` is 100,000 per mount namespace. At 2443 mounts per
closure the daemon's own namespace would hold about 40 containers' worth
before the next mount fails, so the farm cannot live there. It is built in
the mount worker, which `unshare(CLONE_NEWNS)`s first: that namespace holds
the mounts, `open_tree(OPEN_TREE_CLONE | AT_RECURSIVE)` clones the tree onto
an fd, and the namespace dies with the worker. Nothing persists, so nothing
has to be torn down.

## Why each bind is read-only on its own

A bind shares the inode with the host store. A pod that writes through one
writes into /nix/store on the node. Read-only mounts are what stop that, and
they have to be set per mount: `MS_BIND | MS_REMOUNT | MS_RDONLY` on the
parent covers the parent alone (measured).

Read-write pods get the same farm. Nix never mutates an existing store path
-- a build writes a temporary directory and renames it in -- so the writable
part is the *gaps between* the mounts, on the volume's own disk.

## Why not overlayfs

Two measurements. `OVL_MAX_STACK` is 500 and a closure is larger, and
overlayfs does not follow submounts in a lower layer at all: a farm used as
`lowerdir` presents empty directories.
"""

import ctypes
import ctypes.util
import errno
import os
from collections.abc import Iterable
from pathlib import Path

from ..constants import MS_BIND, MS_REC
from ..mountattr import MountSetattrUnsupported, set_readonly
from ..mountbudget import measure

_libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
_libc.mount.argtypes = [
    ctypes.c_char_p,
    ctypes.c_char_p,
    ctypes.c_char_p,
    ctypes.c_ulong,
    ctypes.c_char_p,
]
_libc.mount.restype = ctypes.c_int

MS_PRIVATE = 1 << 18


class FarmError(OSError):
    """A farm that could not be built. Carries the path that failed."""


def _bind(source: Path, target: Path) -> None:
    ret = _libc.mount(
        ctypes.c_char_p(os.fsencode(source)),
        ctypes.c_char_p(os.fsencode(target)),
        None,
        MS_BIND | MS_REC,
        None,
    )
    if ret != 0:
        err = ctypes.get_errno()
        raise FarmError(err, f"bind {source} -> {target}: {os.strerror(err)}")


def detach_namespace() -> None:
    """Take a private mount namespace, so the farm stays in this process.

    Safe only in the spawned mount worker: `unshare(CLONE_NEWNS)` acts on the
    calling thread, and the daemon's loop thread must not lose its namespace.

    `/` is made private as well. Without that, a mount here propagates back
    out through a shared subtree -- systemd leaves `/` shared -- and the
    daemon's namespace collects the mounts this exists to keep out of it.
    """
    if _libc.unshare(ctypes.c_int(0x00020000)) != 0:  # CLONE_NEWNS
        err = ctypes.get_errno()
        raise FarmError(err, f"unshare(CLONE_NEWNS): {os.strerror(err)}")

    ret = _libc.mount(None, ctypes.c_char_p(b"/"), None, MS_PRIVATE | MS_REC, None)
    if ret != 0:
        err = ctypes.get_errno()
        raise FarmError(err, f"make / private: {os.strerror(err)}")


def place(store_dir: Path, store_paths: Iterable[Path]) -> None:
    """Make, on disk, what each store path binds onto.

    A directory or an empty file per path, and a copy of the link for a
    symlink: a bind needs a directory or a regular file to land on, and a
    store path that is a symlink is complete as a copy. An entry already
    there is left alone.

    Separate from `bind` because a CSI volume is published read-only before
    its farm is bound, and nothing can be created through a read-only mount.
    """
    store_dir.mkdir(parents=True, exist_ok=True)
    for source in store_paths:
        target = store_dir / source.name
        if os.path.lexists(target):
            continue
        if source.is_symlink():
            os.symlink(os.readlink(source), target)
        elif source.is_dir():
            target.mkdir()
        elif source.is_file():
            target.touch()
        else:
            # Not a symlink, not a directory, not a file: it is not there.
            # Going on would give the container an empty store path and no
            # error anywhere, which is the failure `hardlink_tree` learned.
            raise FarmError(errno.ENOENT, f"store path is not there: {source}")


def mount_points(mountinfo: Path = Path("/proc/self/mountinfo")) -> set[str]:
    """Every mount point in this namespace."""
    return {line.split()[4] for line in mountinfo.read_text().splitlines()}


def bind(store_dir: Path, store_paths: Iterable[Path]) -> int:
    """Bind each store path read-only onto its place in `store_dir`.

    Answers how many it bound. A symlink has no bind, and a path already
    mounted is left alone, so this is safe to run twice. `place` first: a
    path with nothing to land on fails here.
    """
    mounted = mount_points()
    bound = 0
    for source in store_paths:
        target = store_dir / source.name
        if source.is_symlink() or str(target) in mounted:
            continue
        _bind(source, target)
        try:
            set_readonly(target, recursive=False)
        except MountSetattrUnsupported as error:
            # No fallback. `MS_REMOUNT | MS_RDONLY` would report success and
            # leave the bind writable into the node's store, which is worse
            # than refusing to mount at all.
            raise FarmError(
                error.errno or 0,
                f"kernel cannot make {target} read-only (needs Linux 5.12)",
            ) from error
        bound += 1
    return bound


def build_farm(store_dir: Path, store_paths: Iterable[Path]) -> int:
    """`place` and `bind` every path in `store_paths` into `store_dir`.

    Answers how many it bound. Safe to run twice over the same directory.

    The budget is checked before the first mount. Past `fs.mount-max`,
    mount(2) answers ENOSPC and says nothing about which limit it meant --
    partway through a closure, leaving a store that is half there.
    """
    store_paths = list(store_paths)
    budget = measure(len(store_paths))
    if not budget.fits:
        raise FarmError(
            errno.ENOSPC,
            f"{len(store_paths)} mounts will not fit: this namespace holds {budget.used} of {budget.limit}",
        )
    place(store_dir, store_paths)
    return bind(store_dir, store_paths)
