"""Entry point for nri-wait OCI hook."""

import json
import os
import sys

import zmq

from . import OciState, check_build_status, connect_updates, wait_for_completion


def main() -> None:
    """Main entry point - orchestrates query and wait phases."""
    # OCI runtime passes container state as JSON on stdin for createRuntime hooks.
    oci_state: OciState = {}
    try:
        oci_state = json.load(sys.stdin)
        print(
            f"[nri-wait] OCI state: id={oci_state.get('id')} pid={oci_state.get('pid')} bundle={oci_state.get('bundle')}",
            file=sys.stderr,
        )
    # Anything at all. A hook that dies here says nothing about why, and the
    # missing 'id' below is the message the operator needs.
    except Exception as e:  # noqa: BLE001
        print(f"[nri-wait] Could not parse OCI state from stdin: {e}", file=sys.stderr)

    container_id = oci_state.get("id")
    if not container_id:
        print("[nri-wait] Error: OCI state missing 'id' field", file=sys.stderr)
        sys.exit(1)

    # Read from environment variables (set by OCI hook)
    try:
        query_socket = os.environ["NRI_QUERY_SOCKET"]
        pub_socket = os.environ["NRI_PUB_SOCKET"]
        timeout = int(os.environ.get("NRI_TIMEOUT", "30"))
    except KeyError as e:
        print(f"Error: Required environment variable not set: {e}", file=sys.stderr)
        sys.exit(1)
    except ValueError:
        print("Error: NRI_TIMEOUT must be a valid integer", file=sys.stderr)
        sys.exit(1)

    # Create ZeroMQ context
    context: zmq.Context[zmq.Socket[bytes]] = zmq.Context()

    def ask() -> bool:
        """Ask the daemon whether the build is done, and report our PID and bundle."""
        return check_build_status(context, query_socket, oci_state, timeout)

    try:
        # Phase 1: Subscribe first. See connect_updates for why the order matters.
        sub = connect_updates(context, pub_socket)

        # Phase 2: Query if build is already done (also registers the PID/bundle with nix-nri)
        if ask():
            print(
                f"[nri-wait] Build already completed for {container_id}",
                file=sys.stderr,
            )
            sub.close()
            return

        print(
            f"[nri-wait] Build pending for {container_id}, waiting for updates...",
            file=sys.stderr,
        )

        # Phase 3: Wait for completion
        wait_for_completion(sub, container_id, timeout, ask)
    finally:
        context.term()


if __name__ == "__main__":
    main()
