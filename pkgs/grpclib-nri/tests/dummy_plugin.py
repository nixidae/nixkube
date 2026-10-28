# SPDX-License-Identifier: Apache-2.0
"""Minimal NRI plugin for testing the NRI protocol."""

from __future__ import annotations

from typing import TYPE_CHECKING

import structlog
from nri import nri_grpc, nri_pb2

if TYPE_CHECKING:
    from grpclib.server import Stream


class DummyPlugin(nri_grpc.PluginBase):
    """Minimal NRI plugin that echoes back responses without doing anything."""

    def __init__(self) -> None:
        super().__init__()
        self.logger = structlog.get_logger("test.dummy_plugin")
        self.configure_called = False
        self.synchronize_called = False
        self.create_container_called = False

    async def Configure(self, stream: Stream[nri_pb2.ConfigureRequest, nri_pb2.ConfigureResponse]) -> None:
        """Handle Configure request: just echo back with no event subscriptions."""
        self.logger.debug("configure_called")
        req = await stream.recv_message()
        assert req is not None
        self.logger.info("configure", runtime=req.runtime_name, version=req.runtime_version)
        self.configure_called = True
        # Subscribe to all events using the same bit position formula as server.py:
        # bit_position = (event_value - 1), so sum for events 1..LAST
        all_events = sum(1 << (event - 1) for event in range(1, nri_pb2.Event.LAST + 1))
        await stream.send_message(nri_pb2.ConfigureResponse(events=all_events))

    async def Synchronize(self, stream: Stream[nri_pb2.SynchronizeRequest, nri_pb2.SynchronizeResponse]) -> None:
        """Handle Synchronize request: just echo back empty response."""
        self.logger.debug("synchronize_called")
        req = await stream.recv_message()
        assert req is not None
        self.logger.info("synchronize", pods=len(req.pods), containers=len(req.containers))
        self.synchronize_called = True
        await stream.send_message(nri_pb2.SynchronizeResponse())

    async def Shutdown(self, stream: Stream[nri_pb2.Empty, nri_pb2.Empty]) -> None:
        """Handle Shutdown request."""
        self.logger.debug("shutdown_called")
        await stream.recv_message()
        await stream.send_message(nri_pb2.Empty())

    async def CreateContainer(
        self, stream: Stream[nri_pb2.CreateContainerRequest, nri_pb2.CreateContainerResponse]
    ) -> None:
        """Handle CreateContainer request: do nothing."""
        self.logger.debug("create_container_called")
        req = await stream.recv_message()
        assert req is not None
        self.logger.info(
            "create_container",
            pod=f"{req.pod.namespace}/{req.pod.name}",
            container=req.container.name,
        )
        self.create_container_called = True
        # Return empty adjustment (no modifications)
        adjust = nri_pb2.ContainerAdjustment()
        await stream.send_message(nri_pb2.CreateContainerResponse(adjust=adjust))

    async def UpdateContainer(
        self, stream: Stream[nri_pb2.UpdateContainerRequest, nri_pb2.UpdateContainerResponse]
    ) -> None:
        """Stub: not used in tests."""
        await stream.recv_message()
        await stream.send_message(nri_pb2.UpdateContainerResponse())

    async def StopContainer(self, stream: Stream[nri_pb2.StopContainerRequest, nri_pb2.StopContainerResponse]) -> None:
        """Stub: not used in tests."""
        await stream.recv_message()
        await stream.send_message(nri_pb2.StopContainerResponse())

    async def UpdatePodSandbox(
        self, stream: Stream[nri_pb2.UpdatePodSandboxRequest, nri_pb2.UpdatePodSandboxResponse]
    ) -> None:
        """Stub: not used in tests."""
        await stream.recv_message()
        await stream.send_message(nri_pb2.UpdatePodSandboxResponse())

    async def StateChange(self, stream: Stream[nri_pb2.StateChangeEvent, nri_pb2.Empty]) -> None:
        """Stub: not used in tests."""
        await stream.recv_message()
        await stream.send_message(nri_pb2.Empty())

    async def ValidateContainerAdjustment(
        self,
        stream: Stream[nri_pb2.ValidateContainerAdjustmentRequest, nri_pb2.ValidateContainerAdjustmentResponse],
    ) -> None:
        """Stub: not used in tests."""
        await stream.recv_message()
        await stream.send_message(nri_pb2.ValidateContainerAdjustmentResponse())
