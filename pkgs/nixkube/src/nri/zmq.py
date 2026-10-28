# SPDX-License-Identifier: Apache-2.0

"""ZeroMQ server for NRI build coordination.

Why ZeroMQ instead of raw asyncio streams or queues?

1. **Cross-namespace IPC**: nri-wait runs inside the container's network namespace
   after the OCI hook fires. Unix domain sockets work across Linux namespaces at the
   filesystem level, and ZeroMQ's IPC transport uses exactly that. Replicating this
   cleanly with asyncio streams would require the same Unix socket plumbing but without
   ZeroMQ's framing, reconnection, and buffering guarantees.

2. **PUB/SUB heartbeat pump**: The build daemon publishes periodic progress messages
   so nri-wait can distinguish "build still running" from "daemon crashed". ZeroMQ's
   PUB/SUB gives us fan-out (multiple nri-wait processes can subscribe) and late-join
   semantics for free. Reimplementing this with asyncio would mean maintaining a set of
   writer streams and handling partial failures manually.

3. **Battle-tested**: pyzmq is a mature, well-maintained binding to the ZeroMQ C
   library. The socket lifecycle (bind/connect, context cleanup) is well-understood and
   the async support integrates cleanly with asyncio event loops.

Design:
- REP socket (`wait-req.sock`): nri-wait sends its PID/bundle and polls for build status.
- PUB socket (`wait-pub.sock`): build daemon broadcasts progress/done events; nri-wait
  subscribes and resets its timeout on each heartbeat.
"""

import json
from dataclasses import dataclass, field
from pathlib import Path

import anyio
import structlog
import zmq.asyncio
from cachetools import TTLCache

from .builds import BuildRegistry

logger = structlog.get_logger("nixkube.nri.zmq")


@dataclass
class ContainerInfo:
    """Mutable container metadata collected during build coordination."""

    pid: int | None = None
    bundle: str | None = None
    event: anyio.Event = field(default_factory=anyio.Event)


