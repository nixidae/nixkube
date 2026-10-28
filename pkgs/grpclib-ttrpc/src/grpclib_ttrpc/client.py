"""ttrpc client: unary_call() and streaming Client for ttrpc RPC calls."""

import asyncio
import struct
from collections.abc import AsyncIterator
from typing import Any, Self, TypeVar

import structlog
from grpclib.const import Status
from grpclib.encoding.base import CodecBase
from grpclib.encoding.proto import ProtoCodec
from grpclib.exceptions import GRPCError, ProtocolError
from ttrpc.ttrpc_pb2 import Request, Response

from .protocol import (
    FLAG_REMOTE_CLOSED,
    FLAG_REMOTE_OPEN,
    HEADER_SIZE,
    MAX_PAYLOAD,
    MSG_TYPE_DATA,
    MSG_TYPE_REQUEST,
    MSG_TYPE_RESPONSE,
)

log = structlog.get_logger(__name__)

_RequestT = TypeVar("_RequestT")
_ResponseT = TypeVar("_ResponseT")


async def unary_call[T](
    path: str,
    service: str,
    method: str,
    request: object,
    request_type: type,
    response_type: type[T],
    *,
    connect_timeout: float = 5.0,
    response_timeout: float = 30.0,
    codec: CodecBase | None = None,
) -> T:
    """Make a single unary ttrpc RPC call over a Unix socket.

    Connects to *path*, sends one Request frame with stream_id=1,
    reads one Response frame, then closes the connection.

    Per the ttrpc spec, unary Request frames use flags=0.
    FLAG_REMOTE_CLOSED (0x01) is only set on client-streaming requests.

    :param path: Unix socket path.
    :param service: Fully-qualified protobuf service name
                    (e.g. ``"nri.pkg.api.v1alpha1.Runtime"``).
    :param method: RPC method name (e.g. ``"RegisterPlugin"``).
    :param request: Request message instance.
    :param request_type: Protobuf message class used to encode *request*.
    :param response_type: Protobuf message class used to decode the reply.
    :param connect_timeout: Timeout in seconds for establishing the connection.
    :param response_timeout: Timeout in seconds for receiving the response.
                             For calls like RegisterPlugin that trigger
                             server-side back-connections (Configure/Synchronize),
                             this should be generous (default: 30s).
    :param codec: Codec to use; defaults to :class:`ProtoCodec`.
    :raises GRPCError: If the server returns a non-OK status.
    :raises ProtocolError: On unexpected framing.
    """
    if codec is None:
        codec = ProtoCodec()

    log.debug("unary_call_connecting", path=path, service=service, method=method)
    reader, writer = await asyncio.wait_for(asyncio.open_unix_connection(path), timeout=connect_timeout)

    try:
        payload = codec.encode(request, request_type)

        req = Request(
            service=service,
            method=method,
            payload=payload,
            timeout_nano=int(response_timeout * 1e9),
        )
        req_bytes = req.SerializeToString()

        if len(req_bytes) > MAX_PAYLOAD:
            raise ProtocolError(f"Request payload too large: {len(req_bytes)} > {MAX_PAYLOAD}")

        # Per ttRPC PROTOCOL.md: unary Request frames use flags=0.
        # FLAG_REMOTE_CLOSED (0x01) signals streaming (client done sending
        # but expects multiple server responses) — NOT for unary calls.
        header = struct.pack(">IIBB", len(req_bytes), 1, MSG_TYPE_REQUEST, 0)
        log.debug(
            "unary_call_sending",
            bytes=len(req_bytes),
            header_hex=header.hex(),
            payload_hex=req_bytes[:64].hex(),
        )
        writer.write(header + req_bytes)
        await writer.drain()

        # Read response frame header (10 bytes).
        # response_timeout must be long enough for the server to do any
        # back-connects (e.g. containerd RegisterPlugin → Configure/Synchronize).
        log.debug("unary_call_waiting_response", timeout=response_timeout)
        raw_header = await asyncio.wait_for(reader.readexactly(HEADER_SIZE), timeout=response_timeout)
        length, _stream_id, msg_type, resp_flags = struct.unpack(">IIBB", raw_header)
        log.debug(
            "unary_call_response_header",
            length=length,
            stream_id=_stream_id,
            msg_type=hex(msg_type),
            flags=hex(resp_flags),
        )

        if msg_type != MSG_TYPE_RESPONSE:
            raise ProtocolError(f"Expected RESPONSE frame (0x{MSG_TYPE_RESPONSE:02x}), got 0x{msg_type:02x}")

        if length > MAX_PAYLOAD:
            raise ProtocolError(f"Response payload too large: {length}")

        resp_bytes = await asyncio.wait_for(reader.readexactly(length), timeout=response_timeout)
        log.debug(
            "unary_call_response_payload",
            length=length,
            payload_hex=resp_bytes[:64].hex(),
        )
        resp = Response.FromString(resp_bytes)

        if resp.status.code != 0:
            log.debug(
                "unary_call_rpc_error",
                code=resp.status.code,
                message=resp.status.message,
            )
            raise GRPCError(
                Status(resp.status.code),
                resp.status.message or None,
            )

        log.debug("unary_call_success")
        return codec.decode(resp.payload, response_type)

    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except Exception as exc:  # noqa: BLE001 -- already closing; the reason is worth a line, not a raise
            log.debug("close_error", exc=repr(exc))


