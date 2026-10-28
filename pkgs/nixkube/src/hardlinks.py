# SPDX-License-Identifier: Apache-2.0

import errno
import os
import shutil
import time
from collections.abc import AsyncIterator
from pathlib import Path

import anyio
import anyio.to_thread
import structlog
from anyio.lowlevel import checkpoint

from .errors import HardlinkClosureError
from .metrics import HARDLINK_CLOSURE_DURATION, HARDLINK_CLOSURES, HARDLINK_PATHS

logger = structlog.get_logger("nixkube.hardlinks")

_YIELD_EVERY = 256
"""Directory entries between two checkpoints."""


def _describe(error: Exception) -> str:
    """An OSError from `os.link`, with the side that is actually missing named.

    `Path.hardlink_to(target)` calls `os.link(target, self)`, and Python puts
    the source first in the message whichever side failed. Measured: a link
    whose *destination directory* is gone reports
    `'<store path>' -> '<volume path>'`, the same shape as one whose source
    file is gone. So the message alone cannot tell store corruption from a
    volume that went away mid-walk, which is the question a reader of this log
    always has.

    The answer can change between the failure and this call. `exists` is
    therefore evidence and not proof, which is why both sides are reported
    rather than one verdict.
    """
    if not isinstance(error, OSError) or error.filename is None:
        return str(error)
    sides = [f"source {error.filename} exists={os.path.lexists(error.filename)}"]
    if error.filename2 is not None:
        parent = os.path.dirname(error.filename2)
        sides.append(
            f"target {error.filename2} exists={os.path.lexists(error.filename2)}"
            f" parent={parent} exists={os.path.lexists(parent)}"
        )
    return f"{error}; " + "; ".join(sides)


async def _entries(src: Path) -> AsyncIterator[os.DirEntry[str]]:
    """The entries of `src`, giving the event loop a turn as it goes.

    The loop this runs on serves the NRI heartbeat (`_pump_build_progress`,
    every 10s) and the ZeroMQ REP socket, and `nri-wait` gives up after 30s of
    silence. A walk that never yields therefore kills the container it is
    linking a store into. Issue #45.

    `os.scandir` and not `anyio.Path.iterdir`: hardlinking is a metadata
    syscall that moves no bytes, and every `anyio.Path` call is a thread round
    trip. Measured over 20,000 files: 11.75s through `anyio.Path` against
    1.28s this way, and both hold the loop's longest gap under 0.15s. The
    thread buys responsiveness this already has, at 9x the time.
    """
    with os.scandir(src) as scan:
        for index, entry in enumerate(scan):
            if index % _YIELD_EVERY == 0:
                await checkpoint()
            yield entry


async def hardlink_tree(src: Path, dst: Path) -> None:
    """Hardlink a single path (file or directory tree), preserving symlinks."""
    if src.is_symlink():
        dst.parent.mkdir(parents=True, exist_ok=True)
        os.symlink(os.readlink(src), dst)
    elif src.is_file():
        dst.parent.mkdir(parents=True, exist_ok=True)
        if dst.is_file():
            # Destination is a pre-created empty placeholder file (e.g. for a bind mount).
            # Write content in-place so the inode stays the same — the bind mount is
            # tied to the inode, so unlinking and re-creating would break it.
            #
            # `anyio.Path` here and nowhere else in this file: this is the one
            # branch that moves bytes, so it is the one that can block for long.
            await anyio.Path(dst).write_bytes(await anyio.Path(src).read_bytes())
        else:
            dst.hardlink_to(src)
    elif src.is_dir():
        dst.mkdir(parents=True, exist_ok=True)
        async for entry in _entries(src):
            dst_path = dst / entry.name
            if entry.is_symlink():
                os.symlink(os.readlink(entry.path), dst_path)
            elif entry.is_dir(follow_symlinks=False):
                await hardlink_tree(Path(entry.path), dst_path)
            elif entry.is_file(follow_symlinks=False):
                dst_path.hardlink_to(entry.path)
    else:
        # Not a symlink, not a file, not a directory: `src` is not there.
        #
        # Falling through silently gave the container an empty /nix and no
        # error anywhere, so the failure surfaced later as whatever the
        # application does without its closure. `prepare_volume` passes the
        # output of `nix path-info --recursive`, so a path that has gone
        # missing since means the node store lost it under us -- which is
        # worth failing the container create over, not papering past.
        raise FileNotFoundError(errno.ENOENT, os.strerror(errno.ENOENT), str(src))


def _rmtree(path: Path) -> None:
    shutil.rmtree(path)


