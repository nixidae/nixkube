"""Per-stream async byte buffer for ttrpc."""

import asyncio
from typing import Final

from grpclib.exceptions import StreamTerminatedError


class _Eof:
    """Marks end of stream in the queue. Distinct from None, which marks an error."""


_EOF_SENTINEL: Final = _Eof()


class StreamBuffer:
    """asyncio.Queue-based buffer for one ttrpc stream's incoming payloads.

    The protocol layer calls :py:meth:`feed_data` / :py:meth:`feed_eof` /
    :py:meth:`feed_error` from the event-loop thread (always synchronous).
    The handler task calls :py:meth:`read_message` which awaits the queue.
    """

    def __init__(self) -> None:
        # Queue contains: bytes (data), _EOF_SENTINEL (end-of-stream), or None (error marker)
        self._queue: asyncio.Queue[bytes | _Eof | None] = asyncio.Queue()
        self._error: Exception | None = None

    def feed_data(self, data: bytes) -> None:
        """Push a raw payload into the queue (called by protocol layer)."""
        self._queue.put_nowait(data)

    def feed_eof(self) -> None:
        """Signal end of stream by pushing a sentinel object."""
        self._queue.put_nowait(_EOF_SENTINEL)

    def feed_error(self, exc: Exception) -> None:
        """Record a connection error; subsequent reads will raise it."""
        self._error = exc
        # wake up any waiter immediately so it sees the error
        self._queue.put_nowait(None)

    async def read_message(self) -> bytes | None:
        """Block until a payload is available.

        Returns ``None`` at EOF, raises :py:exc:`StreamTerminatedError` on
        connection error.

        Error takes precedence over EOF: if both feed_error() and feed_eof()
        are called, the error is raised rather than returning EOF.
        """
        item = await self._queue.get()
        # Error takes precedence: check it first
        if self._error is not None:
            raise StreamTerminatedError(str(self._error)) from self._error
        # EOF sentinel means end of stream
        if isinstance(item, _Eof):
            return None
        # Otherwise it's bytes data
        return item
