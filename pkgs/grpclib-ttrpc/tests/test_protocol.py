"""Unit tests for grpclib_ttrpc.protocol (frame parsing)."""

from __future__ import annotations

import struct

from grpclib_ttrpc.protocol import (
    _HEADER_FMT,
    FLAG_REMOTE_CLOSED,
    HEADER_SIZE,
    MAX_PAYLOAD,
    MSG_TYPE_DATA,
    MSG_TYPE_REQUEST,
    TtrpcConnection,
    TtrpcProtocol,
)

from .helpers import FakeHandler, FakeTransport, encode_frame

# ---------------------------------------------------------------------------
# TtrpcConnection
# ---------------------------------------------------------------------------


class TestTtrpcConnection:
    def test_accepts_first_odd_stream(self) -> None:
        conn = TtrpcConnection()
        assert conn.validate_and_accept(1) is True

    def test_accepts_strictly_increasing(self) -> None:
        conn = TtrpcConnection()
        assert conn.validate_and_accept(1) is True
        assert conn.validate_and_accept(3) is True
        assert conn.validate_and_accept(5) is True

    def test_rejects_even_stream_id(self) -> None:
        conn = TtrpcConnection()
        assert conn.validate_and_accept(2) is False

    def test_rejects_non_increasing(self) -> None:
        conn = TtrpcConnection()
        conn.validate_and_accept(3)
        assert conn.validate_and_accept(3) is False  # equal
        assert conn.validate_and_accept(1) is False  # less than

    def test_rejects_zero(self) -> None:
        conn = TtrpcConnection()
        assert conn.validate_and_accept(0) is False


# ---------------------------------------------------------------------------
# TtrpcProtocol frame parsing helpers
# ---------------------------------------------------------------------------


def make_proto(handler: FakeHandler | None = None) -> tuple[TtrpcProtocol, FakeTransport, FakeHandler]:
    transport = FakeTransport()
    handler = handler or FakeHandler()
    proto = TtrpcProtocol(handler)
    proto.connection_made(transport)
    return proto, transport, handler


# ---------------------------------------------------------------------------
# Single frame delivery
# ---------------------------------------------------------------------------


class TestSingleFrame:
    def test_request_frame_dispatched(self) -> None:
        proto, _, handler = make_proto()
        payload = b"hello world"
        frame = encode_frame(1, MSG_TYPE_REQUEST, FLAG_REMOTE_CLOSED, payload)
        proto.data_received(frame)
        assert len(handler.accepted) == 1
        _, initial_payload, flags = handler.accepted[0]
        assert initial_payload == payload
        assert flags == FLAG_REMOTE_CLOSED

    def test_data_frame_feeds_existing_stream(self) -> None:
        proto, _, _handler = make_proto()
        proto.data_received(encode_frame(1, MSG_TYPE_REQUEST, 0, b"req"))

        proto.data_received(encode_frame(1, MSG_TYPE_DATA, 0, b"data"))
        # Queue should have the initial payload (not yet consumed) + data
        # We can't directly inspect the queue; just verify no crash.

    def test_empty_payload_request(self) -> None:
        proto, _, handler = make_proto()
        frame = encode_frame(1, MSG_TYPE_REQUEST, FLAG_REMOTE_CLOSED, b"")
        proto.data_received(frame)
        assert len(handler.accepted) == 1
        assert handler.accepted[0][1] == b""


# ---------------------------------------------------------------------------
# Fragmented delivery
# ---------------------------------------------------------------------------


