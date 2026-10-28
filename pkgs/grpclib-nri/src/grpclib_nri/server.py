# SPDX-License-Identifier: Apache-2.0
"""NRI server: async context for running a plugin with automatic registration and reconnection."""

import asyncio
import contextlib
import struct
from pathlib import Path

import structlog
from grpclib.const import Status
from grpclib.encoding.proto import ProtoCodec
from grpclib.exceptions import GRPCError, ProtocolError
from grpclib_ttrpc.protocol import (
    HEADER_SIZE,
    MAX_PAYLOAD,
    MSG_TYPE_REQUEST,
    MSG_TYPE_RESPONSE,
    TtrpcProtocol,
)
from grpclib_ttrpc.server import TtrpcHandler
from nri import nri_grpc, nri_pb2
from ttrpc.ttrpc_pb2 import Request, Response

from .mux import PLUGIN_SERVICE_CONN, RUNTIME_SERVICE_CONN, NriMux


async def _serve_plugin_channel(
    mux: NriMux,
    protocol: TtrpcProtocol,
) -> None:
    """Feed mux chunks for ConnID=1 into the ttrpc protocol until EOF."""
    try:
        while True:
            chunk = await mux.read_channel(PLUGIN_SERVICE_CONN)
            if chunk is None:
                protocol.connection_lost(None)
                return
            protocol.data_received(chunk)
    except Exception as exc:  # noqa: BLE001 -- whatever went wrong, the protocol has to be told the connection ended
        protocol.connection_lost(exc)


async def _register_plugin(
    mux: NriMux,
    codec: ProtoCodec,
    plugin_name: str,
    plugin_idx: str,
    *,
    timeout: float = 5.0,
) -> None:
    """Send RegisterPlugin on ConnID=2 and wait for the response.

    Wire format (ConnID=2 channel):
        mux header:   [conn_id=2: uint32 BE][length: uint32 BE]
        ttrpc header: [payload_len: uint32 BE][stream_id=1: uint32 BE]
                      [msg_type=REQUEST=0x01: uint8][flags=0x00: uint8]
        ttrpc payload: ttrpc.Request{service, method, payload, timeout_nano}
            where payload = RegisterPluginRequest{plugin_name, plugin_idx}
    """
    logger = structlog.get_logger("grpclib_nri.registerplugin")
    rpr = nri_pb2.RegisterPluginRequest(
        plugin_name=plugin_name,
        plugin_idx=plugin_idx,
    )
    inner_payload = codec.encode(rpr, nri_pb2.RegisterPluginRequest)

    req = Request(
        service="nri.pkg.api.v1alpha1.Runtime",
        method="RegisterPlugin",
        payload=inner_payload,
        timeout_nano=int(timeout * 1e9),
    )
    req_bytes = req.SerializeToString()

    ttrpc_hdr = struct.pack(">IIBB", len(req_bytes), 1, MSG_TYPE_REQUEST, 0)
    ttrpc_frame = ttrpc_hdr + req_bytes
    mux_hdr = struct.pack(">II", RUNTIME_SERVICE_CONN, len(ttrpc_frame))

    logger.debug(
        "sending_register_plugin_frame",
        bytes=len(ttrpc_frame),
        conn_id=RUNTIME_SERVICE_CONN,
    )
    mux.writer.write(mux_hdr + ttrpc_frame)
    await mux.writer.drain()

    # Accumulate mux chunks for ConnID=2 until we have a complete ttRPC frame.
    buf = bytearray()
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while True:
        remaining = deadline - loop.time()
        if remaining <= 0:
            raise TimeoutError("Timed out waiting for RegisterPlugin response")
        chunk: bytes | None = await asyncio.wait_for(mux.read_channel(RUNTIME_SERVICE_CONN), timeout=remaining)
        if chunk is None:
            raise ProtocolError("Connection closed waiting for RegisterPlugin response")
        buf.extend(chunk)
        if len(buf) < HEADER_SIZE:
            continue
        payload_len, _, msg_type, _ = struct.unpack_from(">IIBB", buf)
        if payload_len > MAX_PAYLOAD:
            raise ProtocolError(f"RegisterPlugin response payload too large: {payload_len}")
        if len(buf) < HEADER_SIZE + payload_len:
            continue  # wait for more chunks
        if msg_type != MSG_TYPE_RESPONSE:
            raise ProtocolError(f"Expected RESPONSE (0x{MSG_TYPE_RESPONSE:02x}), got 0x{msg_type:02x}")
        resp_bytes = bytes(buf[HEADER_SIZE : HEADER_SIZE + payload_len])
        resp = Response.FromString(resp_bytes)
        if resp.status.code != 0:
            raise GRPCError(Status(resp.status.code), resp.status.message or None)
        logger.debug("register_plugin_ok")
        return


