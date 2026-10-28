# SPDX-License-Identifier: Apache-2.0

"""Async garbage collection loop for the Nix store.

Ports the logic from environments/modules/gc.nix:
1. Optionally copies all store paths to the cache over SSH.
2. Deletes store paths older than GC_KEEP_SECONDS.
3. Sleeps a randomised interval before repeating.

Every nix call here carries a timeout. The loop awaits one cycle at a time, so
one call that never returns ends collection on that node. Issue #38 measured
that: 33 hours on a four-node cluster, zero completed cycles.
"""

import random
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import BinaryIO

import anyio
import ijson
import structlog
from anyio.lowlevel import checkpoint

from .cache import copy_to_cache
from .constants import (
    GC_DELETE_TIMEOUT_SECONDS,
    GC_INTERVAL_SECONDS,
    GC_KEEP_SECONDS,
    GC_PATH_INFO_TIMEOUT_SECONDS,
    GC_STALL_CYCLES,
    PYNIXD_ENABLED,
)
from .metrics import (
    GC_CYCLE_DURATION,
    GC_CYCLES,
    GC_LAST_SUCCESS,
    GC_PATHS_DELETED,
    STORE_NAR_BYTES,
    STORE_PATHS,
)

logger = structlog.get_logger("nixkube.gc")

# `nix store delete` says this, one line per path, and exits 1. It is the
# normal outcome and not a failure: the loop offers every path older than
# GC_KEEP_SECONDS, and most of them are still in use.
_STILL_ALIVE = "since it is still alive"


async def gc_loop() -> None:
    """Run garbage collection on the local Nix store in a loop.

    Non-fatal: exceptions are logged as warnings and the loop continues.
    """
    last_ok = time.monotonic()
    while True:
        stalled_for = time.monotonic() - last_ok
        if stalled_for > GC_STALL_CYCLES * GC_INTERVAL_SECONDS:
            # Loud, because the store keeps growing while this is true and
            # nothing else on the node says so.
            logger.error(
                "gc_stalled",
                seconds_since_last_cycle=round(stalled_for),
                cycles=GC_STALL_CYCLES,
            )

        started = time.monotonic()
        try:
            await _run_gc_cycle()
        except anyio.get_cancelled_exc_class():
            raise
        except Exception:
            # Counted, and not only logged. A cycle that fails every time
            # leaves the store growing, and the daemon says nothing about it
            # otherwise: the loop swallows the error and sleeps.
            GC_CYCLES.labels(result="error").inc()
            logger.warning("gc_error", exc_info=True)
        else:
            GC_CYCLES.labels(result="ok").inc()
            last_ok = time.monotonic()
            # `time.time` and not `time.monotonic`: this one leaves the node.
            GC_LAST_SUCCESS.set(time.time())
        finally:
            GC_CYCLE_DURATION.observe(time.monotonic() - started)

        sleep_secs = random.uniform(GC_INTERVAL_SECONDS / 2, GC_INTERVAL_SECONDS)
        logger.debug("gc_sleeping", seconds=round(sleep_secs, 1))
        await anyio.sleep(sleep_secs)


_SCAN_YIELD_EVERY = 2000
"""Store paths between two checkpoints while reading the store's metadata."""


@dataclass
class StoreScan:
    """What one pass over the store's metadata answers."""

    paths: int = 0
    nar_bytes: int = 0
    old_paths: list[str] = field(default_factory=list)


def _scan_entry(scan: StoreScan, path: str, facts: object, cutoff: float) -> None:
    """Fold one path's metadata into the scan.

    `nix path-info --json` answers an object keyed by store path, and the value
    holds no `path` of its own. Reading it as a list of entries walked the
    keys, which are strings, so the filter matched nothing and the loop deleted
    nothing on any node, ever. Issue #38.
    """
    scan.paths += 1
    if not isinstance(facts, dict):
        return
    scan.nar_bytes += facts.get("narSize") or 0
    if facts.get("registrationTime", cutoff + 1) < cutoff:
        scan.old_paths.append(path)


async def scan_path_info(handle: BinaryIO, cutoff: float) -> StoreScan:
    """Reduce `nix path-info --all --json` without ever holding it.

    **Streamed, not parsed whole.** This loop also serves the CSI server, the
    NRI plugin and the heartbeat `nri-wait` gives up on. Measured on a store of
    216,455 paths, whose metadata is 282 MB of JSON on one line:

        json.loads on the loop      worst loop gap 1.374s
        json.loads in a thread      worst loop gap 0.841s
        this, checkpointing         worst loop gap 0.057s

    A thread only recovers the read: the C json decoder holds the GIL for the
    parse. `ijson` hands back one path at a time, so the checkpoint below has
    somewhere to go -- and neither the 282 MB string nor the dict it parses
    into is ever held.

    `--json-format 2` is not a way out. It is also one line, and it keys on the
    base name instead of the path.
    """
    scan = StoreScan()
    for path, facts in ijson.kvitems(handle, "", use_float=True):
        _scan_entry(scan, path, facts, cutoff)
        if scan.paths % _SCAN_YIELD_EVERY == 0:
            await checkpoint()
    return scan


