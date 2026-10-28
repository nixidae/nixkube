"""ttrpc server: Stream, request_handler, TtrpcHandler, Server."""

import asyncio
import socket
from collections.abc import Callable, Collection
from contextlib import nullcontext
from types import TracebackType
from typing import TYPE_CHECKING, Any, Optional, Self

import structlog
from grpclib._typing import IServable
from grpclib.const import Cardinality, Status
from grpclib.encoding.base import CodecBase
from grpclib.encoding.proto import ProtoCodec
from grpclib.exceptions import GRPCError, ProtocolError, StreamTerminatedError
from grpclib.metadata import Deadline, _Metadata
from grpclib.server import _GC
from grpclib.stream import StreamIterator
from grpclib.utils import DeadlineWrapper, Wrapper
from multidict import MultiDict
from ttrpc.ttrpc_pb2 import Request

from ._messages import build_response
from .protocol import (
    FLAG_NO_DATA,
    FLAG_REMOTE_CLOSED,
    MSG_TYPE_DATA,
    MSG_TYPE_RESPONSE,
    AbstractTtrpcHandler,
    TtrpcProtocol,
    TtrpcRawStream,
)

if TYPE_CHECKING:
    import ssl as _ssl

    from grpclib import const


log = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Metadata helpers
# ---------------------------------------------------------------------------


def _decode_ttrpc_metadata(kv_list: Any) -> _Metadata:
    """Convert a repeated KeyValue list from a ttrpc Request into _Metadata."""
    md: _Metadata = _Metadata(MultiDict())
    for kv in kv_list:
        md.add(kv.key, kv.value)
    return md


# ---------------------------------------------------------------------------
# Stream
# ---------------------------------------------------------------------------


class Stream[RecvT, SendT](StreamIterator[RecvT]):
    """Handler-facing stream for a single ttrpc RPC call.

    The API mirrors :py:class:`grpclib.server.Stream`; most handlers written
    for grpclib's HTTP/2 server work unchanged over ttrpc.
    """

    # state
    _send_message_done: bool = False
    _send_trailing_metadata_done: bool = False

    def __init__(
        self,
        raw_stream: TtrpcRawStream,
        method_name: str,
        cardinality: Cardinality,
        recv_type: type[RecvT],
        send_type: type[SendT],
        *,
        codec: CodecBase,
        deadline: Deadline | None = None,
        metadata: _Metadata | None = None,
    ) -> None:
        self._raw_stream = raw_stream
        self._method_name = method_name
        self._cardinality = cardinality
        self._recv_type = recv_type
        self._send_type = send_type
        self._codec = codec
        self._pending_payload: bytes = b""

        #: :py:class:`~grpclib.metadata.Deadline` of the current request
        self.deadline = deadline
        #: Invocation metadata as a multi-dict object
        self.metadata = metadata

    # --- receiving -----------------------------------------------------------

    async def recv_message(self) -> RecvT | None:
        """Receive the next message from the client."""
        payload = await self._raw_stream.read_payload()
        if payload is None:
            return None
        return self._codec.decode(payload, self._recv_type)

    # --- sending -------------------------------------------------------------

    async def send_initial_metadata(
        self,
        *,
        _metadata: Any = None,
    ) -> None:
        """No-op: ttrpc has no HTTP/2 initial metadata frame."""

    async def send_message(self, message: SendT) -> None:
        """Send one message to the client.

        For unary server responses the encoded bytes are buffered until
        :py:meth:`send_trailing_metadata` sends them together with the status.
        For server-streaming responses each call emits a DATA frame.
        """
        if not self._cardinality.server_streaming and self._send_message_done:
            raise ProtocolError("Message was already sent")

        encoded = self._codec.encode(message, self._send_type)

        if self._cardinality.server_streaming:
            # Each message goes out as a DATA frame immediately.
            self._raw_stream.send_frame(MSG_TYPE_DATA, 0, encoded)
        else:
            # Buffer for unary: sent together with the Response frame.
            self._pending_payload = encoded

        self._send_message_done = True

    async def send_trailing_metadata(
        self,
        *,
        status: Status = Status.OK,
        status_message: str | None = None,
        **_kwargs: Any,
    ) -> None:
        """Send the final frame for this RPC call.

        For unary server responses: emits a ``MSG_TYPE_RESPONSE`` frame.
        For server-streaming responses: emits a final ``MSG_TYPE_DATA`` frame
        with ``FLAG_REMOTE_CLOSED | FLAG_NO_DATA`` on OK status, or a
        ``MSG_TYPE_RESPONSE`` frame with the error status otherwise.
        """
        if self._send_trailing_metadata_done:
            raise ProtocolError("Trailing metadata was already sent")

        if self._cardinality.server_streaming and status is Status.OK:
            # Normal end-of-stream for server streaming.
            self._raw_stream.send_frame(MSG_TYPE_DATA, FLAG_REMOTE_CLOSED | FLAG_NO_DATA, b"")
        else:
            # Unary response OR streaming error: use the Response frame.
            payload = self._pending_payload if status is Status.OK else b""
            response_bytes = build_response(status, status_message, payload)
            self._raw_stream.send_frame(MSG_TYPE_RESPONSE, FLAG_REMOTE_CLOSED, response_bytes)

        self._send_trailing_metadata_done = True

    # --- context manager -----------------------------------------------------

    async def __aenter__(self) -> "Stream[RecvT, SendT]":
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> bool | None:
        if self._send_trailing_metadata_done:
            return True

        if exc_val is not None:
            if isinstance(exc_val, GRPCError):
                status: Status = exc_val.status
                status_message: str | None = exc_val.message
            elif isinstance(exc_val, Exception):
                status = Status.UNKNOWN
                status_message = "Internal Server Error"
            else:
                # propagate non-Exception (e.g. KeyboardInterrupt)
                return None
        elif not self._cardinality.server_streaming and not self._send_message_done:
            status = Status.UNKNOWN
            status_message = "Internal Server Error"
            log.error("handler_no_response", method=self._method_name)
        else:
            status = Status.OK
            status_message = None

        try:
            await self.send_trailing_metadata(status=status, status_message=status_message)
        except Exception:
            log.exception("trailing_metadata_error")

        return True  # suppress the original exception


