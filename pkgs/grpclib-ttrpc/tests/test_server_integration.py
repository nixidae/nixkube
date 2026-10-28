# SPDX-License-Identifier: Apache-2.0
"""Integration tests for grpclib_ttrpc against Go test server.

Tests the Python client implementation against the real Go test server
(github.com/containerd/ttrpc/integration/streaming) to verify Python-Go
interoperability across all RPC cardinalities.

The Go server implements the Streaming service:
- Echo (unary)
- EchoStream (server streaming)
- SumStream (client streaming)
- DivideStream (bidirectional streaming)
"""

import asyncio
from pathlib import Path

import pytest
import pytest_asyncio
import structlog
from google.protobuf.empty_pb2 import Empty
from grpclib.encoding.proto import ProtoCodec
from grpclib_ttrpc import Client
from ttrpc.streaming_pb2 import EchoPayload, Part, Sum

from .helpers import FLAG_REMOTE_CLOSED, TtrpcClient

CODEC = ProtoCodec()


def enc(msg):
    return CODEC.encode(msg, type(msg))


def dec(payload: bytes, msg_type):
    return CODEC.decode(payload, msg_type)


@pytest_asyncio.fixture
async def streaming_client(test_server_process, socket_path: Path):
    """Create a raw TtrpcClient connected to the Go test server via Unix socket."""
    logger = structlog.get_logger("test.streaming_client")
    logger.info("connecting_client", socket=str(socket_path))

    reader, writer = await asyncio.open_unix_connection(str(socket_path))
    client = TtrpcClient(reader, writer)
    yield client
    client.close()


# ---------------------------------------------------------------------------
# UNARY_UNARY
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unary_echo(streaming_client: TtrpcClient) -> None:
    """Test unary Echo RPC."""
    req = EchoPayload(seq=1, msg="hello")
    resp_bytes = await streaming_client.send_unary_request(
        "ttrpc.integration.streaming.Streaming",
        "Echo",
        enc(req),
    )
    from ttrpc.ttrpc_pb2 import Response

    resp = Response.FromString(resp_bytes)
    reply = dec(resp.payload, EchoPayload)
    assert reply.seq == 2  # Server increments seq
    assert reply.msg == "hello"


@pytest.mark.asyncio
async def test_unary_echo_multiple(streaming_client: TtrpcClient) -> None:
    """Test multiple unary Echo calls."""
    for seq in [1, 10, 100]:
        req = EchoPayload(seq=seq, msg=f"msg{seq}")
        resp_bytes = await streaming_client.send_unary_request(
            "ttrpc.integration.streaming.Streaming",
            "Echo",
            enc(req),
        )
        from ttrpc.ttrpc_pb2 import Response

        resp = Response.FromString(resp_bytes)
        reply = dec(resp.payload, EchoPayload)
        assert reply.seq == seq + 1


# ---------------------------------------------------------------------------
# UNARY_STREAM (server streaming)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_echo_stream(streaming_client: TtrpcClient) -> None:
    """Test server streaming EchoStream RPC."""
    req = EchoPayload(seq=42, msg="stream")
    sid = await streaming_client.open_request(
        "ttrpc.integration.streaming.Streaming",
        "EchoStream",
        enc(req),
    )
    # Close the stream from client side
    await streaming_client.send_data_frame(sid, b"", close=True)

    # Server echoes back the payload
    _flags, payload = await streaming_client.read_data_frame()
    reply = dec(payload, EchoPayload)
    assert reply.seq == 43
    assert reply.msg == "stream"


# ---------------------------------------------------------------------------
# STREAM_UNARY (client streaming)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sum_stream(streaming_client: TtrpcClient) -> None:
    """Test client streaming SumStream RPC (sums multiple Part messages)."""
    sid = await streaming_client.open_request(
        "ttrpc.integration.streaming.Streaming",
        "SumStream",
    )

    # Send multiple Part messages with numbers to sum
    values = [10, 20, 30]
    for value in values:
        part = Part(add=value)
        await streaming_client.send_data_frame(sid, enc(part))

    # Close stream
    await streaming_client.send_data_frame(sid, b"", close=True)

    # Read response
    resp_bytes = await streaming_client._read_response()
    from ttrpc.ttrpc_pb2 import Response

    resp = Response.FromString(resp_bytes)
    result = dec(resp.payload, Sum)
    assert result.sum == sum(values)  # 10 + 20 + 30 = 60