async def _read_store_scan(work: Path, cutoff: float) -> StoreScan:
    """Ask nix for the store's metadata, and reduce it as it is read.

    **To a file, and not to a pipe.** The kernel gives the process one file
    descriptor either way, so nothing in between has to hold the line.
    """
    out = work / "path-info.json"
    with anyio.fail_after(GC_PATH_INFO_TIMEOUT_SECONDS):
        with out.open("wb") as handle:
            await anyio.run_process(
                [
                    "nix",
                    "path-info",
                    "--store",
                    "local",
                    "--all",
                    "--json",
                    "--json-format",
                    "1",
                ],
                stdout=handle,
            )
    with out.open("rb") as read_handle:
        return await scan_path_info(read_handle, cutoff)


def _record_store_size(scan: StoreScan) -> None:
    """How big this node's store is, from the answer the cycle already has.

    There is no way to see a node's store grow from outside it, which is how
    issue #38 stayed invisible for 33 hours: the file system alert fired
    against `/var`, and nothing said which share was the store. Issue #39.

    The cost is nothing. `nix path-info --all --json` carries `narSize` beside
    the `registrationTime` the cycle reads, so the scan sums it on the way
    past. A `du`-style walk over a multi-GB store is what this avoids.

    `narSize` over-counts. See `STORE_NAR_BYTES` for the measurement.
    """
    STORE_PATHS.set(scan.paths)
    STORE_NAR_BYTES.set(scan.nar_bytes)


def _count_deleted(lines: list[str]) -> int:
    """How many paths Nix reported deleting, and a raise for anything else.

    Nix decides which of the offered paths are safe. It deletes those, refuses
    the rest with `_STILL_ALIVE`, and **exits 1 whenever it refused any**.
    Measured on nix 2.34.8: a mixed list exits 1 with the dead path gone, an
    all-live list exits 1 with nothing gone, an all-dead list exits 0.

    So a liveness error is not a failure. Any other `error:` is.

    The count is what Nix reported, not the length of the offered list. Those
    are different numbers on every cycle that offers a live path.
    """
    errors = [line for line in lines if line.startswith("error:")]
    unexpected = [line for line in errors if _STILL_ALIVE not in line]
    if unexpected:
        raise RuntimeError(f"nix store delete failed: {unexpected[0]}")
    if errors:
        logger.debug("gc_paths_still_alive", count=len(errors))
    return sum(1 for line in lines if line.startswith("deleting '"))


async def _delete(work: Path, old_paths: list[str]) -> int:
    """Offer the old paths to Nix, and answer how many it took."""
    # The trailing newline is deliberate: `"\n".join(...)` leaves the last path
    # without one, and `--stdin` reads one path per line.
    offered = ("\n".join(old_paths) + "\n").encode()

    out = work / "delete.log"
    with anyio.fail_after(GC_DELETE_TIMEOUT_SECONDS):
        with out.open("wb") as handle:
            completed = await anyio.run_process(
                ["nix", "store", "delete", "--store", "local", "--stdin"],
                input=offered,
                stdout=handle,
                stderr=subprocess.STDOUT,
                check=False,
            )

    # 1 is the normal outcome, not a failure: Nix exits 1 whenever it refused
    # any offered path, and most of them are still in use. `_count_deleted`
    # reads the log and decides which refusals are ordinary. Anything past 1
    # never reached that log.
    if completed.returncode not in (0, 1):
        raise RuntimeError(f"nix store delete exited {completed.returncode}")

    return _count_deleted(out.read_text().splitlines())


async def _run_gc_cycle() -> None:
    """Execute one GC cycle: optionally copy to cache, then delete old paths."""
    if PYNIXD_ENABLED:
        try:
            await copy_to_cache(None)
        except Exception:
            logger.warning("gc_cache_copy_failed", exc_info=True)

    with tempfile.TemporaryDirectory(prefix="nixkube-gc-") as tmp:
        work = Path(tmp)

        try:
            scan = await _read_store_scan(work, time.time() - GC_KEEP_SECONDS)
        except ijson.JSONError:
            logger.warning("gc_path_info_parse_error", exc_info=True)
            return

        _record_store_size(scan)

        if not scan.old_paths:
            logger.debug("gc_nothing_to_delete", known=scan.paths)
            return

        logger.info("gc_deleting_paths", count=len(scan.old_paths))
        deleted = await _delete(work, scan.old_paths)

    GC_PATHS_DELETED.inc(deleted)
    logger.info("gc_done", offered=len(scan.old_paths), deleted=deleted)
