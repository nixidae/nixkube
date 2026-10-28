# SPDX-License-Identifier: Apache-2.0

"""Crash-loop supervision for long-running coroutines, and background work.

Wraps coroutine factories with restart logic and crash-loop detection.
A crash loop is defined as max_restarts failures within a sliding time window.
When detected, CrashLoopError is raised. It leaves the task group that holds
every service, which cancels the siblings and exits the process (Kubernetes
restarts the pod with backoff).
"""

import time
from collections import deque
from collections.abc import Callable, Coroutine
from typing import Any

import anyio
import anyio.abc
import structlog

from .metrics import SERVICE_CRASH_LOOPS, SERVICE_RESTARTS

logger = structlog.get_logger("nixkube.supervision")


def detach(
    tasks: anyio.abc.TaskGroup,
    func: Callable[..., Coroutine[Any, Any, None]],
    *args: Any,
    name: str,
) -> None:
    """Start `func` in `tasks`, and keep its failure inside.

    A task group cancels every sibling when a child raises, and the group
    these run in holds a server. The children are per-container work -- a
    cache copy, a build, a sweep -- and any of them may fail on its own
    container without the node being unwell. Letting one out takes the server
    down, `supervised` counts a restart, and five failed builds inside the
    window is a `CrashLoopError` on a node that is working.

    Started from a request handler, which runs in a task the server owns
    rather than in this group. That is allowed while the group is open. A
    handler that fires after the server has begun to stop gets
    `RuntimeError: This task group is not active`, which reaches the runtime
    as an error it retries.
    """

    async def guarded() -> None:
        try:
            await func(*args)
        except anyio.get_cancelled_exc_class():
            logger.debug("background_task_cancelled", task=name)
            raise
        except Exception:
            logger.exception("background_task_failed", task=name)

    tasks.start_soon(guarded, name=name)


class CrashLoopError(Exception):
    """Raised when a supervised task has crashed too many times within the window."""


class CrashLoopTracker:
    """Tracks restart timestamps and detects crash loops.

    A crash loop is detected when max_restarts restarts occur within window seconds.
    """

    def __init__(self, max_restarts: int = 5, window: float = 60.0, name: str = "") -> None:
        self.max_restarts = max_restarts
        self.window = window
        self.name = name
        self._timestamps: deque[float] = deque()

    def record_and_check(self) -> None:
        """Record a restart and raise CrashLoopError if the crash loop threshold is exceeded."""
        now = time.monotonic()
        self._timestamps.append(now)
        # Prune timestamps outside the window
        cutoff = now - self.window
        while self._timestamps and self._timestamps[0] < cutoff:
            self._timestamps.popleft()
        if len(self._timestamps) >= self.max_restarts:
            raise CrashLoopError(
                f"{self.name!r}: crashed {len(self._timestamps)} times in {self.window:.0f}s (max {self.max_restarts})"
            )


async def supervised(
    factory: Callable[[], Coroutine[Any, Any, None]],
    name: str,
    *,
    max_restarts: int = 5,
    window: float = 60.0,
) -> None:
    """Run a coroutine factory in a supervised restart loop.

    Calls factory() to create a new coroutine each iteration. If the coroutine
    raises or returns, records the failure and restarts after a 1s backoff.
    CancelledError propagates immediately without recording (clean shutdown).
    CrashLoopError propagates when the threshold is exceeded.
    """
    tracker = CrashLoopTracker(max_restarts=max_restarts, window=window, name=name)
    log = logger.bind(service=name)

    while True:
        try:
            await factory()
            # Coroutine returned without raising — treat as unexpected exit
            log.warning("service_exited_unexpectedly")
        except anyio.get_cancelled_exc_class():
            raise
        except CrashLoopError:
            raise
        except Exception:
            log.exception("service_crashed")

        SERVICE_RESTARTS.labels(service=name).inc()
        try:
            tracker.record_and_check()
        except CrashLoopError:
            SERVICE_CRASH_LOOPS.labels(service=name).inc()
            raise
        log.info("service_restarting", backoff_seconds=1)
        await anyio.sleep(1)