class ZeroMQServer:
    """Manages ZeroMQ PUB/REP sockets for build status coordination."""

    def __init__(self, socket_base_dir: str = "/nix/var/nixkube"):
        """Initialize ZeroMQ server (sockets not yet created)."""
        self.socket_base_dir = socket_base_dir
        self.context: zmq.asyncio.Context | None = None
        self.rep_socket: zmq.asyncio.Socket | None = None
        self.pub_socket: zmq.asyncio.Socket | None = None
        self.build_status = TTLCache[str, dict[str, str]](maxsize=10000, ttl=3600)
        self.pending_builds = BuildRegistry()
        # Container metadata from nri-wait, TTL-evicted to avoid unbounded growth
        self._container_info = TTLCache[str, ContainerInfo](maxsize=10000, ttl=3600)

    async def initialize(self) -> None:
        """Create ZeroMQ context and bind sockets."""
        logger.info("zmq_initializing", socket_base_dir=self.socket_base_dir)

        socket_dir = Path(self.socket_base_dir)
        try:
            socket_dir.mkdir(parents=True, exist_ok=True)
            logger.debug("zmq_socket_dir_ready", socket_dir=str(socket_dir))
        except Exception:
            logger.exception("zmq_socket_dir_failed")
            raise

        try:
            self.context = zmq.asyncio.Context()
            logger.debug("zmq_context_created")
        except Exception:
            logger.exception("zmq_context_failed")
            raise

        try:
            req_socket_path = socket_dir / "wait-req.sock"
            self.rep_socket = self.context.socket(zmq.REP)
            self.rep_socket.bind(f"ipc://{req_socket_path}")
            logger.info("zmq_rep_bound", path=str(req_socket_path))

            pub_socket_path = socket_dir / "wait-pub.sock"
            self.pub_socket = self.context.socket(zmq.PUB)
            self.pub_socket.bind(f"ipc://{pub_socket_path}")
            logger.info("zmq_pub_bound", path=str(pub_socket_path))
        except Exception:
            logger.exception("zmq_sockets_failed")
            self.shutdown()
            raise

        logger.info("zmq_initialized")

    def _get_info(self, container_id: str) -> ContainerInfo:
        """Return (creating if needed) the ContainerInfo for a container."""
        if container_id not in self._container_info:
            self._container_info[container_id] = ContainerInfo()
        return self._container_info[container_id]

    async def wait_for_pid(self, container_id: str, timeout: float = 30.0) -> tuple[int, str] | None:
        """Wait until nri-wait reports the container PID and bundle, then return (pid, bundle)."""
        log = logger.bind(container_id=container_id)
        info = self._get_info(container_id)
        with anyio.move_on_after(timeout):
            await info.event.wait()
        if not info.event.is_set():
            log.warning("zmq_pid_timeout")
            return None
        if info.pid is None or info.bundle is None:
            log.warning("zmq_pid_missing", pid=info.pid, bundle=info.bundle)
            return None
        return (info.pid, info.bundle)

    async def query_build_status(self, container_id: str) -> dict:
        """Return build status for a container."""
        if container_id in self.build_status:
            return self.build_status[container_id]
        elif container_id in self.pending_builds:
            return {"status": "pending"}
        else:
            return {"status": "unknown"}

    async def publish_build_progress(self, container_id: str) -> None:
        """Publish build progress heartbeat on PUB socket to reset nri-wait timeout."""
        if self.pub_socket is None:
            logger.warning("zmq_pub_not_initialized")
            return
        log = logger.bind(container_id=container_id)
        try:
            msg = json.dumps({"container_id": container_id, "status": "progress"})
            log.debug("zmq_publishing_progress")
            await self.pub_socket.send(msg.encode())
        except Exception:
            log.warning("zmq_publish_progress_failed", exc_info=True)

    async def publish_build_complete(self, container_id: str) -> None:
        """Publish build completion message on PUB socket."""
        if self.pub_socket is None:
            logger.warning("zmq_pub_not_initialized")
            return
        log = logger.bind(container_id=container_id)
        try:
            msg = json.dumps({"container_id": container_id, "status": "done"})
            log.debug("zmq_publishing_done")
            await self.pub_socket.send(msg.encode())
            log.info("zmq_published_done")
        except Exception:
            log.exception("zmq_publish_done_failed")

    async def publish_build_failed(self, container_id: str, reason: str) -> None:
        """Publish a build failure and its reason on the PUB socket."""
        if self.pub_socket is None:
            logger.warning("zmq_pub_not_initialized")
            return
        log = logger.bind(container_id=container_id)
        try:
            msg = json.dumps({"container_id": container_id, "status": "failed", "reason": reason})
            await self.pub_socket.send(msg.encode())
            log.info("zmq_published_failed")
        except Exception:
            log.exception("zmq_publish_failed_failed")

    async def start_request_handler(self) -> None:
        """Handle build status queries on REP socket (blocks until cancelled)."""
        if self.rep_socket is None:
            logger.warning("zmq_rep_not_initialized")
            return

        logger.info("zmq_rep_handler_starting")
        try:
            while True:
                logger.debug("zmq_rep_waiting")
                query_bytes = await self.rep_socket.recv()
                logger.debug("zmq_rep_received", bytes=len(query_bytes))
                try:
                    query = json.loads(query_bytes.decode())
                    container_id = query.get("id")
                    pid = query.get("pid")
                    bundle = query.get("bundle")
                    log = logger.bind(container_id=container_id)
                    if pid is not None and bundle is not None and container_id is not None:
                        info = self._get_info(container_id)
                        info.pid = pid
                        info.bundle = bundle
                        info.event.set()
                        log.debug("zmq_rep_stored_pid", pid=pid, bundle=bundle)
                    log.info("zmq_rep_query", pid=pid, bundle=bundle)

                    # A query carrying no id can only ever be "unknown":
                    # `query_build_status` looks the id up in two containers
                    # and falls through. Answering it here says so, instead
                    # of passing `None` into a `str` parameter and relying on
                    # both lookups missing.
                    status = (
                        {"status": "unknown"} if container_id is None else await self.query_build_status(container_id)
                    )
                    log.debug("zmq_rep_responding", status=status)
                    response = json.dumps(status)
                    await self.rep_socket.send(response.encode())
                    log.debug("zmq_rep_response_sent")
                except Exception:
                    logger.exception("zmq_rep_query_error")
                    await self.rep_socket.send(b'{"error":"internal error"}')
        except anyio.get_cancelled_exc_class():
            # Re-raised, not swallowed. A cancellation that stops here makes a
            # shutdown look like a clean return to whatever started this.
            logger.info("zmq_rep_handler_cancelled")
            raise
        except Exception:
            logger.exception("zmq_rep_handler_error")

    def shutdown(self) -> None:
        """Terminate ZeroMQ context."""
        if self.context is not None:
            self.context.term()
            logger.debug("zmq_context_terminated")
