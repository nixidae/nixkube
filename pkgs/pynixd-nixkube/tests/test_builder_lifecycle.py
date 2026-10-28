# SPDX-License-Identifier: Apache-2.0
"""`BuilderManager.running()`, which owns the five loops and the probes.

Two things this states that nothing else does. The block ends the loops -- the
old `stop()` cancelled one task and the five were inside it, so a loop that
outlived the block would have been invisible. And a probe that raises does not
take the manager down: probes are per-node work, and the cluster has many
nodes.

The API calls are patched out. This is about lifetime, not about Kubernetes.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

import anyio
import anyio.lowlevel
import pytest
from structlog.testing import capture_logs

from pynixd_nixkube.builder_manager import BuilderManager

if TYPE_CHECKING:
    from pynixd.instance import Server


def manager() -> BuilderManager:
    # These tests never reach the server, so none is given.
    return BuilderManager(server=cast("Server", None), namespace="nixkube")


@pytest.fixture
def quiet(monkeypatch: pytest.MonkeyPatch) -> None:
    """Replace the loops and the two startup calls, so nothing needs a cluster."""

    async def nothing(*_args: object, **_kwargs: object) -> None:
        return None

    async def forever(*_args: object, **_kwargs: object) -> None:
        await anyio.sleep(3600)

    for name in ("_reap_orphaned_builder_pods", "_sync_dynamic_features"):
        monkeypatch.setattr(BuilderManager, name, nothing)
    for name in (
        "_watch_jobs",
        "_watch_queue",
        "_reap_idle",
        "_watch_nodes",
        "_periodic_reconcile",
    ):
        monkeypatch.setattr(BuilderManager, name, forever)


@pytest.mark.asyncio
class TestRunning:
    async def test_the_block_ends_the_loops(self, quiet: None) -> None:
        """A group that did not cancel would hang here: all five loops sleep
        for an hour. `fail_after` is what turns that hang into a failure."""
        m = manager()

        with anyio.fail_after(10):
            async with m.running() as running:
                assert running is m
                assert m._tasks is not None

        assert m._tasks is None

    async def test_a_detached_failure_does_not_end_the_manager(self, quiet: None) -> None:
        async def boom() -> None:
            raise RuntimeError("one node's probe")

        with anyio.fail_after(10):
            async with manager().running() as m:
                with capture_logs() as logs:
                    m._detach(boom, name="probe:node-1")
                    await anyio.lowlevel.checkpoint()
                    await anyio.lowlevel.checkpoint()

        failed = [e for e in logs if e["event"] == "builder_task_failed"]
        assert len(failed) == 1
        assert failed[0]["task"] == "probe:node-1"

    async def test_the_same_failure_started_bare_does_end_it(self, quiet: None) -> None:
        """The negative control. Without it the test above passes against a
        `_detach` that starts nothing at all."""

        async def boom() -> None:
            raise RuntimeError("one node's probe")

        with pytest.raises(BaseExceptionGroup) as caught:
            with anyio.fail_after(10):
                async with manager().running() as m:
                    assert m._tasks is not None
                    m._tasks.start_soon(boom)
                    await anyio.lowlevel.checkpoint()
                    await anyio.lowlevel.checkpoint()

        assert any(isinstance(e, RuntimeError) for e in caught.value.exceptions)

    async def test_detaching_before_the_block_says_so(self, quiet: None) -> None:
        """Rather than `AttributeError` on a `None` group, or silence."""
        m = manager()

        async def boom() -> None:
            raise RuntimeError("never runs")

        with capture_logs() as logs:
            m._detach(boom, name="probe:node-1")

        assert [e["event"] for e in logs] == ["builder_task_not_started"]
