# SPDX-License-Identifier: Apache-2.0

"""Synchronize sweeps: a container gone while the runtime was down sends no
REMOVE_CONTAINER."""

from pathlib import Path
from types import SimpleNamespace

import pytest

from nri import nri_pb2
from src.nri import server


class _Stream:
    def __init__(self, request: nri_pb2.SynchronizeRequest) -> None:
        self.request = request
        self.sent: list = []

    async def recv_message(self):
        return self.request

    async def send_message(self, message) -> None:
        self.sent.append(message)


@pytest.fixture
def sweeps(monkeypatch) -> list:
    calls: list = []
    monkeypatch.setattr(
        server,
        "schedule_garbage_collection",
        lambda *args, **kwargs: calls.append((args, kwargs)),
    )
    return calls


def _plugin() -> server.NriPlugin:
    zmq = SimpleNamespace(pending_builds=object())
    return server.NriPlugin(zmq, Path("/cri.sock"), tasks=object())  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_the_last_part_sweeps(sweeps):
    stream = _Stream(nri_pb2.SynchronizeRequest(more=False))
    await _plugin().Synchronize(stream)
    assert isinstance(stream.sent[0], nri_pb2.SynchronizeResponse)
    assert len(sweeps) == 1


@pytest.mark.asyncio
async def test_an_earlier_part_does_not(sweeps):
    stream = _Stream(nri_pb2.SynchronizeRequest(more=True))
    await _plugin().Synchronize(stream)
    assert isinstance(stream.sent[0], nri_pb2.SynchronizeResponse)
    assert sweeps == []