# ---------------------------------------------------------------------------
# Streaming Client API
# ---------------------------------------------------------------------------


class Client:
    """Persistent ttrpc client for making RPC calls with streaming support.

    Supports all 4 RPC cardinalities: unary, server-streaming,
    client-streaming, and bidirectional streaming.
    """

    def __init__(
        self,
        path: str = "",
        *,
        host: str = "localhost",
        port: int = 9000,
        codec: CodecBase | None = None,
        connect_timeout: float = 5.0,
    ) -> None:
        """Create a new ttrpc Client.

        :param path: Unix socket path (either path or host+port must be provided)
        :param host: Hostname for TCP connection (default: localhost)
        :param port: Port for TCP connection (default: 9000)
        :param codec: Codec to use; defaults to ProtoCodec
        :param connect_timeout: Timeout in seconds for establishing connection
        """
        if not path and not host:
            raise ValueError("Either path (unix socket) or host+port must be provided")

        self.path = path
        self.host = host
        self.port = port
        self.codec = codec or ProtoCodec()
        self.connect_timeout = connect_timeout
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._next_stream_id = 1  # odd = client-initiated

    async def _connect(self) -> None:
        """Establish connection to server."""
        if self._writer is not None:
            return

        if self.path:
            self._reader, self._writer = await asyncio.wait_for(
                asyncio.open_unix_connection(self.path),
                timeout=self.connect_timeout,
            )
            log.debug("connected_unix", path=self.path)
        else:
            self._reader, self._writer = await asyncio.wait_for(
                asyncio.open_connection(self.host, self.port),
                timeout=self.connect_timeout,
            )
            log.debug("connected_tcp", host=self.host, port=self.port)

    async def close(self) -> None:
        """Close the connection."""
        if self._writer:
            self._writer.close()
            try:
                await self._writer.wait_closed()
            except Exception as exc:  # noqa: BLE001 -- already closing; the reason is worth a line, not a raise
                log.debug("close_error", exc=repr(exc))
            self._writer = None
            self._reader = None

    def _get_stream_id(self) -> int:
        """Get next stream ID (client-initiated are odd)."""
        sid = self._next_stream_id
        self._next_stream_id += 2
        return sid

    async def _send_frame(
        self,
        stream_id: int,
        msg_type: int,
        flags: int,
        payload: bytes,
    ) -> None:
        """Send a frame to the server."""
        if not self._writer:
            await self._connect()

        assert self._writer is not None
        header = struct.pack(">IIBB", len(payload), stream_id, msg_type, flags)
        self._writer.write(header + payload)
        await self._writer.drain()

    async def _read_frame(self) -> tuple[int, int, int, bytes]:
        """Read a frame from the server. Returns (stream_id, msg_type, flags, payload)."""
        if not self._reader:
            await self._connect()

        assert self._reader is not None
        header = await self._reader.readexactly(HEADER_SIZE)
        length, stream_id, msg_type, flags = struct.unpack(">IIBB", header)

        if length > MAX_PAYLOAD:
            raise ProtocolError(f"Payload too large: {length}")

        payload = await self._reader.readexactly(length) if length else b""
        return stream_id, msg_type, flags, payload

    async def unary(
        self,
        service: str,
        method: str,
        request: _RequestT,
        request_type: type[_RequestT],
        response_type: type[_ResponseT],
        *,
        timeout: float = 30.0,
    ) -> _ResponseT:
        """Make a unary RPC call.

        :param service: Fully-qualified service name
        :param method: RPC method name
        :param request: Request message instance
        :param request_type: Protobuf class for request
        :param response_type: Protobuf class for response
        :param timeout: Timeout in seconds
        :return: Response message instance
        :raises GRPCError: If server returns an error status
        """
        await self._connect()

        stream_id = self._get_stream_id()
        payload = self.codec.encode(request, request_type)

        req = Request(
            service=service,
            method=method,
            payload=payload,
            timeout_nano=int(timeout * 1e9),
        )
        req_bytes = req.SerializeToString()

        await self._send_frame(stream_id, MSG_TYPE_REQUEST, 0, req_bytes)

        # Read response
        _, msg_type, _, resp_payload = await self._read_frame()

        if msg_type != MSG_TYPE_RESPONSE:
            raise ProtocolError(f"Expected RESPONSE, got {msg_type}")

        resp = Response.FromString(resp_payload)
        if resp.status.code != 0:
            msg = getattr(resp.status, "message", None)
            raise GRPCError(Status(resp.status.code), msg)

        return self.codec.decode(resp.payload, response_type)

    async def server_stream(
        self,
        service: str,
        method: str,
        request: _RequestT,
        request_type: type[_RequestT],
        response_type: type[_ResponseT],
        *,
        timeout: float = 30.0,
    ) -> AsyncIterator[_ResponseT]:
        """Make a server streaming RPC call.

        :param service: Fully-qualified service name
        :param method: RPC method name
        :param request: Request message instance
        :param request_type: Protobuf class for request
        :param response_type: Protobuf class for response
        :param timeout: Timeout in seconds
        :yields: Response messages
        :raises GRPCError: If server returns an error status
        """
        await self._connect()

        stream_id = self._get_stream_id()
        payload = self.codec.encode(request, request_type)

        req = Request(
            service=service,
            method=method,
            payload=payload,
            timeout_nano=int(timeout * 1e9),
        )
        req_bytes = req.SerializeToString()

        # Send request (server-streaming, so client closes immediately)
        await self._send_frame(stream_id, MSG_TYPE_REQUEST, FLAG_REMOTE_CLOSED, req_bytes)

        # Read DATA frames until REMOTE_CLOSED
        while True:
            _, msg_type, flags, resp_payload = await self._read_frame()

            if msg_type == MSG_TYPE_RESPONSE:
                # Error response
                resp = Response.FromString(resp_payload)
                if resp.status.code != 0:
                    msg = getattr(resp.status, "message", None)
                    raise GRPCError(Status(resp.status.code), msg)
                break

            if msg_type != MSG_TYPE_DATA:
                raise ProtocolError(f"Expected DATA, got {msg_type}")

            if resp_payload:
                yield self.codec.decode(resp_payload, response_type)

            if flags & FLAG_REMOTE_CLOSED:
                break

    async def client_stream(
        self,
        service: str,
        method: str,
        response_type: type[_ResponseT],
        *,
        timeout: float = 30.0,
    ) -> "ClientStreamContext":
        """Make a client streaming RPC call.

        :param service: Fully-qualified service name
        :param method: RPC method name
        :param response_type: Protobuf class for response
        :param timeout: Timeout in seconds
        :return: Context manager for the stream
        :raises GRPCError: If server returns an error status
        """
        await self._connect()

        stream_id = self._get_stream_id()
        req = Request(service=service, method=method, timeout_nano=int(timeout * 1e9))
        req_bytes = req.SerializeToString()

        # Send initial request (no payload, FLAG_REMOTE_OPEN means more frames coming)
        await self._send_frame(stream_id, MSG_TYPE_REQUEST, FLAG_REMOTE_OPEN, req_bytes)

        return ClientStreamContext(self, stream_id, response_type)

    async def bidirectional_stream(
        self,
        service: str,
        method: str,
        response_type: type[_ResponseT],
        *,
        timeout: float = 30.0,
    ) -> "BidirectionalStreamContext":
        """Make a bidirectional streaming RPC call.

        :param service: Fully-qualified service name
        :param method: RPC method name
        :param response_type: Protobuf class for response
        :param timeout: Timeout in seconds
        :return: Context manager for the stream
        :raises GRPCError: If server returns an error status
        """
        await self._connect()

        stream_id = self._get_stream_id()
        req = Request(service=service, method=method, timeout_nano=int(timeout * 1e9))
        req_bytes = req.SerializeToString()

        # Send initial request (no payload, FLAG_REMOTE_OPEN means more frames coming)
        await self._send_frame(stream_id, MSG_TYPE_REQUEST, FLAG_REMOTE_OPEN, req_bytes)

        return BidirectionalStreamContext(self, stream_id, response_type)