# ---------------------------------------------------------------------------
# STREAM_STREAM (bidirectional streaming)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_divide_stream(streaming_client: TtrpcClient) -> None:
    """Test bidirectional streaming DivideStream RPC.

    Sends a Sum message and receives streamed Part responses.
    """
    sid = await streaming_client.open_request(
        "ttrpc.integration.streaming.Streaming",
        "DivideStream",
        enc(Sum(sum=100, num=5)),  # Server will divide 100/5 = 20, 5 times
    )

    # Close stream from client side
    await streaming_client.send_data_frame(sid, b"", close=True)

    # Collect all Part responses
    parts = []
    while True:
        flags, payload = await streaming_client.read_data_frame()
        if payload:
            parts.append(dec(payload, Part))
        if flags & FLAG_REMOTE_CLOSED:
            break

    # Server returns num=5 Part messages with avg=20
    assert len(parts) == 5
    for part in parts:
        assert part.add == 20  # 100 / 5 = 20


# ---------------------------------------------------------------------------
# Empty/null message handling
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_echo_null(streaming_client: TtrpcClient) -> None:
    """Test client streaming that returns empty response.

    Server receives multiple EchoPayload messages but returns google.protobuf.Empty.
    """
    from ttrpc.ttrpc_pb2 import Response

    sid = await streaming_client.open_request(
        "ttrpc.integration.streaming.Streaming",
        "EchoNull",
    )

    # Send multiple EchoPayload messages
    for i in range(3):
        payload = EchoPayload(seq=i, msg=f"msg{i}")
        await streaming_client.send_data_frame(sid, enc(payload))

    # Close stream
    await streaming_client.send_data_frame(sid, b"", close=True)

    # Read response (should be empty)
    resp_bytes = await streaming_client._read_response()
    resp = Response.FromString(resp_bytes)
    assert resp.status.code == 0  # OK
    # Response payload should be empty
    assert resp.payload == b""


@pytest.mark.asyncio
async def test_echo_null_stream(streaming_client: TtrpcClient) -> None:
    """Test bidirectional streaming that sends empty messages.

    Server receives EchoPayload messages and responds with google.protobuf.Empty.
    """
    sid = await streaming_client.open_request(
        "ttrpc.integration.streaming.Streaming",
        "EchoNullStream",
    )

    # Send EchoPayload messages
    for i in range(2):
        payload = EchoPayload(seq=i, msg=f"msg{i}")
        await streaming_client.send_data_frame(sid, enc(payload))

    # Close stream
    await streaming_client.send_data_frame(sid, b"", close=True)

    # Collect empty responses (should get 2 empty DATA frames + final close)
    count = 0
    while True:
        flags, payload = await streaming_client.read_data_frame()
        # Each response should be empty
        if payload:
            # Empty message should decode as empty bytes
            empty = dec(payload, Empty)
            assert empty.ByteSize() == 0
        if flags & FLAG_REMOTE_CLOSED:
            break
        count += 1


@pytest.mark.asyncio
async def test_empty_payload_stream(streaming_client: TtrpcClient) -> None:
    """Test server streaming from empty input.

    Client sends google.protobuf.Empty, server responds with 5 EchoPayload messages.
    """
    # Send empty request
    req = Empty()
    sid = await streaming_client.open_request(
        "ttrpc.integration.streaming.Streaming",
        "EmptyPayloadStream",
        enc(req),
    )

    # Close stream (client side)
    await streaming_client.send_data_frame(sid, b"", close=True)

    # Collect EchoPayload responses (should get 5)
    payloads = []
    while True:
        flags, payload = await streaming_client.read_data_frame()
        if payload:
            echo = dec(payload, EchoPayload)
            payloads.append(echo)
        if flags & FLAG_REMOTE_CLOSED:
            break

    # Server should send exactly 5 payloads
    assert len(payloads) == 5
    # Each payload should have incrementing seq and message
    for i, echo in enumerate(payloads):
        assert echo.seq == i
        assert echo.msg == f"payload {i}"


# ---------------------------------------------------------------------------
# Timeout/deadline handling
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_request_with_long_timeout(streaming_client: TtrpcClient) -> None:
    """Test request with generous timeout completes successfully."""
    from grpclib.const import Status
    from ttrpc.ttrpc_pb2 import Response

    req = EchoPayload(seq=1, msg="hello")
    # Send with 5 second timeout (plenty of time)
    timeout_nano = int(5 * 1e9)  # 5 seconds in nanoseconds

    resp_bytes = await streaming_client.send_unary_request(
        "ttrpc.integration.streaming.Streaming",
        "Echo",
        enc(req),
        timeout_nano=timeout_nano,
    )
    resp = Response.FromString(resp_bytes)
    assert resp.status.code == Status.OK.value
    reply = dec(resp.payload, EchoPayload)
    assert reply.seq == 2


