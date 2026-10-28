# SPDX-License-Identifier: Apache-2.0
"""NRI plugin base class: handles protocol boilerplate so subclasses focus on business logic.

NRI Event Subscription Bitmask Encoding:
  Event enum values use 1-based indexing for bit positions: bit = (event_value - 1)
  - Event 0 (UNKNOWN)  → bit -1 (invalid, not used)
  - Event 1 (RUN_POD_SANDBOX) → bit 0
  - Event 4 (CREATE_CONTAINER) → bit 3: (1 << 3) = 8
  - Event 11 (REMOVE_CONTAINER) → bit 10: (1 << 10) = 1024
"""

from __future__ import annotations

import abc
from collections.abc import Sequence
from typing import TYPE_CHECKING

import structlog
from nri import nri_grpc, nri_pb2

if TYPE_CHECKING:
    from grpclib.server import Stream


def make_event_bitmask(events: Sequence[int]) -> int:
    """Convert a list of nri_pb2.Event values to a subscription bitmask."""
    return sum(1 << (event - 1) for event in events)


class NriPlugin(nri_grpc.PluginBase):
    """NRI plugin base: implements protocol boilerplate, leaving business logic to subclasses.

    Handles Configure (event subscription), Synchronize, Shutdown, and all mandatory
    stub handlers. Subclasses only need to implement CreateContainer and StateChange.

    Args:
        subscribed_events: NRI events to subscribe to (nri_pb2.Event values).
    """

    def __init__(self, subscribed_events: Sequence[int]) -> None:
        super().__init__()
        self._event_mask = make_event_bitmask(subscribed_events)

    async def Configure(self, stream: Stream[nri_pb2.ConfigureRequest, nri_pb2.ConfigureResponse]) -> None:
        logger = structlog.get_logger("grpclib_nri.configure")
        req: nri_pb2.ConfigureRequest | None = await stream.recv_message()
        runtime_name = req.runtime_name if req else None
        runtime_version = req.runtime_version if req else None
        logger.debug("configure", runtime=runtime_name, version=runtime_version)
        await stream.send_message(nri_pb2.ConfigureResponse(events=self._event_mask))

    async def Synchronize(self, stream: Stream[nri_pb2.SynchronizeRequest, nri_pb2.SynchronizeResponse]) -> None:
        logger = structlog.get_logger("grpclib_nri.synchronize")
        req: nri_pb2.SynchronizeRequest | None = await stream.recv_message()
        pods = len(req.pods) if req else 0
        containers = len(req.containers) if req else 0
        logger.debug("synchronize", pods=pods, containers=containers)
        await stream.send_message(nri_pb2.SynchronizeResponse())

    async def Shutdown(self, stream: Stream[nri_pb2.Empty, nri_pb2.Empty]) -> None:
        logger = structlog.get_logger("grpclib_nri.shutdown")
        await stream.recv_message()
        logger.debug("shutdown")
        await stream.send_message(nri_pb2.Empty())

    async def UpdateContainer(
        self, stream: Stream[nri_pb2.UpdateContainerRequest, nri_pb2.UpdateContainerResponse]
    ) -> None:
        await stream.recv_message()
        await stream.send_message(nri_pb2.UpdateContainerResponse())

    async def StopContainer(self, stream: Stream[nri_pb2.StopContainerRequest, nri_pb2.StopContainerResponse]) -> None:
        await stream.recv_message()
        await stream.send_message(nri_pb2.StopContainerResponse())

    async def UpdatePodSandbox(
        self, stream: Stream[nri_pb2.UpdatePodSandboxRequest, nri_pb2.UpdatePodSandboxResponse]
    ) -> None:
        await stream.recv_message()
        await stream.send_message(nri_pb2.UpdatePodSandboxResponse())

    async def ValidateContainerAdjustment(
        self,
        stream: Stream[nri_pb2.ValidateContainerAdjustmentRequest, nri_pb2.ValidateContainerAdjustmentResponse],
    ) -> None:
        await stream.recv_message()
        await stream.send_message(nri_pb2.ValidateContainerAdjustmentResponse())

    @abc.abstractmethod
    async def CreateContainer(
        self, stream: Stream[nri_pb2.CreateContainerRequest, nri_pb2.CreateContainerResponse]
    ) -> None: ...

    @abc.abstractmethod
    async def StateChange(self, stream: Stream[nri_pb2.StateChangeEvent, nri_pb2.Empty]) -> None: ...