# ---------------------------------------------------------------------------
# request_handler
# ---------------------------------------------------------------------------


async def request_handler(
    mapping: dict[str, "const.Handler"],
    raw_stream: TtrpcRawStream,
    method_name: str,
    flags: int,
    timeout_nano: int,
    kv_metadata: Any,
    codec: CodecBase,
    release_stream: Callable[[], None],
) -> None:
    """Dispatch one ttrpc request to the appropriate handler method.

    By the time this coroutine runs, the initial payload has already been
    fed into *raw_stream*'s buffer by :py:meth:`TtrpcHandler.accept`.
    """
    try:
        method = mapping.get(method_name)
        if method is None:
            response_bytes = build_response(Status.UNIMPLEMENTED, "Method not found", b"")
            raw_stream.send_frame(MSG_TYPE_RESPONSE, FLAG_REMOTE_CLOSED, response_bytes)
            return

        deadline: Deadline | None = None
        if timeout_nano:
            deadline = Deadline.from_timeout(timeout_nano / 1e9)

        metadata = _decode_ttrpc_metadata(kv_metadata)

        # For non-client-streaming (UNARY_UNARY / UNARY_STREAM), the
        # initial payload was already fed by accept().  If FLAG_REMOTE_CLOSED
        # was set, the protocol also already called feed_eof().  For
        # client-streaming, subsequent DATA frames supply more data.

        async with Stream(
            raw_stream,
            method_name,
            method.cardinality,
            method.request_type,
            method.reply_type,
            codec=codec,
            deadline=deadline,
            metadata=metadata,
        ) as stream:
            wrapper: Any
            deadline_ctx: Any
            if deadline is None:
                wrapper = Wrapper()
                deadline_ctx = nullcontext()
            else:
                wrapper = DeadlineWrapper()
                deadline_ctx = wrapper.start(deadline)

            try:
                with deadline_ctx, wrapper:
                    await method.func(stream)
            except GRPCError:
                raise
            except TimeoutError:
                if wrapper.cancel_failed:
                    log.exception("deadline_cancellation_failed")
                    raise GRPCError(Status.DEADLINE_EXCEEDED) from None
                elif wrapper.cancelled:
                    log.info("deadline_exceeded")
                    raise GRPCError(Status.DEADLINE_EXCEEDED) from None
                else:
                    log.exception("timeout_error")
                    raise
            except StreamTerminatedError as err:
                if wrapper.cancel_failed:
                    log.exception("cancellation_failed")
                    raise
                else:
                    assert wrapper.cancelled
                    log.info("request_cancelled", error=str(err))
                    raise
            except Exception:
                log.exception("application_error")
                raise
    except ProtocolError:
        log.exception("protocol_error")
    except Exception:
        log.exception("server_error")
    finally:
        release_stream()


# ---------------------------------------------------------------------------
# TtrpcHandler
# ---------------------------------------------------------------------------


