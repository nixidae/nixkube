# SPDX-License-Identifier: Apache-2.0

"""What `_spawn_build_task` leaves behind, however it ends.

`pending_builds` is not bookkeeping. The volume sweep reads it to decide
that a directory is still being filled (issue #64), so an id left in it
pins that container's hardlink farm against both collection rules for the
life of the process.
"""

from collections.abc import Awaitable, Callable
from pathlib import Path

import anyio
import pytest
from cachetools import TTLCache
from kr8s.asyncio.objects import Pod

from src.nri.builds import BuildRegistry
from src.nri.server import NriPlugin
from src.nri.zmq import ZeroMQServer


class _Zmq(ZeroMQServer):
    """The state `_spawn_build_task` writes, without the ZeroMQ sockets.

    `ZeroMQServer.__init__` opens those sockets, so it is not called.
    """

    def __init__(self) -> None:
        self.pending_builds = BuildRegistry()
        self.build_status: TTLCache[str, dict[str, str]] = TTLCache(maxsize=10000, ttl=3600)
        self.failed: list[tuple[str, str]] = []

    async def publish_build_progress(self, container_id: str) -> None:
        pass

    async def publish_build_complete(self, container_id: str) -> None:
        pass

    async def publish_build_failed(self, container_id: str, reason: str) -> None:
        self.failed.append((container_id, reason))


def _plugin(monkeypatch: pytest.MonkeyPatch, zmq: _Zmq, build_and_mount: Callable[..., Awaitable[None]]) -> NriPlugin:
    """A plugin with only what `_spawn_build_task` touches.

    `__new__` and not `__init__`: the real constructor wants a CRI socket,
    a kr8s client and an NRI connection, none of which this path uses.
    The method under test is the real one.
    """
    plugin = NriPlugin.__new__(NriPlugin)
    plugin.zmq_server = zmq
    monkeypatch.setattr(plugin, "_build_and_mount", build_and_mount)
    return plugin


CONTAINER = "ad22c728"
PATHS = {Path("/nix/store/abc-pkg")}
POD = Pod({"metadata": {"name": "app-pod", "namespace": "nixkube"}})


@pytest.mark.asyncio
async def test_a_cancelled_build_does_not_stay_pending(monkeypatch: pytest.MonkeyPatch) -> None:
    """A cancelled task reaches neither the success nor the except path.

    `CancelledError` is a BaseException, so `except Exception` never sees
    it. Only `finally` runs.
    """
    started = anyio.Event()

    async def hang(*_args: object, **_kwargs: object) -> None:
        started.set()
        await anyio.sleep_forever()

    zmq = _Zmq()
    plugin = _plugin(monkeypatch, zmq, hang)
    zmq.pending_builds.add(CONTAINER)

    with anyio.fail_after(10):
        async with anyio.create_task_group() as tg:
            tg.start_soon(plugin._spawn_build_task, CONTAINER, "app", POD, PATHS)  # type: ignore[arg-type]  # zuban-bug: start_soon counts defaulted parameters
            await started.wait()
            tg.cancel_scope.cancel()

    assert CONTAINER not in zmq.pending_builds, "a cancelled build left its container pinned against the volume sweep"


@pytest.mark.asyncio
async def test_a_failed_build_does_not_stay_pending(monkeypatch: pytest.MonkeyPatch) -> None:
    async def refuse(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("no substituter that can build it")

    reported: list[object] = []

    async def report(*_args: object, **kwargs: object) -> None:
        reported.append(kwargs.get("reason"))

    monkeypatch.setattr("src.nri.server.report_event", report)

    zmq = _Zmq()
    plugin = _plugin(monkeypatch, zmq, refuse)
    zmq.pending_builds.add(CONTAINER)

    await plugin._spawn_build_task(CONTAINER, "app", POD, PATHS)

    assert CONTAINER not in zmq.pending_builds
    assert reported == ["BuildFailed"], "a failed build must say so on the pod"
    # And to the hook, which otherwise waits out its timeout and says nothing.
    reason = "no substituter that can build it"
    assert zmq.build_status[CONTAINER] == {
        "status": "failed",
        "reason": reason,
    }
    assert zmq.failed == [(CONTAINER, reason)]