@pytest.mark.asyncio
async def test_request_with_very_short_timeout(streaming_client: TtrpcClient) -> None:
    """Test request with very short timeout may expire.

    Note: This test is probabilistic - the server might respond before
    timeout on fast systems. We check that if it times out, the status
    is DEADLINE_EXCEEDED.
    """
    from grpclib.const import Status
    from ttrpc.ttrpc_pb2 import Response

    req = EchoPayload(seq=1, msg="test")
    # 1 millisecond timeout (very tight, server unlikely to respond in time)
    timeout_nano = int(0.001 * 1e9)  # 1ms in nanoseconds

    try:
        resp_bytes = await asyncio.wait_for(
            streaming_client.send_unary_request(
                "ttrpc.integration.streaming.Streaming",
                "Echo",
                enc(req),
                timeout_nano=timeout_nano,
            ),
            timeout=2.0,  # Client-side timeout as fallback
        )
        resp = Response.FromString(resp_bytes)
        # If we got a response, it might be OK or DEADLINE_EXCEEDED
        # depending on timing
        assert resp.status.code in (Status.OK.value, Status.DEADLINE_EXCEEDED.value)
    except TimeoutError:
        # Client-side timeout is also acceptable for this test
        pass


@pytest.mark.asyncio
async def test_client_side_timeout_on_slow_operation(
    streaming_client: TtrpcClient,
) -> None:
    """Test client-side timeout during streaming operation.

    Open a streaming connection and timeout if we don't get response quickly.
    """
    req = EchoPayload(seq=1, msg="stream")
    sid = await streaming_client.open_request(
        "ttrpc.integration.streaming.Streaming",
        "EchoStream",
        enc(req),
    )
    await streaming_client.send_data_frame(sid, b"", close=True)

    # Should receive response within 1 second
    try:
        _flags, payload = await asyncio.wait_for(
            streaming_client.read_data_frame(),
            timeout=1.0,
        )
        # If we got a response, validate it
        if payload:
            reply = dec(payload, EchoPayload)
            assert reply.seq == 2
    except TimeoutError:
        pytest.fail("Response took longer than 1 second")


@pytest.mark.asyncio
async def test_streaming_with_timeout(streaming_client: TtrpcClient) -> None:
    """Test client streaming respects timeout."""
    from grpclib.const import Status
    from ttrpc.ttrpc_pb2 import Response

    sid = await streaming_client.open_request(
        "ttrpc.integration.streaming.Streaming",
        "SumStream",
    )

    # Send messages with a reasonable timeout expectation
    for value in [5, 10, 15]:
        part = Part(add=value)
        await streaming_client.send_data_frame(sid, enc(part))

    await streaming_client.send_data_frame(sid, b"", close=True)

    # Should complete within 2 seconds
    try:
        resp_bytes = await asyncio.wait_for(
            streaming_client._read_response(),
            timeout=2.0,
        )
        resp = Response.FromString(resp_bytes)
        assert resp.status.code == Status.OK.value
        result = dec(resp.payload, Sum)
        assert result.sum == sum([5, 10, 15])
    except TimeoutError:
        pytest.fail("Streaming operation timed out")


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_method_not_found(streaming_client: TtrpcClient) -> None:
    """Test calling non-existent method returns UNIMPLEMENTED error."""
    from grpclib.const import Status
    from ttrpc.ttrpc_pb2 import Response

    req = EchoPayload(seq=1, msg="test")
    resp_bytes = await streaming_client.send_unary_request(
        "ttrpc.integration.streaming.Streaming",
        "NonExistentMethod",
        enc(req),
    )
    resp = Response.FromString(resp_bytes)
    assert resp.status.code == Status.UNIMPLEMENTED.value


@pytest.mark.asyncio
async def test_service_not_found(streaming_client: TtrpcClient) -> None:
    """Test calling non-existent service returns UNIMPLEMENTED error."""
    from grpclib.const import Status
    from ttrpc.ttrpc_pb2 import Response

    req = EchoPayload(seq=1, msg="test")
    resp_bytes = await streaming_client.send_unary_request(
        "com.example.NonExistent",
        "SomeMethod",
        enc(req),
    )
    resp = Response.FromString(resp_bytes)
    assert resp.status.code == Status.UNIMPLEMENTED.value


