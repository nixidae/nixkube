# SPDX-License-Identifier: Apache-2.0

from pathlib import Path

from google.protobuf.wrappers_pb2 import BoolValue
from grpclib import GRPCError
from grpclib.const import Status
from grpclib.server import Stream

from csi import csi_grpc, csi_pb2

from ..constants import CSI_PLUGIN_NAME, CSI_VENDOR_VERSION


class IdentityServicer(csi_grpc.IdentityBase):
    def __init__(self, plugin_name: str = CSI_PLUGIN_NAME) -> None:
        self.plugin_name = plugin_name

    async def GetPluginInfo(self, stream: Stream[csi_pb2.GetPluginInfoRequest, csi_pb2.GetPluginInfoResponse]) -> None:
        request: csi_pb2.GetPluginInfoRequest | None = await stream.recv_message()
        if request is None:
            raise GRPCError(Status.INVALID_ARGUMENT, "Received None request in GetPluginInfo")
        reply = csi_pb2.GetPluginInfoResponse(name=self.plugin_name, vendor_version=CSI_VENDOR_VERSION)
        await stream.send_message(reply)

    async def GetPluginCapabilities(
        self, stream: Stream[csi_pb2.GetPluginCapabilitiesRequest, csi_pb2.GetPluginCapabilitiesResponse]
    ) -> None:
        request: csi_pb2.GetPluginCapabilitiesRequest | None = await stream.recv_message()
        if request is None:
            raise GRPCError(
                Status.INVALID_ARGUMENT,
                "Received None request in GetPluginCapabilities",
            )
        reply = csi_pb2.GetPluginCapabilitiesResponse(
            capabilities=[
                csi_pb2.PluginCapability(
                    service=csi_pb2.PluginCapability.Service(type=csi_pb2.PluginCapability.Service.CONTROLLER_SERVICE)
                ),
            ]
        )
        await stream.send_message(reply)

    async def Probe(self, stream: Stream[csi_pb2.ProbeRequest, csi_pb2.ProbeResponse]) -> None:
        request: csi_pb2.ProbeRequest | None = await stream.recv_message()
        if request is None:
            raise GRPCError(Status.INVALID_ARGUMENT, "Received None request in Probe")

        # Verify /nix/store is accessible - critical for CSI driver operation
        nix_store = Path("/nix/store")
        ready = nix_store.is_dir()

        reply = csi_pb2.ProbeResponse(ready=BoolValue(value=ready))
        await stream.send_message(reply)