class ClientStreamContext:
    """Context manager for client streaming RPC."""

    def __init__(
        self,
        client: Client,
        stream_id: int,
        response_type: type[_ResponseT],
    ) -> None:
        self.client = client
        self.stream_id = stream_id
        self.response_type = response_type
        self._closed = False

    async def send(self, message: Any) -> None:
        """Send a message to the server."""
        if self._closed:
            raise RuntimeError("Stream is closed")

        payload = self.client.codec.encode(message, type(message))
        await self.client._send_frame(self.stream_id, MSG_TYPE_DATA, 0, payload)

    async def close(self) -> None:
        """Close the client stream (signal server no more messages)."""
        if not self._closed:
            await self.client._send_frame(self.stream_id, MSG_TYPE_DATA, FLAG_REMOTE_CLOSED, b"")
            self._closed = True

    async def recv(self) -> _ResponseT:
        """Receive the response from the server."""
        if not self._closed:
            await self.close()

        # Read response
        _, msg_type, _, resp_payload = await self.client._read_frame()

        if msg_type != MSG_TYPE_RESPONSE:
            raise ProtocolError(f"Expected RESPONSE, got {msg_type}")

        resp = Response.FromString(resp_payload)
        if resp.status.code != 0:
            msg = getattr(resp.status, "message", None)
            raise GRPCError(Status(resp.status.code), msg)

        return self.client.codec.decode(resp.payload, self.response_type)

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        await self.close()


