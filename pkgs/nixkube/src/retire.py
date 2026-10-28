# SPDX-License-Identifier: Apache-2.0

"""Retire the separate store of a node that now shares its host's /nix.

Issue #25. A node that moves to the host-store DaemonSet leaves
`hostMountPath` behind on its disk: the store, every volume prepared in it,
and the empty directories kubelet creates for a pod that never started.

**Deleting it while something mounts from it breaks a pod.** A bind mount
shares the directory's inodes, so the files vanish from the pod as well. So
it goes only when no mount in any mount namespace on the host has its
source inside it. A mount of a parent directory -- `/host`, kubelet's
directory -- does not hold it, and is not counted.

The host's mountinfo names a mount's source by device and by the path inside
that filesystem, not by a host path. `locate` turns the host path into the
same terms first, from pid 1's mountinfo.
"""

from __future__ import annotations

import os
import shutil
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path

import anyio
import anyio.to_thread
import structlog

from .constants import HOST_MOUNT_PATH, HOST_PROC_PATH, HOST_ROOT
from .events import report_event
from .metrics import OLD_STORE_BYTES
from .mountinfo import unescape

logger = structlog.get_logger("nixkube.retire")

# A minute is long enough that the scan is cheap and short enough that a
# node does not hold a dead store for long after its last pod goes.
RETIRE_INTERVAL = 60.0

# Measuring is a walk of every file in the store, seconds for a node's, so
# the size is taken once an hour while the store waits.
SIZE_INTERVAL = 3600.0


@dataclass(frozen=True)
class Located:
    """A host path as mountinfo spells a mount's source."""

    device: str
    """major:minor, field 3."""

    root: str
    """The path inside that filesystem, field 4."""


def unsafe(path: str) -> bool:
    """Paths this must never delete, whatever the option says."""
    return not path.startswith("/") or path.rstrip("/") in ("", "/nix") or path.startswith("/nix/")


def locate(path: str, mountinfo: str) -> Located | None:
    """Where `path` lives: the mount whose mount point is the longest
    prefix of it, and the path inside that mount's filesystem."""
    best: tuple[str, str, str] | None = None
    for line in mountinfo.splitlines():
        fields = line.split()
        point = unescape(fields[4])
        inside = point == "/" or path == point or path.startswith(point + "/")
        if inside and (best is None or len(point) > len(best[0])):
            best = (point, fields[2], unescape(fields[3]))
    if best is None:
        return None
    point, device, root = best
    rest = path if point == "/" else path[len(point) :]
    return Located(device=device, root=root.rstrip("/") + rest)


def users(old: Located, mountinfos: Iterable[str]) -> list[str]:
    """Every mount point, in these mountinfo files, whose source is the old
    store or inside it."""
    found = []
    for mountinfo in mountinfos:
        for line in mountinfo.splitlines():
            fields = line.split()
            root = unescape(fields[3])
            if fields[2] == old.device and (root == old.root or root.startswith(old.root + "/")):
                found.append(unescape(fields[4]))
    return found


def _namespaces(proc: Path) -> Iterator[str]:
    """Each mount namespace's mountinfo, once, by its namespace inode."""
    seen: set[int] = set()
    for entry in os.scandir(proc):
        if not entry.name.isdigit():
            continue
        try:
            ns = os.stat(f"{entry.path}/ns/mnt").st_ino
            if ns in seen:
                continue
            seen.add(ns)
            yield Path(entry.path, "mountinfo").read_text()
        except OSError:
            # The process exited between the listing and the read.
            continue


def scan(host_path: str) -> list[str] | None:
    """The mounts that hold `host_path`, or None when it cannot be located."""
    proc = Path(HOST_PROC_PATH)
    old = locate(host_path, (proc / "1" / "mountinfo").read_text())
    if old is None:
        return None
    return users(old, _namespaces(proc))


def disk_usage(path: Path) -> int:
    """Bytes on disk under `path`, as `du` counts them: a hardlinked inode
    once."""
    seen: set[tuple[int, int]] = set()
    total = 0
    for dirpath, dirnames, filenames in os.walk(path):
        for name in dirnames + filenames:
            try:
                st = os.lstat(os.path.join(dirpath, name))
            except OSError:
                continue
            if (st.st_dev, st.st_ino) not in seen:
                seen.add((st.st_dev, st.st_ino))
                total += st.st_blocks * 512
    return total


async def retire_loop() -> None:
    """Delete the old store once nothing mounts from it, then stop."""
    host_path = str(HOST_MOUNT_PATH)
    if unsafe(host_path):
        logger.error("old_store_unsafe_path", path=host_path)
        return
    old = HOST_ROOT / HOST_MOUNT_PATH.relative_to("/")
    measured = -SIZE_INTERVAL
    while await anyio.Path(old).exists():
        if anyio.current_time() - measured >= SIZE_INTERVAL:
            OLD_STORE_BYTES.set(await anyio.to_thread.run_sync(disk_usage, old))
            measured = anyio.current_time()
        held = await anyio.to_thread.run_sync(scan, host_path)
        if held is None:
            logger.warning("old_store_not_located", path=host_path)
        elif held:
            logger.info("old_store_in_use", path=host_path, mounts=len(held), first=held[0])
        else:
            await anyio.to_thread.run_sync(shutil.rmtree, old)
            OLD_STORE_BYTES.set(0)
            logger.info("old_store_retired", path=host_path)
            await report_event(
                None,
                "OldStoreRetired",
                note=f"{host_path} held the separate store; nothing mounts from it now",
            )
            return
        await anyio.sleep(RETIRE_INTERVAL)
    OLD_STORE_BYTES.set(0)