async def _discard(path: Path) -> None:
    """Remove a staging path, whatever it is, without raising."""
    try:
        if path.is_dir() and not path.is_symlink():
            # In a thread. A staging directory left by a crashed walk holds
            # most of a store path, and deleting it on this loop is one
            # synchronous gap in the heartbeat `nri-wait` gives up on after
            # 30s -- the failure `_entries` checkpoints to avoid (issue #45).
            await anyio.to_thread.run_sync(_rmtree, path)
        else:
            path.unlink(missing_ok=True)
    except OSError:
        logger.warning("staging_discard_failed", path=str(path), exc_info=True)


async def hardlink_closure(store_paths: set[Path], dst: Path) -> None:
    """
    Hardlink multiple store paths into dst.

    store_paths: {/nix/store/abc-foo, /nix/store/def-bar, ...}
    dst: volume_root/nix/store
    result: dst/abc-foo/..., dst/def-bar/...
    """
    logger.debug("hardlink_closure", paths=store_paths, dst=dst)
    started = time.monotonic()
    HARDLINK_PATHS.inc(len(store_paths))
    try:
        dst.mkdir(parents=True, exist_ok=True)

        for store_path in store_paths:
            target = dst / store_path.name
            # `lexists`, because a store path can be a symlink and a broken
            # one answers False to `exists`. The `os.symlink` below would
            # then fail with EEXIST on every retry.
            if os.path.lexists(target):
                continue  # already linked, and complete: see the rename below

            # Link into a staging name and rename it into place, so a path
            # that exists is a path that is *finished*. A walk that stops
            # partway otherwise leaves a half-filled directory that the next
            # attempt skips as "already copied", and the container gets a
            # truncated closure with nothing logged anywhere.
            #
            # Cancellation is the case that needs the rename rather than the
            # cleanup below: a removed container cancels its own build now,
            # and `CancelledError` is a BaseException, so no `except Exception`
            # runs on the way out.
            staging = dst / f".{store_path.name}.partial"
            await _discard(staging)
            try:
                await hardlink_tree(store_path, staging)
                os.rename(staging, target)
            except Exception as e:
                await _discard(staging)
                HARDLINK_CLOSURES.labels(result="error").inc()
                raise HardlinkClosureError(
                    f"Failed to hardlink {store_path.name}",
                    logs=_describe(e),
                ) from e
    except HardlinkClosureError:
        # Re-raise to prevent outer except Exception from double-wrapping
        raise
    except Exception as e:
        HARDLINK_CLOSURES.labels(result="error").inc()
        raise HardlinkClosureError(
            "Failed to hardlink store paths to volume",
            logs=_describe(e),
        ) from e
    else:
        HARDLINK_CLOSURES.labels(result="ok").inc()
    finally:
        HARDLINK_CLOSURE_DURATION.observe(time.monotonic() - started)


async def deref_hardlink_tree(src: Path, dst: Path) -> None:
    """
    Recursively copy src to dst, dereferencing symlinks and
    hardlinking files for space efficiency.

    Symlink handling:
    - Symlinks to /nix/store that exist: dereference recursively
    - Symlinks to /nix/store that are broken: log warning and copy as-is
    - Symlinks to outside /nix/store: log warning and copy as-is
    """
    src = Path(src)

    if src.is_symlink():
        target = os.readlink(src)
        resolved = (src.parent / target).resolve()

        # Check if target is outside /nix/store
        try:
            resolved.relative_to("/nix/store")
            in_store = True
        except ValueError:
            in_store = False

        if not in_store:
            # Target is outside /nix/store, log warning and copy symlink as-is
            logger.warning("symlink_outside_store", src=str(src), target=target)
            dst.parent.mkdir(parents=True, exist_ok=True)
            os.symlink(target, dst)
        elif not resolved.exists():
            # Target is in /nix/store but broken, log warning and copy as-is
            logger.warning("broken_symlink", src=str(src), target=target)
            dst.parent.mkdir(parents=True, exist_ok=True)
            os.symlink(target, dst)
        else:
            # Target is in /nix/store and exists, dereference it
            await deref_hardlink_tree(resolved, dst)
    elif src.is_file():
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.hardlink_to(src)
    elif src.is_dir():
        dst.mkdir(parents=True, exist_ok=True)
        async for entry in _entries(src):
            await deref_hardlink_tree(Path(entry.path), dst / entry.name)
    else:
        # `src` is not there. A broken symlink is handled above and copied as
        # it is, so reaching here means the path itself is absent.
        raise FileNotFoundError(errno.ENOENT, os.strerror(errno.ENOENT), str(src))
