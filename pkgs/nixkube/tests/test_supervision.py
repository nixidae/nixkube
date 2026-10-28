# SPDX-License-Identifier: Apache-2.0
"""`detach`, which is what stops one container's failure ending the node's.

A task group cancels every sibling when a child raises. The groups these
children run in hold the CSI and NRI servers, so a build that fails on its own
container would take the server down, `supervised` would count a restart, and
five of them inside the window is `CrashLoopError` on a node that is working.

Nothing else catches this: a failing build is not a failing node, and no
existing test starts one in a group.
"""

from __future__ import annotations

import anyio
import anyio.lowlevel
import pytest
from structlog.testing import capture_logs

from src.supervision import detach


async def _raises() -> None:
    raise RuntimeError("one container's build")


async def _records(seen: list[str]) -> None:
    seen.append("ran")


@pytest.mark.asyncio
class TestDetach:
    async def test_a_child_that_raises_does_not_take_the_group_down(self) -> None:
        after: list[str] = []

        async with anyio.create_task_group() as tg:
            detach(tg, _raises, name="probe")
            await anyio.lowlevel.checkpoint()
            detach(tg, _records, after, name="sibling")

        assert after == ["ran"], "the sibling was cancelled by the failure"

    async def test_the_same_child_started_bare_does_take_it_down(self) -> None:
        """The negative control. Without it, the test above passes just as well
        against a `detach` that never starts anything."""
        with pytest.raises(BaseExceptionGroup) as caught:
            async with anyio.create_task_group() as tg:
                tg.start_soon(_raises)

        assert any(isinstance(e, RuntimeError) for e in caught.value.exceptions)

    async def test_the_failure_is_logged_rather_than_lost(self) -> None:
        """Swallowed and silent are different things. The task name is on the
        line, because a node runs many of these at once.

        `capture_logs`, not `caplog`: these loggers are structlog's, and the
        test session never runs `configure_structlog`, so nothing reaches the
        stdlib handler that `caplog` reads.
        """
        with capture_logs() as logs:
            async with anyio.create_task_group() as tg:
                detach(tg, _raises, name="nri_build")

        failures = [entry for entry in logs if entry["event"] == "background_task_failed"]
        assert len(failures) == 1
        assert failures[0]["task"] == "nri_build"
        assert failures[0]["log_level"] == "error"

    async def test_a_cancellation_is_passed_on(self) -> None:
        """Only `Exception` is kept in. A cancelled child that returned
        normally would leave its group waiting on a task that is already
        gone, and the server would never close."""
        started = anyio.Event()

        async def forever() -> None:
            started.set()
            await anyio.sleep(3600)

        with anyio.fail_after(10):
            async with anyio.create_task_group() as tg:
                detach(tg, forever, name="pump")
                await started.wait()
                tg.cancel_scope.cancel()
