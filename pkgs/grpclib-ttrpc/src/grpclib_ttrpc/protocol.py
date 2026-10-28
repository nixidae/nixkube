"""ttrpc wire protocol: frame parsing, raw streams, connection management."""

import abc
import asyncio
import struct
from collections.abc import Callable

import structlog
from grpclib.exceptions import ProtocolError

from ._stream_buffer import StreamBuffer

log = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

HEADER_SIZE = 10  # 4 + 4 + 1 + 1 bytes
MAX_PAYLOAD = 4 * 1024 * 1024  # 4 MiB

# Frame message types
MSG_TYPE_REQUEST = 0x1
MSG_TYPE_RESPONSE = 0x2
MSG_TYPE_DATA = 0x3

# Frame flags
FLAG_REMOTE_CLOSED = 0x1
FLAG_REMOTE_OPEN = 0x2
FLAG_NO_DATA = 0x4

_HEADER_FMT = ">IIBB"  # payload_length, stream_id, msg_type, flags


# ---------------------------------------------------------------------------
# TtrpcRawStream
# ---------------------------------------------------------------------------


class TtrpcRawStream:
    """Per-stream object bridging the protocol layer and request handlers.

    The protocol layer writes *into* the stream via :py:meth:`feed_*`.
    The handler layer reads *from* it via :py:meth:`read_payload` and
    writes *to* the peer via :py:meth:`send_frame`.
    """

    def __init__(
        self,
        stream_id: int,
        transport: asyncio.Transport,
    ) -> None:
        self.stream_id = stream_id
        self._transport = transport
        self._buffer = StreamBuffer()

    # --- protocol → handler --------------------------------------------------

    def feed_data(self, data: bytes) -> None:
        self._buffer.feed_data(data)

    def feed_eof(self) -> None:
        self._buffer.feed_eof()

    def feed_error(self, exc: Exception) -> None:
        self._buffer.feed_error(exc)

    async def read_payload(self) -> bytes | None:
        """Await the next raw payload from the client."""
        return await self._buffer.read_message()

    # --- handler → peer -------------------------------------------------------

    def send_frame(self, msg_type: int, flags: int, payload: bytes) -> None:
        """Encode and write one ttrpc frame to the transport.

        Uses ``transport.write()`` directly — ttrpc has no flow control.
        Raises :py:exc:`~grpclib.exceptions.ProtocolError` if the payload
        exceeds :py:data:`MAX_PAYLOAD`.
        """
        length = len(payload)
        if length > MAX_PAYLOAD:
            raise ProtocolError(f"Payload too large: {length} > {MAX_PAYLOAD}")
        header = struct.pack(_HEADER_FMT, length, self.stream_id, msg_type, flags)
        self._transport.write(header + payload)


# ---------------------------------------------------------------------------
# TtrpcConnection
# ---------------------------------------------------------------------------


class TtrpcConnection:
    """Server-side stream-ID bookkeeping.

    ttrpc uses odd stream IDs for client-initiated streams; they must
    strictly increase with each new request.
    """

    def __init__(self) -> None:
        self._last_stream_id: int = 0

    def validate_and_accept(self, stream_id: int) -> bool:
        """Return True if *stream_id* is valid (odd & strictly increasing)."""
        if stream_id % 2 == 0:
            return False
        if stream_id <= self._last_stream_id:
            return False
        self._last_stream_id = stream_id
        return True


# ---------------------------------------------------------------------------
# AbstractTtrpcHandler
# ---------------------------------------------------------------------------


class AbstractTtrpcHandler(abc.ABC):
    """Abstract interface implemented by the server-side handler."""

    @abc.abstractmethod
    def accept(
        self,
        raw_stream: TtrpcRawStream,
        initial_payload: bytes,
        flags: int,
        release: Callable[[], None],
    ) -> None: ...

    @abc.abstractmethod
    def close(self) -> None: ...


# ---------------------------------------------------------------------------
# TtrpcProtocol
# ---------------------------------------------------------------------------