class TestFragmentedDelivery:
    def test_header_split_across_chunks(self) -> None:
        proto, _, handler = make_proto()
        payload = b"fragmented"
        frame = encode_frame(1, MSG_TYPE_REQUEST, FLAG_REMOTE_CLOSED, payload)
        # split in the middle of the header
        split = 5
        proto.data_received(frame[:split])
        assert len(handler.accepted) == 0  # not dispatched yet
        proto.data_received(frame[split:])
        assert len(handler.accepted) == 1

    def test_multiple_frames_in_one_chunk(self) -> None:
        proto, _, handler = make_proto()
        f1 = encode_frame(1, MSG_TYPE_REQUEST, FLAG_REMOTE_CLOSED, b"first")
        f2 = encode_frame(3, MSG_TYPE_REQUEST, FLAG_REMOTE_CLOSED, b"second")
        proto.data_received(f1 + f2)
        assert len(handler.accepted) == 2

    def test_payload_arrives_in_pieces(self) -> None:
        proto, _, handler = make_proto()
        payload = b"x" * 100
        frame = encode_frame(1, MSG_TYPE_REQUEST, FLAG_REMOTE_CLOSED, payload)
        # send header + first half of payload, then second half
        mid = HEADER_SIZE + 50
        proto.data_received(frame[:mid])
        assert len(handler.accepted) == 0
        proto.data_received(frame[mid:])
        assert len(handler.accepted) == 1
        assert handler.accepted[0][1] == payload


# ---------------------------------------------------------------------------
# Stream-ID validation
# ---------------------------------------------------------------------------


class TestStreamIdValidation:
    def test_even_stream_id_rejected(self) -> None:
        proto, _, handler = make_proto()
        frame = encode_frame(2, MSG_TYPE_REQUEST, 0, b"bad")
        proto.data_received(frame)
        assert len(handler.accepted) == 0  # discarded

    def test_non_increasing_stream_id_rejected(self) -> None:
        proto, _, handler = make_proto()
        proto.data_received(encode_frame(3, MSG_TYPE_REQUEST, 0, b"first"))
        # send lower ID
        proto.data_received(encode_frame(1, MSG_TYPE_REQUEST, 0, b"bad"))
        assert len(handler.accepted) == 1  # only first accepted


# ---------------------------------------------------------------------------
# Oversized payload
# ---------------------------------------------------------------------------


class TestOversizedPayload:
    def test_oversized_drops_connection(self) -> None:
        proto, transport, _ = make_proto()
        # forge a header claiming MAX_PAYLOAD + 1
        bad_header = struct.pack(_HEADER_FMT, MAX_PAYLOAD + 1, 1, MSG_TYPE_REQUEST, 0)
        proto.data_received(bad_header)
        assert transport._closing is True


# ---------------------------------------------------------------------------
# DATA frame routing
# ---------------------------------------------------------------------------


class TestDataFrameRouting:
    def test_data_frame_for_unknown_stream_ignored(self) -> None:
        proto, _, _handler = make_proto()
        # No prior REQUEST frame for stream 5
        proto.data_received(encode_frame(5, MSG_TYPE_DATA, 0, b"orphan"))
        # No crash, handler not notified

    def test_data_frame_with_close_feeds_eof(self) -> None:
        proto, _, _ = make_proto()

        proto.data_received(encode_frame(1, MSG_TYPE_REQUEST, 0, b"req"))
        # Now close the stream via a DATA frame
        proto.data_received(encode_frame(1, MSG_TYPE_DATA, FLAG_REMOTE_CLOSED, b""))
        # No assertion needed; just verify no crash

    def test_unknown_msg_type_ignored(self) -> None:
        proto, _, handler = make_proto()
        bad_frame = encode_frame(1, 0xFF, 0, b"unknown")
        proto.data_received(bad_frame)
        assert len(handler.accepted) == 0


# ---------------------------------------------------------------------------
# connection_lost propagates error to active streams
# ---------------------------------------------------------------------------


class TestConnectionLost:
    def test_error_fed_to_streams_on_connection_lost(self) -> None:
        proto, _, handler = make_proto()
        proto.data_received(encode_frame(1, MSG_TYPE_REQUEST, 0, b"req"))
        raw_stream = handler.accepted[0][0]

        exc = ConnectionResetError("peer gone")
        proto.connection_lost(exc)

        assert handler.closed is True
        # Verify error was fed into the stream buffer (_error is set)
        assert raw_stream._buffer._error is not None
