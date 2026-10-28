"""End-to-end functional tests for grpclib_ttrpc.Server."""

import asyncio
import os
from typing import cast

import pytest
import pytest_asyncio
from grpclib.const import Status
from grpclib.encoding.proto import ProtoCodec
from grpclib.exceptions import GRPCError
from grpclib_ttrpc import Server
from grpclib_ttrpc.protocol import FLAG_REMOTE_CLOSED
from ttrpc.ttrpc_pb2 import Response

from .dummy_pb2 import (
    DummyReply,  # type: ignore[import-not-found]
    DummyRequest,  # type: ignore[import-not-found]
)
from .helpers import DummyServiceImpl, TtrpcClient

CODEC = ProtoCodec()


def enc(msg):
    return CODEC.encode(msg, type(msg))


def dec_reply(payload: bytes) -> DummyReply:
    return CODEC.decode(payload, DummyReply)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def tcp_server():
    svc = DummyServiceImpl()
    server = Server([svc])
    await server.start(host="127.0.0.1", port=0)
    # Extract the bound port
    assert server._server is not None
    sock = cast("asyncio.Server", server._server).sockets[0]
    port = sock.getsockname()[1]
    yield server, port
    server.close()
    await server.wait_closed()


@pytest_asyncio.fixture
async def tcp_client(tcp_server):
    _server, port = tcp_server
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    client = TtrpcClient(reader, writer)
    yield client
    client.close()


@pytest_asyncio.fixture
async def unix_server():
    # Use /tmp to avoid "AF_UNIX path too long" error when running in Nix build
    # (pytest's tmp_path is too deep in the Nix store, exceeding 108-byte limit)
    sock_path = f"/tmp/ttrpc_test_{os.getpid()}.sock"
    svc = DummyServiceImpl()
    server = Server([svc])
    await server.start(path=sock_path)
    yield server, sock_path
    server.close()
    await server.wait_closed()


@pytest_asyncio.fixture
async def unix_client(unix_server):
    _server, path = unix_server
    reader, writer = await asyncio.open_unix_connection(path)
    client = TtrpcClient(reader, writer)
    yield client
    client.close()


# ---------------------------------------------------------------------------
# UNARY_UNARY
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unary_unary_tcp(tcp_client):
    req_bytes = enc(DummyRequest(value="hi"))
    resp_bytes = await tcp_client.send_unary_request("dummy.DummyService", "UnaryUnary", req_bytes)
    resp = Response.FromString(resp_bytes)
    assert resp.status.code == Status.OK.value
    reply = dec_reply(resp.payload)
    assert reply.value == "echo:hi"


@pytest.mark.asyncio
async def test_unary_unary_unix(unix_client):
    req_bytes = enc(DummyRequest(value="unix"))
    resp_bytes = await unix_client.send_unary_request("dummy.DummyService", "UnaryUnary", req_bytes)
    resp = Response.FromString(resp_bytes)
    assert resp.status.code == Status.OK.value
    reply = dec_reply(resp.payload)
    assert reply.value == "echo:unix"


# ---------------------------------------------------------------------------
# UNARY_STREAM
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unary_stream_tcp(tcp_client):
    req_bytes = enc(DummyRequest(value="item"))
    sid = await tcp_client.open_request("dummy.DummyService", "UnaryStream", req_bytes)
    # For unary client, send FLAG_REMOTE_CLOSED via a DATA frame immediately
    await tcp_client.send_data_frame(sid, b"", close=True)

    # Collect 3 DATA frames + 1 final DATA with FLAG_REMOTE_CLOSED|FLAG_NO_DATA
    results = []
    while True:
        flags, payload = await tcp_client.read_data_frame()
        if payload:
            results.append(dec_reply(payload).value)
        if flags & FLAG_REMOTE_CLOSED:
            break
    assert results == ["item:0", "item:1", "item:2"]