class TtrpcProtocol(asyncio.Protocol):
    """asyncio.Protocol for the ttrpc binary framing protocol.

    Accumulates incoming bytes, parses complete 10-byte-header + payload
    frames, and dispatches to *handler*.
    """

    def __init__(self, handler: AbstractTtrpcHandler) -> None:
        self._handler = handler
        self._buf = bytearray()
        self._transport: asyncio.Transport | None = None
        self._connection: TtrpcConnection | None = None
        self._streams: dict[int, TtrpcRawStream] = {}

    # asyncio.Protocol callbacks -------------------------------------------

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        if not isinstance(transport, asyncio.Transport):
            raise TypeError(f"Expected asyncio.Transport, got {type(transport)}")
        self._transport = transport
        self._connection = TtrpcConnection()
        try:
            peer = transport.get_extra_info("peername")
        # `Exception` already covers the `AttributeError` a transport double
        # raises; naming both said nothing the second one did not.
        except Exception:  # noqa: BLE001 -- the peer name is for a log line, and no transport owes us one
            peer = None
        log.debug("connection_established", peer=peer)

    def data_received(self, data: bytes) -> None:
        log.debug("data_received", bytes=len(data))
        self._buf.extend(data)
        self._try_parse_frames()

    def connection_lost(self, exc: Exception | None) -> None:
        log.debug("connection_lost", exc=repr(exc), active_streams=len(self._streams))
        err: Exception = exc or ConnectionResetError("ttrpc connection closed")
        for raw_stream in list(self._streams.values()):
            raw_stream.feed_error(err)
        self._streams.clear()
        self._handler.close()

    # Internal frame parsing -----------------------------------------------

    def _try_parse_frames(self) -> None:
        while len(self._buf) >= HEADER_SIZE:
            length, stream_id, msg_type, flags = struct.unpack_from(_HEADER_FMT, self._buf, 0)
            if length > MAX_PAYLOAD:
                log.warning("payload_too_large", bytes=length)
                if self._transport:
                    self._transport.close()
                return

            total = HEADER_SIZE + length
            if len(self._buf) < total:
                break  # wait for more data

            payload = bytes(self._buf[HEADER_SIZE:total])
            del self._buf[:total]
            self._dispatch_frame(stream_id, msg_type, flags, payload)

    def _dispatch_frame(
        self,
        stream_id: int,
        msg_type: int,
        flags: int,
        payload: bytes,
    ) -> None:
        if self._transport is None or self._connection is None:
            raise RuntimeError("Transport or connection not initialized")

        if msg_type == MSG_TYPE_REQUEST:
            if not self._connection.validate_and_accept(stream_id):
                log.warning("invalid_stream_id", stream_id=stream_id)
                return

            raw_stream = TtrpcRawStream(stream_id, self._transport)
            self._streams[stream_id] = raw_stream

            def release(sid: int = stream_id) -> None:
                self._streams.pop(sid, None)

            # accept() is responsible for feeding the initial payload into the
            # stream buffer (synchronously, before this method returns).
            # We then call feed_eof() unless FLAG_REMOTE_OPEN is set, because:
            #   flags=0 (unary): one request, no more DATA frames from client
            #   FLAG_REMOTE_CLOSED (0x01): client done sending, no more DATA frames
            #   FLAG_REMOTE_OPEN  (0x02): client-streaming, more DATA frames follow
            self._handler.accept(raw_stream, payload, flags, release)
            if not (flags & FLAG_REMOTE_OPEN):
                raw_stream.feed_eof()

        elif msg_type == MSG_TYPE_DATA:
            data_stream = self._streams.get(stream_id)
            if data_stream is None:
                log.warning("data_frame_unknown_stream", stream_id=stream_id)
                return
            if flags & FLAG_REMOTE_CLOSED:
                if payload and not (flags & FLAG_NO_DATA):
                    data_stream.feed_data(payload)
                data_stream.feed_eof()
            else:
                data_stream.feed_data(payload)

        else:
            log.warning("unknown_message_type", msg_type=hex(msg_type))
