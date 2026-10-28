# SPDX-License-Identifier: Apache-2.0

import json
import time
from collections import defaultdict
from pathlib import Path

import anyio
import anyio.abc
import structlog

from .constants import (
    CACHE_PING_TIMEOUT_SECONDS,
    CACHE_PROBE_INTERVAL_SECONDS,
    CACHE_PROBE_RETRY_SECONDS,
    GC_COPY_TIMEOUT_SECONDS,
    PYNIXD_ENABLED,
)
from .errors import CommandTimeoutError
from .metrics import (
    CACHE_COPIES,
    CACHE_COPY_ATTEMPTS,
    CACHE_COPY_DURATION,
    CACHE_LAST_CHECK,
    CACHE_REACHABLE,
)
from .subprocessing import SubprocessResult, run_captured
from .supervision import detach

logger = structlog.get_logger("nixkube.cache")

# Prevent concurrent cache uploads of the same store paths.
# Uses frozenset of paths as key (all paths in a copy operation are serialized
# together), so only one copy_to_cache() call per unique path set can run at a
# time. `anyio.Lock` refuses a re-acquire by the task that holds it, where a
# semaphore of one deadlocks; nothing here re-enters, so that is a better
# failure and not a behaviour change.
copy_lock: defaultdict[frozenset[Path], anyio.Lock] = defaultdict(anyio.Lock)


def _record(reachable: bool) -> None:
    """Write the answer and the moment it was taken.

    Both, together, at every exit. `CACHE_REACHABLE` alone cannot say
    whether a 0 means "the check failed" or "no check has run", and a node
    that has not built since it started is in the second state. Any branch
    that sets one and not the other puts the gauge back in that hole.
    """
    CACHE_REACHABLE.set(1 if reachable else 0)
    CACHE_LAST_CHECK.set(time.time())


async def check_cache_connectivity() -> bool:
    """Whether pynixd can serve as a substituter right now.

    **Only success is success, and failure is not fatal.** The one caller is
    `get_build_args`, and the only thing this decides is whether to pass
    `--extra-substituters`. So every way of not getting a clean answer -- the
    ping timed out, the name did not resolve, `nix` exited non-zero, the
    output was not the JSON it promised -- is the same answer: no.

    The alternative is what this used to do. `CommandTimeoutError` escaped,
    and because every `NodePublishVolume` calls this, a pynixd that did not
    answer stopped the node mounting any volume at all. Measured in the UML
    test, which runs no CoreDNS: `ssh-ng://nix@pynixd` never resolved, the
    ping ran to its timeout on every mount, and each workload sat in
    ContainerCreating until the test gave up. The visible symptom was
    `NixInternalError: CommandTimeoutError`, which names neither pynixd nor
    the cache.

    Saying no costs that mount its substituter. It does not cost the mount.
    """
    if not PYNIXD_ENABLED:
        # Not reported as unreachable: there is nothing to reach. The series
        # stays at whatever it was, which is 0 from the start.
        return False

    logger.debug("cache_connectivity_check")
    try:
        # `run_captured` owns the deadline, and answers it as
        # `CommandTimeoutError`. A second one around this call would only
        # cancel the first before it can say that.
        result = await run_captured(
            "nix",
            "store",
            "ping",
            "--json",
            "--store",
            "ssh-ng://nix@pynixd",
            timeout=CACHE_PING_TIMEOUT_SECONDS,
        )
    except Exception:
        # Deliberately every exception. This answers a yes/no question about
        # something outside the node, and there is no failure of it that a
        # mount should be made to care about.
        logger.warning("cache_connectivity_failed", exc_info=True)
        _record(reachable=False)
        return False

    if result.returncode != 0:
        logger.warning(
            "cache_connectivity_failed",
            returncode=result.returncode,
            stderr=result.stderr,
        )
        _record(reachable=False)
        return False

    try:
        ping_data = json.loads(result.stdout)
    except json.JSONDecodeError:
        # `nix store ping --json` that answers with something else is a nix
        # this code does not understand, not a cache that works.
        logger.warning("cache_connectivity_unparsable", stdout=result.stdout[:200])
        _record(reachable=False)
        return False

    logger.debug("cache_connectivity_ok", **ping_data)
    _record(reachable=True)
    return True


def probe_delay(failures: int) -> float:
    """Seconds until the next ping, after `failures` failed pings in a row."""
    if failures == 0:
        return CACHE_PROBE_INTERVAL_SECONDS
    return min(CACHE_PROBE_RETRY_SECONDS * 2.0 ** (failures - 1), CACHE_PROBE_INTERVAL_SECONDS)


async def cache_probe_loop() -> None:
    """Keep `CACHE_REACHABLE` current, whether or not the node builds.

    A mount pings once and does not retry, so one ping that fails during a
    pynixd restart used to hold the gauge at 0 until the node built again.
    Measured on nixlab2 (#72): the ping failed 11 s before pynixd was Ready,
    the node built next 10 h later, and the unreachable alert fired for the
    whole 10 h. This loop pings at startup, retries a failure with backoff,
    and pings on a schedule after that.
    """
    if not PYNIXD_ENABLED:
        return
    failures = 0
    while True:
        failures = 0 if await check_cache_connectivity() else failures + 1
        await anyio.sleep(probe_delay(failures))


def get_substituter_args() -> list[str]:
    """Get nix command arguments for using the cache as a substituter."""
    return [
        "--extra-substituters",
        "ssh-ng://nix@pynixd?trusted=1&priority=20",
    ]