class TtrpcHandler(_GC, AbstractTtrpcHandler):
    """Manages per-connection request tasks."""

    __gc_interval__ = 10
    closing: bool = False

    def __init__(
        self,
        mapping: dict[str, "const.Handler"],
        codec: CodecBase,
    ) -> None:
        self.mapping = mapping
        self.codec = codec
        self._tasks: dict[int, asyncio.Task[None]] = {}
        self._cancelled: set[asyncio.Task[None]] = set()

    def __gc_collect__(self) -> None:
        self._tasks = {s: t for s, t in self._tasks.items() if not t.done()}
        self._cancelled = {t for t in self._cancelled if not t.done()}

    def accept(
        self,
        raw_stream: TtrpcRawStream,
        initial_payload: bytes,
        flags: int,
        release: Callable[[], None],
    ) -> None:
        """Parse the ttrpc Request proto and spawn a handler task.

        The initial message payload is fed into the stream buffer HERE,
        synchronously, so that any subsequent DATA frames processed in the
        same ``data_received()`` call see the correct queue ordering.
        """
        self.__gc_step__()

        try:
            req = Request.FromString(initial_payload)
        except Exception:
            log.exception("parse_request_failed")
            release()
            return

        method_name = f"/{req.service}/{req.method}"
        payload_bytes = bytes(req.payload)

        # Pre-seed the stream buffer synchronously *before* spawning the task
        # so that the protocol layer's feed_eof() (called right after accept()
        # returns when FLAG_REMOTE_CLOSED is set) arrives *after* the data.
        if payload_bytes:
            raw_stream.feed_data(payload_bytes)

        loop = asyncio.get_event_loop()
        task = loop.create_task(
            request_handler(
                self.mapping,
                raw_stream,
                method_name,
                flags,
                req.timeout_nano,
                req.metadata,
                self.codec,
                release,
            )
        )
        self._tasks[raw_stream.stream_id] = task

    def close(self) -> None:
        for task in self._tasks.values():
            task.cancel()
        self._cancelled.update(self._tasks.values())
        self.closing = True

    async def wait_closed(self) -> None:
        if self._cancelled:
            await asyncio.wait(self._cancelled)

    def check_closed(self) -> bool:
        self.__gc_collect__()
        return not self._tasks and not self._cancelled


# ---------------------------------------------------------------------------
# Server
# ---------------------------------------------------------------------------


class Server(_GC):
    """ttrpc server over TCP or Unix sockets.

    Uses the same handler classes (with ``__mapping__()``) as
    :py:class:`grpclib.server.Server`.

    Example::

        server = Server([MyServiceImpl()])
        await server.start(path='/run/my.sock')
        async with server:
            await server.wait_closed()
    """

    __gc_interval__ = 10

    def __init__(
        self,
        handlers: Collection[IServable],
        *,
        codec: CodecBase | None = None,
    ) -> None:
        mapping: dict[str, const.Handler] = {}
        for handler in handlers:
            mapping.update(handler.__mapping__())

        self._mapping = mapping
        self._codec = codec if codec is not None else ProtoCodec()

        self._server: asyncio.AbstractServer | None = None
        self._server_closed_fut: asyncio.Future[None] | None = None
        self._handlers: set[TtrpcHandler] = set()

    def __gc_collect__(self) -> None:
        self._handlers = {h for h in self._handlers if not (h.closing and h.check_closed())}

    def _protocol_factory(self) -> TtrpcProtocol:
        self.__gc_step__()
        handler = TtrpcHandler(self._mapping, self._codec)
        self._handlers.add(handler)
        return TtrpcProtocol(handler)

    async def start(
        self,
        host: str | None = None,
        port: int | None = None,
        *,
        path: str | None = None,
        family: "socket.AddressFamily" = socket.AF_UNSPEC,
        flags: "socket.AddressInfo" = socket.AI_PASSIVE,
        sock: socket.socket | None = None,
        backlog: int = 100,
        ssl: Optional["_ssl.SSLContext"] = None,
        reuse_address: bool | None = None,
        reuse_port: bool | None = None,
    ) -> None:
        """Start listening.

        :param host: hostname or IP; ``None`` = all interfaces.
        :param port: TCP port number.
        :param path: Unix socket path.  Mutually exclusive with host/port.
        :param sock: pre-existing socket.
        :param backlog: maximum queued connections.
        :param ssl: SSL context for TLS.
        """
        if path is not None and (host is not None or port is not None):
            raise ValueError("The 'path' parameter cannot be combined with 'host'/'port'.")
        if self._server is not None:
            raise RuntimeError("Server is already started")

        loop = asyncio.get_event_loop()

        if path is not None:
            self._server = await loop.create_unix_server(
                self._protocol_factory,
                path,
                sock=sock,
                backlog=backlog,
                ssl=ssl,
            )
        else:
            self._server = await loop.create_server(  # type: ignore[no-matching-overload]
                self._protocol_factory,
                host,
                port,
                family=family,
                flags=flags,
                sock=sock,
                backlog=backlog,
                ssl=ssl,
                reuse_address=reuse_address,
                reuse_port=reuse_port,
            )
        self._server_closed_fut = loop.create_future()

    def close(self) -> None:
        """Stop accepting connections and cancel all in-flight requests."""
        if self._server is None or self._server_closed_fut is None:
            raise RuntimeError("Server is not started")
        self._server.close()
        if not self._server_closed_fut.done():
            self._server_closed_fut.set_result(None)
        for handler in self._handlers:
            handler.close()

    async def wait_closed(self) -> None:
        """Wait until all request handlers have finished."""
        if self._server is None or self._server_closed_fut is None:
            raise RuntimeError("Server is not started")
        await self._server_closed_fut
        await self._server.wait_closed()
        if self._handlers:
            loop = asyncio.get_event_loop()
            await asyncio.wait({loop.create_task(h.wait_closed()) for h in self._handlers})

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        self.close()
        await self.wait_closed()
