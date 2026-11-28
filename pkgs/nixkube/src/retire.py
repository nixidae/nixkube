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

**A composefs volume has no source inside it.** Its EROFS layer is mounted
detached, so no mountinfo line shows it, and the overlay's source reads
`overlay`. Only the overlay's `lowerdir` names the volume's directory, as
the separate DaemonSet saw it: `/nix/var/nix-csi/volumes/<id>/erofs`.
`overlay_users` finds those. Measured on sg1, 2026-11-19: without it the
store went under 46 running pods, whose files then failed to open.
"""

from __future__ import annotations

import os
import shutil
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path

import anyio
import anyio.to_thread
import structlog

from .constants import HOST_MOUNT_PATH, HOST_PROC_PATH, HOST_ROOT
from .event_reason import EventReason
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


# Where the separate DaemonSet keeps each volume's and container's own
# directory, as that pod sees it. The names under it are unique, so the
# same name inside the old store means a mount from the old store.
STATE_PREFIX = "/nix/var/nix-csi/"


def overlay_lowers(line: str) -> list[str]:
    """The lower layers of an overlay mountinfo line; [] for any other
    filesystem. `lowerdir=a:b::c` (`::` before a data-only layer) and the
    `lowerdir+=`/`datadir+=` options each name layers."""
    fields = line.split()
    try:
        sep = fields.index("-")
    except ValueError:
        return []
    if len(fields) < sep + 4 or fields[sep + 1] != "overlay":
        return []
    lowers = []
    for option in fields[sep + 3].split(","):
        key, _, value = option.partition("=")
        if key == "lowerdir":
            lowers += [unescape(p) for p in value.split(":") if p]
        elif key in ("lowerdir+", "datadir+"):
            lowers.append(unescape(value))
    return lowers


def overlay_users(in_old_store: Callable[[str], bool], mountinfos: Iterable[str]) -> list[str]:
    """Every overlay mount point whose layers include a volume's or a
    container's directory that `in_old_store` finds in the old store."""
    found = []
    for mountinfo in mountinfos:
        for line in mountinfo.splitlines():
            if any(p.startswith(STATE_PREFIX) and in_old_store(p) for p in overlay_lowers(line)):
                found.append(unescape(line.split()[4]))
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
    root = HOST_ROOT / host_path.lstrip("/")
    mountinfos = list(_namespaces(proc))
    return users(old, mountinfos) + overlay_users(lambda p: (root / p.lstrip("/")).exists(), mountinfos)


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


def _rmtree(path: Path) -> None:
    shutil.rmtree(path)


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
            await anyio.to_thread.run_sync(_rmtree, old)
            OLD_STORE_BYTES.set(0)
            logger.info("old_store_retired", path=host_path)
            await report_event(
                None,
                EventReason.OLD_STORE_RETIRED,
                note=f"{host_path} held the separate store; nothing mounts from it now",
            )
            return
        await anyio.sleep(RETIRE_INTERVAL)
    OLD_STORE_BYTES.set(0)