async def copy_to_cache(package_paths: set[Path] | None) -> None:
    """
    Copy packages and their closures to the cache.

    If package_paths is None, copies all paths in the local store (used by GC).

    TODO: Rewrite this entire copy process to support user-supplied copy scripts.
    This will allow end-users to copy to arbitrary destinations (S3, GCS, custom caches, etc.)
    rather than hard-coding ssh-ng://nix@nix-cache.

    TODO: Building should be moved to separate builder pods rather than happening
    within the CSI daemonset. The daemonset should only handle mounting pre-built paths.
    This will improve separation of concerns and allow dedicated builder infrastructure.
    """
    if not PYNIXD_ENABLED:
        # There is no cache to copy to. Without this the node signs the
        # closure and then runs `nix copy` six times against a name that
        # does not resolve, sleeping 5, 10, 20, 40 and 60 seconds between
        # the attempts. Measured on a node with pynixd off: 135s of that
        # per container, and the same again on every GC pass.
        logger.debug("copy_to_cache_skipped", reason="pynixd_disabled")
        return

    if package_paths is not None and not package_paths:
        logger.debug("copy_to_cache_skipped", reason="no_paths")
        return

    lock_key = frozenset(package_paths) if package_paths is not None else frozenset()

    copy_started = time.monotonic()
    async with copy_lock[lock_key]:
        if package_paths is None:
            # All-paths mode: sign and copy everything in the local store.
            path_args: list[str | Path] = ["--all"]
            log = logger.bind(all=True)
        else:
            logger.debug("copy_to_cache_start", count=len(package_paths))
            asked = set(package_paths)
            paths: set[Path] = {Path(p) for p in asked}

            # Run path-info calls concurrently (regular + derivation)
            info: dict[str, SubprocessResult] = {}

            async def _path_info(key: str, *extra: str) -> None:
                info[key] = await run_captured("nix", "path-info", "--recursive", *extra, *asked)

            async with anyio.create_task_group() as tg:
                tg.start_soon(_path_info, "plain")
                tg.start_soon(_path_info, "drv", "--derivation")

            path_info = info["plain"]
            path_info_drv = info["drv"]

            # Get regular closure paths for all packages
            if path_info.returncode == 0:
                paths.update(Path(p) for p in path_info.stdout.splitlines())
            else:
                logger.warning(
                    "path_info_failed",
                    returncode=path_info.returncode,
                    stderr=path_info.stderr,
                )

            # Try to get derivation paths recursively. This may fail if we only have
            # store paths without .drv files (e.g., fetched from substituters), which is normal.
            if path_info_drv.returncode == 0:
                paths.update(Path(p) for p in path_info_drv.stdout.splitlines())
            # Filter out .drv files and deduplicate
            paths = {p for p in paths if p.suffix != ".drv"}
            path_args = list(paths)
            log = logger.bind(count=len(paths))

        sign_result = await run_captured(
            "nix",
            "store",
            "sign",
            "--key-file",
            "/etc/nix-key/nix_ed25519",
            *path_args,
        )
        if sign_result.returncode != 0:
            log.warning(
                "sign_paths_failed",
                returncode=sign_result.returncode,
                stderr=sign_result.stderr,
            )

        for attempt in range(6):
            if attempt > 0:
                exp_backoff = min(5 * (2 ** (attempt - 1)), 60)
                log.warning(
                    "copy_retry",
                    attempt=attempt,
                    max_attempts=6,
                    backoff=exp_backoff,
                )
                await anyio.sleep(exp_backoff)

            # The timeout is what makes the retry loop below reachable. With
            # no deadline, a copy to an unreachable cache never returned, and
            # `gc_loop` calls this first: one stuck copy stopped collection on
            # that node until the pod restarted. Issue #38.
            try:
                nix_copy = await run_captured(
                    "nix",
                    "copy",
                    "--no-check-sigs",
                    "--to",
                    "ssh-ng://nix@pynixd",
                    *path_args,
                    timeout=GC_COPY_TIMEOUT_SECONDS,
                )
            except CommandTimeoutError:
                # It leaves through here rather than round the loop, so
                # without this the copy that issue #38 is about moves none of
                # these three series.
                CACHE_COPY_ATTEMPTS.labels(result="timeout").inc()
                CACHE_COPIES.labels(result="timeout").inc()
                CACHE_COPY_DURATION.observe(time.monotonic() - copy_started)
                raise
            if nix_copy.returncode == 0:
                CACHE_COPY_ATTEMPTS.labels(result="ok").inc()
                CACHE_COPIES.labels(result="ok").inc()
                # A copy that pynixd took is an answer from pynixd. A failed
                # copy says nothing here: it fails for reasons of its own.
                _record(reachable=True)
                log.debug("copy_to_cache_done")
                break
            else:
                CACHE_COPY_ATTEMPTS.labels(result="error").inc()
                log.warning(
                    "copy_attempt_failed",
                    attempt=attempt + 1,
                    max_attempts=6,
                    returncode=nix_copy.returncode,
                    stdout=nix_copy.stdout,
                    stderr=nix_copy.stderr,
                )
        else:
            CACHE_COPIES.labels(result="exhausted").inc()
            log.error("copy_to_cache_exhausted")

        CACHE_COPY_DURATION.observe(time.monotonic() - copy_started)


def schedule_copy_to_cache(tasks: anyio.abc.TaskGroup, package_paths: set[Path]) -> None:
    """Copy in the background, so the caller does not wait for the network.

    `tasks` belongs to the server that is handling the request, so the copy
    dies with that server rather than outliving a restart. A copy that is
    lost that way is not lost for good: the GC cycle offers the whole store
    again on its next pass.
    """
    if not package_paths:
        return
    detach(tasks, copy_to_cache, package_paths, name="copy_to_cache")
