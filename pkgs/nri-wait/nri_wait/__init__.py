"""NRI wait - OCI hook for waiting on Nix builds."""

import json
import sys
import time
from collections.abc import Callable

import zmq

__version__ = "0.1.0"


def fail_build(container_id: str, reason: str) -> None:
    """Exit with the daemon's reason, which kubelet shows in the pod's events.

    Raises SystemExit(1).
    """
    print(f"[nri-wait] The build for {container_id} failed: {reason}", file=sys.stderr)
    sys.exit(1)


def check_build_status(
    context: zmq.Context,
    query_socket_path: str,
    oci_state: dict,
    timeout: int,
) -> bool:
    """Query REP socket to check if build is already done.

    Returns True if build is complete, False if still pending or socket unavailable.
    Raises SystemExit(1) if the daemon says the build failed.
    """
    req = context.socket(zmq.REQ)

    # Set timeouts so we don't hang
    timeout_ms = timeout * 1000
    req.setsockopt(zmq.RCVTIMEO, timeout_ms)
    req.setsockopt(zmq.SNDTIMEO, timeout_ms)

    socket_path = f"ipc://{query_socket_path}"
    print(f"[nri-wait] Connecting to query socket: {socket_path}", file=sys.stderr)

    try:
        req.connect(socket_path)
    except zmq.error.ZMQError as e:
        print(
            f"[nri-wait] Failed to connect to query socket: {e} (may not be ready yet)",
            file=sys.stderr,
        )
        req.close()
        return False  # Assume not done, will wait on pub socket

    # Send the full OCI state so nix-nri can extract id, pid, and bundle itself.
    query = json.dumps(oci_state)

    try:
        req.send(query.encode())
        response_bytes = req.recv()
    except zmq.error.Again:
        print("[nri-wait] Query socket timeout", file=sys.stderr)
        req.close()
        return False
    except zmq.error.ZMQError as e:
        print(f"[nri-wait] Query error: {e}", file=sys.stderr)
        req.close()
        return False

    req.close()

    # Parse response
    try:
        response = json.loads(response_bytes.decode())
        print(f"[nri-wait] Query response: {response}", file=sys.stderr)

        if response.get("status") == "done":
            return True
        if response.get("status") == "failed":
            fail_build(oci_state.get("id", "?"), response.get("reason", "no reason given"))
    except (json.JSONDecodeError, UnicodeDecodeError, KeyError) as e:
        print(f"[nri-wait] Failed to parse response: {e}", file=sys.stderr)

    return False


def connect_updates(context: zmq.Context, pub_socket_path: str) -> zmq.Socket:
    """Subscribe to the PUB socket.

    The caller does this *before* it queries the REP socket. ZeroMQ drops a
    published message that no subscriber has joined for yet, so a "done"
    published between the query's answer and the subscription reaches
    nobody. The hook would then wait for a heartbeat that never arrives:
    the daemon stops its pump as soon as the build is done.

    Raises SystemExit(1) if the socket cannot be reached.
    """
    sub = context.socket(zmq.SUB)

    # Subscribe to all messages (empty filter = all)
    sub.setsockopt(zmq.SUBSCRIBE, b"")

    socket_path = f"ipc://{pub_socket_path}"
    print(f"[nri-wait] Connecting to pub socket: {socket_path}", file=sys.stderr)

    try:
        sub.connect(socket_path)
    except zmq.error.ZMQError as e:
        print(f"[nri-wait] Failed to connect to pub socket: {e}", file=sys.stderr)
        sub.close()
        sys.exit(1)

    return sub


def wait_for_completion(
    sub: zmq.Socket,
    container_id: str,
    timeout: int,
    recheck: Callable[[], bool],
) -> None:
    """Wait on the PUB socket for the build completion message.

    ``timeout`` is how long the build daemon may stay silent. It is not a
    budget for the build. The daemon publishes a heartbeat every ten seconds
    while it works, and each heartbeat pushes the deadline out again. So a
    build takes as long as it takes, and only silence fails.

    There used to be a second deadline as well, an absolute one. It started
    at the same moment and ran for the same duration, and nothing reset it.
    That made the heartbeat decoration: no build could last longer than
    ``timeout``, however healthy it was. Measured on a one-CPU node with
    three containers starting at once -- the hook gave up at 30s, and the
    daemon reported the build a success 11s later, into a container runc had
    already destroyed.

    One gap stays, and it is worth naming. A pump that publishes while Nix
    hangs is a pump that lies. This detects a dead daemon, not a stuck build.

    ``recheck`` asks the REP socket for the status. The REP socket is the
    truth and the PUB socket is only the fast path, so a silent daemon gets
    one direct question before this gives up.

    Raises SystemExit(1) on timeout or error.
    """
    # The only deadline. Every heartbeat pushes it out again.
    deadline = time.time() + timeout

    while True:
        remaining = deadline - time.time()
        if remaining <= 0:
            if recheck():
                print(
                    f"[nri-wait] Build already completed for {container_id}",
                    file=sys.stderr,
                )
                sub.close()
                return
            print(
                f"[nri-wait] The build daemon said nothing for {timeout}s",
                file=sys.stderr,
            )
            sub.close()
            sys.exit(1)

        sub.setsockopt(zmq.RCVTIMEO, max(1, int(remaining * 1000)))

        try:
            msg_bytes = sub.recv()
        except zmq.error.Again:
            continue  # Re-check the deadline at the top of the loop.
        except zmq.error.ZMQError as e:
            print(f"[nri-wait] Socket error: {e}", file=sys.stderr)
            sub.close()
            sys.exit(1)

        try:
            msg = json.loads(msg_bytes.decode())
        except (json.JSONDecodeError, UnicodeDecodeError):
            continue  # Ignore unparsable messages.

        print(f"[nri-wait] Received message: {msg}", file=sys.stderr)
        if msg.get("container_id") != container_id:
            continue

        if msg.get("status") == "done":
            print(
                f"[nri-wait] Build completed for container {container_id}",
                file=sys.stderr,
            )
            sub.close()
            return

        if msg.get("status") == "failed":
            sub.close()
            fail_build(container_id, msg.get("reason", "no reason given"))

        if msg.get("status") in ("progress", "building"):
            deadline = time.time() + timeout
            print(
                f"[nri-wait] Progress for container {container_id}, {timeout}s more",
                file=sys.stderr,
            )