# ---------------------------------------------------------------------------
# STREAM_UNARY
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stream_unary_tcp(tcp_client):
    # Open request without closing
    sid = await tcp_client.open_request("dummy.DummyService", "StreamUnary")

    # Send 3 messages as DATA frames
    for v in ["a", "b", "c"]:
        msg_bytes = enc(DummyRequest(value=v))
        await tcp_client.send_data_frame(sid, msg_bytes)

    # Final DATA frame closes the stream
    await tcp_client.send_data_frame(sid, b"", close=True)

    resp_bytes = await tcp_client._read_response()
    resp = Response.FromString(resp_bytes)
    assert resp.status.code == Status.OK.value
    assert dec_reply(resp.payload).value == "a,b,c"


# ---------------------------------------------------------------------------
# STREAM_STREAM
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stream_stream_tcp(tcp_client):
    sid = await tcp_client.open_request("dummy.DummyService", "StreamStream")

    messages = ["x", "y", "z"]
    for v in messages:
        await tcp_client.send_data_frame(sid, enc(DummyRequest(value=v)))
    await tcp_client.send_data_frame(sid, b"", close=True)

    results = []
    while True:
        flags, payload = await tcp_client.read_data_frame()
        if payload:
            results.append(dec_reply(payload).value)
        if flags & FLAG_REMOTE_CLOSED:
            break

    assert results == ["echo:x", "echo:y", "echo:z"]


# ---------------------------------------------------------------------------
# Method not found → UNIMPLEMENTED
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unimplemented_method(tcp_client):
    resp_bytes = await tcp_client.send_unary_request("dummy.DummyService", "NonExistentMethod", b"")
    resp = Response.FromString(resp_bytes)
    assert resp.status.code == Status.UNIMPLEMENTED.value


# ---------------------------------------------------------------------------
# GRPCError in handler → error response
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_grpc_error_in_handler(tcp_server):
    """Handler that raises GRPCError should produce an error Response."""

    class ErrorService:
        async def Fail(self, stream):
            raise GRPCError(Status.PERMISSION_DENIED, "nope")

        def __mapping__(self):
            import grpclib.const

            return {
                "/svc/Fail": grpclib.const.Handler(
                    self.Fail,
                    grpclib.const.Cardinality.UNARY_UNARY,
                    DummyRequest,
                    DummyReply,
                )
            }

    _server, _port = tcp_server
    # spin up a separate server for this test
    err_server = Server([ErrorService()])
    await err_server.start(host="127.0.0.1", port=0)
    assert err_server._server is not None
    err_port = cast("asyncio.Server", err_server._server).sockets[0].getsockname()[1]

    reader, writer = await asyncio.open_connection("127.0.0.1", err_port)
    client = TtrpcClient(reader, writer)
    resp_bytes = await client.send_unary_request("svc", "Fail", b"")
    resp = Response.FromString(resp_bytes)
    assert resp.status.code == Status.PERMISSION_DENIED.value
    # Note: ttrpc Status message only has code field, not message field like gRPC
    client.close()
    err_server.close()
    await err_server.wait_closed()


# ---------------------------------------------------------------------------
# Deadline exceeded
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_deadline_exceeded(tcp_server):
    """Handler that sleeps past deadline should get DEADLINE_EXCEEDED."""

    class SlowService:
        async def Slow(self, stream):
            await asyncio.sleep(10)
            await stream.send_message(DummyReply(value="too late"))

        def __mapping__(self):
            import grpclib.const

            return {
                "/svc/Slow": grpclib.const.Handler(
                    self.Slow,
                    grpclib.const.Cardinality.UNARY_UNARY,
                    DummyRequest,
                    DummyReply,
                )
            }

    slow_server = Server([SlowService()])
    await slow_server.start(host="127.0.0.1", port=0)
    assert slow_server._server is not None
    slow_port = cast("asyncio.Server", slow_server._server).sockets[0].getsockname()[1]

    reader, writer = await asyncio.open_connection("127.0.0.1", slow_port)
    client = TtrpcClient(reader, writer)

    # 1 ms timeout = 1,000,000 ns
    resp_bytes = await asyncio.wait_for(
        client.send_unary_request(
            "svc",
            "Slow",
            enc(DummyRequest(value="x")),
            timeout_nano=1_000_000,
        ),
        timeout=5.0,
    )
    resp = Response.FromString(resp_bytes)
    assert resp.status.code == Status.DEADLINE_EXCEEDED.value

    client.close()
    slow_server.close()
    await slow_server.wait_closed()