class BidirectionalStreamContext:
    """Context manager for bidirectional streaming RPC."""

    def __init__(
        self,
        client: Client,
        stream_id: int,
        response_type: type[_ResponseT],
    ) -> None:
        self.client = client
        self.stream_id = stream_id
        self.response_type = response_type
        self._closed = False
        self._reader_task: asyncio.Task | None = None
        self._response_queue: asyncio.Queue = asyncio.Queue()

    async def send(self, message: Any) -> None:
        """Send a message to the server."""
        if self._closed:
            raise RuntimeError("Stream is closed")

        payload = self.client.codec.encode(message, type(message))
        await self.client._send_frame(self.stream_id, MSG_TYPE_DATA, 0, payload)

    async def close(self) -> None:
        """Close the bidirectional stream."""
        if not self._closed:
            await self.client._send_frame(self.stream_id, MSG_TYPE_DATA, FLAG_REMOTE_CLOSED, b"")
            self._closed = True

    async def __aiter__(self) -> AsyncIterator[_ResponseT]:
        """Async iterator for receiving messages."""
        # Start reader task if not already started
        if not self._reader_task:
            self._reader_task = asyncio.create_task(self._read_responses())

        while True:
            try:
                msg = await asyncio.wait_for(self._response_queue.get(), timeout=30.0)
                if msg is None:  # Sentinel value for EOF
                    break
                if isinstance(msg, Exception):
                    raise msg
                yield msg
            except TimeoutError:
                raise ProtocolError("Timeout waiting for response") from None

    async def _read_responses(self) -> None:
        """Background task to read responses from server."""
        try:
            while True:
                _, msg_type, flags, resp_payload = await self.client._read_frame()

                if msg_type == MSG_TYPE_RESPONSE:
                    # Error response
                    resp = Response.FromString(resp_payload)
                    if resp.status.code != 0:
                        msg = getattr(resp.status, "message", None)
                        exc = GRPCError(Status(resp.status.code), msg)
                        await self._response_queue.put(exc)
                    else:
                        await self._response_queue.put(None)
                    break

                if msg_type != MSG_TYPE_DATA:
                    exc = ProtocolError(f"Expected DATA, got {msg_type}")
                    await self._response_queue.put(exc)
                    break

                if resp_payload:
                    msg = self.client.codec.decode(resp_payload, self.response_type)
                    await self._response_queue.put(msg)

                if flags & FLAG_REMOTE_CLOSED:
                    await self._response_queue.put(None)
                    break
        except Exception as exc:  # noqa: BLE001 -- forwarded to the awaiting caller through the queue, not swallowed
            await self._response_queue.put(exc)

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        await self.close()
        # Wait for reader task to finish
        if self._reader_task:
            try:
                await asyncio.wait_for(self._reader_task, timeout=2.0)
            except TimeoutError:
                self._reader_task.cancel()
