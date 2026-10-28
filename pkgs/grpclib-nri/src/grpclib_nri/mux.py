"""NRI multiplex layer: routes 8-byte-framed mux channels over one Unix socket.

NRI wraps every ttrpc frame in an 8-byte mux header before sending it over
the single /var/run/nri/nri.sock connection:

    [ConnID: uint32 BE][payload_length: uint32 BE][...ttrpc frame...]

ConnID=1 (PLUGIN_SERVICE_CONN)  — the plugin acts as a ttrpc *server*
ConnID=2 (RUNTIME_SERVICE_CONN) — the plugin acts as a ttrpc *client*

Source: nri/pkg/net/multiplex/{mux.go,ttrpc.go}
"""

import asyncio
import struct

import structlog

log = structlog.get_logger(__name__)

MUX_HEADER_FMT = ">II"
MUX_HEADER_SIZE = 8
MAX_MUX_PAYLOAD = 10 + 4 * 1024 * 1024  # mirrors Go mux.go maxPayloadSize

PLUGIN_SERVICE_CONN = 1  # plugin is ttRPC server on this channel
RUNTIME_SERVICE_CONN = 2  # plugin is ttRPC client on this channel


class MuxChannelTransport(asyncio.Transport):
    """asyncio.Transport that prefixes every write with an NRI mux frame header.

    Pass an instance of this to TtrpcProtocol.connection_made() so that all
    ttrpc frames emitted by the protocol are automatically wrapped with the
    correct ConnID.
    """

    def __init__(self, conn_id: int, writer: asyncio.StreamWriter) -> None:
        super().__init__()
        self._conn_id = conn_id
        self._writer = writer

    def write(self, data: bytes | bytearray | memoryview) -> None:
        # Convert to bytes for len() and concatenation
        data_bytes = bytes(data)
        header = struct.pack(MUX_HEADER_FMT, self._conn_id, len(data_bytes))
        self._writer.write(header + data_bytes)

    def is_closing(self) -> bool:
        return self._writer.is_closing()

    def close(self) -> None:
        # Closing one logical channel does not close the underlying trunk.
        pass

    def get_extra_info(self, name: str, default: object = None) -> object:
        return self._writer.get_extra_info(name, default)


class NriMux:
    """Demultiplexes NRI mux frames from a single Unix socket connection.

    Usage::

        mux = NriMux(reader, writer)
        loop.create_task(mux.read_loop())

        # ttRPC server side (ConnID=1):
        transport = mux.channel_transport(PLUGIN_SERVICE_CONN)
        protocol.connection_made(transport)
        loop.create_task(_serve_channel(mux, protocol))

        # ttRPC client side (ConnID=2): write directly then read back
        mux.writer.write(mux_header + ttrpc_frame)
        chunk = await mux.read_channel(RUNTIME_SERVICE_CONN)
    """

    def __init__(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        self._reader = reader
        self._writer = writer
        # Pre-create queues for both channels so read_loop and consumers
        # always share the same queue object regardless of start order.
        # Items are payload chunks; None marks EOF.
        self._channels: dict[int, asyncio.Queue[bytes | None]] = {
            PLUGIN_SERVICE_CONN: asyncio.Queue(),
            RUNTIME_SERVICE_CONN: asyncio.Queue(),
        }

    @property
    def writer(self) -> asyncio.StreamWriter:
        return self._writer

    def channel_transport(self, conn_id: int) -> MuxChannelTransport:
        """Return a Transport that wraps writes for *conn_id*."""
        return MuxChannelTransport(conn_id, self._writer)

    async def read_channel(self, conn_id: int) -> bytes | None:
        """Return the next payload chunk for *conn_id*, or None on EOF."""
        q = self._channels.get(conn_id)
        if q is None:
            raise KeyError(f"Unknown mux channel: {conn_id}")
        return await q.get()

    async def read_loop(self) -> None:
        """Background task: read mux frames and route to per-channel queues.

        Puts ``None`` into every channel queue when the connection closes so
        that waiting consumers can detect EOF.
        """
        try:
            while True:
                raw = await self._reader.readexactly(MUX_HEADER_SIZE)
                conn_id, length = struct.unpack(MUX_HEADER_FMT, raw)
                if length > MAX_MUX_PAYLOAD:
                    log.warning("mux_payload_too_large", bytes=length, conn_id=conn_id)
                    return
                payload = await self._reader.readexactly(length)
                log.debug("mux_recv", conn_id=conn_id, length=length)
                q = self._channels.get(conn_id)
                if q is None:
                    log.debug("mux_unknown_conn_id", conn_id=conn_id)
                else:
                    await q.put(payload)
        except asyncio.IncompleteReadError:
            log.debug("mux_connection_closed")
        except Exception as exc:  # noqa: BLE001 -- a read loop that dies takes every channel with it
            log.warning("mux_read_loop_error", exc=repr(exc))
        finally:
            # Signal EOF to all channel consumers.
            # put_nowait is safe here: all queues are unbounded (maxsize=0).
            for q in self._channels.values():
                q.put_nowait(None)
