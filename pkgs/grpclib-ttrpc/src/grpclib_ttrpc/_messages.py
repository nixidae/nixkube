"""Helpers for ttrpc protocol: build_response() function."""

from grpclib.const import Status as gStatus
from ttrpc.proto.status_pb2 import Status as ttStatus
from ttrpc.ttrpc_pb2 import Response


def build_response(
    status: gStatus,
    message: str | None,
    payload: bytes,
) -> bytes:
    """Serialize a ttrpc Response frame payload."""
    # Note: ttrpc Status message only has a code field, message is not part of the protocol
    ttrpc_status = ttStatus(code=status.value)
    response = Response(
        status=ttrpc_status,
        payload=payload,
    )
    # Generated protobuf code is untyped, so SerializeToString() returns Any.
    serialized: bytes = response.SerializeToString()
    return serialized