class NriServer:
    """NRI server: handles mux setup, registration, and plugin channel serving.

    Provides an API similar to grpclib.Server but tailored for NRI:
    - One plugin per socket connection
    - Automatic reconnection with exponential backoff
    - Built-in registration handshake
    """

    def __init__(
        self,
        plugin: nri_grpc.PluginBase,
        socket_path: Path,
        plugin_name: str = "nixkube",
        plugin_idx: str = "1",
    ):
        """Initialize NRI server.

        Args:
            plugin: NRI plugin instance (must inherit from nri_grpc.PluginBase)
            socket_path: Path to NRI socket (e.g., /var/run/nri/nri.sock)
            plugin_name: Name to register with NRI runtime (default: "nixkube")
            plugin_idx: Plugin index for registration as string (default: "1")
        """
        self.plugin = plugin
        self.socket_path = socket_path
        self.plugin_name = plugin_name
        self.plugin_idx = plugin_idx
        self.codec = ProtoCodec()

        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._mux: NriMux | None = None
        self._handler: TtrpcHandler | None = None
        self._protocol: TtrpcProtocol | None = None

        self._read_task: asyncio.Task | None = None
        self._serve_task: asyncio.Task | None = None
        self._is_closed = False

    async def start(self) -> None:
        """Connect to NRI socket, register plugin, and start serving.

        Handles reconnection with exponential backoff on failure.
        This method blocks until the connection is closed.
        """
        logger = structlog.get_logger("grpclib_nri.server")
        delay = 1.0
        while not self._is_closed:
            try:
                await self._run()
            except Exception as e:  # noqa: BLE001 -- reconnect loop: any failure is retried with backoff
                if self._is_closed:
                    break
                logger.warning(
                    "connection_failed",
                    error_type=type(e).__name__,
                    error=str(e),
                    retry_delay=round(delay),
                )
                await asyncio.sleep(delay)
                delay = min(delay * 2, 30.0)
            else:
                # Clean disconnect: brief pause so containerd processes the old
                # connection's close before we re-register the same plugin identity.
                if not self._is_closed:
                    await asyncio.sleep(1.0)
                    delay = 1.0

    async def wait_closed(self) -> None:
        """Wait until the server is closed."""
        # If we haven't started yet, this will wait for start() to finish
        while not self._is_closed:
            await asyncio.sleep(0.1)

    async def close(self) -> None:
        """Gracefully close the connection and cancel all tasks."""
        logger = structlog.get_logger("grpclib_nri.server")
        self._is_closed = True

        # Cancel tasks
        if self._read_task:
            self._read_task.cancel()
        if self._serve_task:
            self._serve_task.cancel()

        # Wait for tasks to finish
        tasks_to_wait = [t for t in [self._read_task, self._serve_task] if t]
        if tasks_to_wait:
            await asyncio.gather(*tasks_to_wait, return_exceptions=True)

        # Clean up protocol
        if self._protocol:
            self._protocol.connection_lost(None)

        # Close writer
        if self._writer:
            self._writer.close()
            # Already closing; a failure to confirm it changes nothing here.
            with contextlib.suppress(Exception):
                await self._writer.wait_closed()

        logger.debug("server_closed")

    async def _run(self) -> None:
        """Run one connection cycle: connect, register, serve until disconnect."""
        logger = structlog.get_logger("grpclib_nri.server.run")

        logger.info("connecting", socket=str(self.socket_path))
        self._reader, self._writer = await asyncio.wait_for(
            asyncio.open_unix_connection(str(self.socket_path)), timeout=5.0
        )

        mux = NriMux(self._reader, self._writer)
        self._mux = mux
        mapping = self.plugin.__mapping__()
        self._handler = TtrpcHandler(mapping, self.codec)
        self._protocol = TtrpcProtocol(self._handler)
        self._protocol.connection_made(mux.channel_transport(PLUGIN_SERVICE_CONN))

        loop = asyncio.get_running_loop()
        self._read_task = loop.create_task(mux.read_loop())
        self._serve_task = loop.create_task(_serve_plugin_channel(mux, self._protocol))

        # Register with NRI runtime
        await _register_plugin(
            mux,
            self.codec,
            self.plugin_name,
            self.plugin_idx,
        )
        logger.info("plugin_registered", name=self.plugin_name, idx=self.plugin_idx)

        # Block until the connection drops (read_loop exits → serve_task exits)
        await asyncio.gather(self._read_task, self._serve_task)
