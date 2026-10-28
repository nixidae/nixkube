# SPDX-License-Identifier: Apache-2.0

"""Synchronize sweeps: a container gone while the runtime was down sends no
REMOVE_CONTAINER."""

from pathlib import Path

import anyio
import pytest
from grpclib.server import Stream

from nri import nri_pb2
from src.nri import server
from src.nri.builds import BuildRegistry
from src.nri.zmq import ZeroMQServer


class _Stream(Stream[nri_pb2.SynchronizeRequest, nri_pb2.SynchronizeResponse]):
    """`Stream.__init__` needs a live HTTP/2 stream, so it is not called."""

    def __init__(self, request: nri_pb2.SynchronizeRequest) -> None:
        self.request = request
        self.sent: list[nri_pb2.SynchronizeResponse] = []

    async def recv_message(self) -> nri_pb2.SynchronizeRequest:
        return self.request

    async def send_message(self, message: nri_pb2.SynchronizeResponse) -> None:
        self.sent.append(message)


class _Zmq(ZeroMQServer):
    """Only the registry `Synchronize` hands to the sweep."""

    def __init__(self) -> None:
        self.pending_builds = BuildRegistry()


Sweep = tuple[tuple[object, ...], dict[str, object]]


@pytest.fixture
def sweeps(monkeypatch: pytest.MonkeyPatch) -> list[Sweep]:
    calls: list[Sweep] = []
    monkeypatch.setattr(
        server,
        "schedule_garbage_collection",
        lambda *args, **kwargs: calls.append((args, kwargs)),
    )
    return calls


def _plugin(tasks: anyio.abc.TaskGroup) -> server.NriPlugin:
    return server.NriPlugin(_Zmq(), Path("/cri.sock"), tasks=tasks)


@pytest.mark.asyncio
async def test_the_last_part_sweeps(sweeps: list[Sweep]) -> None:
    stream = _Stream(nri_pb2.SynchronizeRequest(more=False))
    async with anyio.create_task_group() as tg:
        await _plugin(tg).Synchronize(stream)
    assert isinstance(stream.sent[0], nri_pb2.SynchronizeResponse)
    assert len(sweeps) == 1


@pytest.mark.asyncio
async def test_an_earlier_part_does_not(sweeps: list[Sweep]) -> None:
    stream = _Stream(nri_pb2.SynchronizeRequest(more=True))
    async with anyio.create_task_group() as tg:
        await _plugin(tg).Synchronize(stream)
    assert isinstance(stream.sent[0], nri_pb2.SynchronizeResponse)
    assert sweeps == []