@pytest.mark.asyncio
async def test_malformed_payload(streaming_client: TtrpcClient) -> None:
    """Test sending malformed payload returns error."""
    from ttrpc.ttrpc_pb2 import Response

    # Send garbage bytes instead of valid protobuf
    malformed = b"\x00\x01\x02\x03\xff\xfe\xfd"
    resp_bytes = await streaming_client.send_unary_request(
        "ttrpc.integration.streaming.Streaming",
        "Echo",
        malformed,
    )
    resp = Response.FromString(resp_bytes)
    # Server should return an error status (likely INVALID_ARGUMENT or UNKNOWN)
    assert resp.status.code != 0  # Not OK


@pytest.mark.asyncio
async def test_streaming_with_invalid_message(streaming_client: TtrpcClient) -> None:
    """Test server streaming with malformed input message."""
    from ttrpc.ttrpc_pb2 import Response

    # Send garbage as initial payload
    malformed = b"\xff\xfe\xfd\xfc"
    sid = await streaming_client.open_request(
        "ttrpc.integration.streaming.Streaming",
        "EchoStream",
        malformed,
    )
    await streaming_client.send_data_frame(sid, b"", close=True)

    # Server should respond with error
    resp_bytes = await streaming_client._read_response()
    resp = Response.FromString(resp_bytes)
    assert resp.status.code != 0  # Not OK


# ---------------------------------------------------------------------------
# Smoke tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_server_starts(test_server_process, socket_path: Path) -> None:
    """Smoke test: Verify Go test server starts and creates socket."""
    logger = structlog.get_logger("test.server_starts")

    assert socket_path.exists(), f"Server socket not created at {socket_path}"
    assert test_server_process.returncode is None, (
        f"Server exited with code {test_server_process.returncode}"
    )

    logger.info("server_running")


# ---------------------------------------------------------------------------
# Client API tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_client_unary(socket_path: Path, test_server_process) -> None:
    """Test Client API for unary RPC."""
    client = Client(path=str(socket_path))
    try:
        req = EchoPayload(seq=1, msg="hello")
        resp = await client.unary(
            "ttrpc.integration.streaming.Streaming",
            "Echo",
            req,
            EchoPayload,
            EchoPayload,
        )
        assert resp.seq == 2
        assert resp.msg == "hello"
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_client_server_streaming(socket_path: Path, test_server_process) -> None:
    """Test Client API for server streaming RPC."""
    client = Client(path=str(socket_path))
    try:
        req = Sum(sum=100, num=5)
        results = []
        async for resp in client.server_stream(
            "ttrpc.integration.streaming.Streaming",
            "DivideStream",
            req,
            Sum,
            Part,
        ):
            results.append(resp)
        assert len(results) == 5
        for part in results:
            assert part.add == 20
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_client_client_streaming(socket_path: Path, test_server_process) -> None:
    """Test Client API for client streaming RPC."""
    client = Client(path=str(socket_path))
    try:
        async with await client.client_stream(
            "ttrpc.integration.streaming.Streaming",
            "SumStream",
            Sum,
        ) as stream:
            for value in [5, 10, 15]:
                part = Part(add=value)
                await stream.send(part)
            result = await stream.recv()

        assert result.sum == 30
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_client_bidirectional_streaming(
    socket_path: Path, test_server_process
) -> None:
    """Test Client API for bidirectional streaming RPC."""
    client = Client(path=str(socket_path))
    try:
        async with await client.bidirectional_stream(
            "ttrpc.integration.streaming.Streaming",
            "EchoStream",
            EchoPayload,
        ) as stream:
            # Send three echo messages
            for seq in range(1, 4):
                msg = EchoPayload(seq=seq, msg=f"echo{seq}")
                await stream.send(msg)
            await stream.close()

            # Receive echoes
            results = []
            async for resp in stream:
                results.append(resp)

        # Server echoes back all messages - expect 3 echoed messages
        assert len(results) >= 3
        # Collect the echoed messages (seq values should match sent messages)
        echoed_seqs = sorted([resp.seq for resp in results if resp.seq in (1, 2, 3)])
        assert echoed_seqs == [1, 2, 3]
    finally:
        await client.close()
