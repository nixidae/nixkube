# SPDX-License-Identifier: Apache-2.0
"""nix-node's readiness: the driver answers, and the node runs the nodeEnv its
deployment asks for.

**Not the CSI `Probe`.** livenessprobe (v2.18.0, `checkProbe`) serves one
endpoint, `/healthz`, and answers 500 for a failed Probe and for
`ready=false` alike. nix-node's livenessProbe reads that endpoint too. So a
not-ready Probe restarts the container, and a restart does not re-run
`appstarter-init`: the pod restarts for as long as the cache is down. This
endpoint has one reader, the readinessProbe.

**Behind is not Ready, and never a crash.** When `appstarter init` cannot
fetch the wanted nodeEnv, it seeds the image's copy, so a node whose cache is
down still serves volumes (issues #27, #55). That node goes on serving. It
only stops reporting Ready, so a DaemonSet rolling update stops at it
(nixidae/nixidae#1220).

An image older than this module serves no `/readyz`, and its probe fails as
well. That image's nodeEnv is behind by definition.
"""

from __future__ import annotations

import functools
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Final

import anyio
import huggorm
import structlog
from grpclib.client import Channel
from grpclib.exceptions import GRPCError, ProtocolError, StreamTerminatedError

from csi import csi_grpc, csi_pb2

from .constants import NIX_ROOT
from .nix.node_store import node_store_uri, open_node_store

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from anyio.abc import SocketStream
    from huggorm import AsyncStore

logger = structlog.get_logger("nixkube.readiness")

# What `appstarter run` execs nixkube out of, so its target is the nodeEnv
# this process runs.
RESULT_LINK: Final = NIX_ROOT / "nix/var/result"

CSI_SOCKET: Final = Path("/csi/nixkube/csi.sock")

# livenessprobe's own `--probe-timeout` default.
PROBE_TIMEOUT: Final = 3.0

# Below the readinessProbe's `timeoutSeconds`, so a slow client is dropped
# here and not counted twice.
REQUEST_TIMEOUT: Final = 4.0

MAX_REQUEST_HEAD: Final = 8192

PATH: Final = "/readyz"

_REASONS: Final = {200: "OK", 404: "Not Found", 503: "Service Unavailable"}


def wanted_for(spec: str, system: str) -> str | None:
    """The store path `spec` names for `system`, or `None` where it names none.

    `spec` is `APPSTARTER_WANTED`: one store path, or JSON from system to
    store path, as `appstarter`'s `store_path_for` reads it.

    Raises `ValueError` on JSON that does not parse.
    """
    spec = spec.strip()
    if not spec:
        return None
    if not spec.startswith("{"):
        return spec
    # JSON text that starts with "{" parses to an object or not at all. The
    # values come from a pod spec, so their type is checked, not trusted.
    by_system: dict[str, object] = json.loads(spec)
    path = by_system.get(system)
    return path if isinstance(path, str) else None


@dataclass(frozen=True, slots=True)
class NodeEnv:
    """The nodeEnv this node runs, and the one its deployment asks for.

    `None` is "cannot tell". A node that cannot tell is not behind: there is
    nothing to compare, and blocking every rollout on a missing variable is
    worse than the report it would give. `nixkube_appstarter_degraded` stays
    absent in that case, which is the signal there.
    """

    wanted: str | None
    running: str | None

    @property
    def behind(self) -> bool:
        return self.wanted is not None and self.running is not None and self.wanted != self.running


def read_node_env(spec: str, system: str, result_link: Path = RESULT_LINK) -> NodeEnv:
    """Read both nodeEnvs once. Neither changes while the process runs."""
    try:
        wanted = wanted_for(spec, system)
    except ValueError:
        logger.warning("appstarter_wanted_unreadable", system=system, exc_info=True)
        wanted = None
    try:
        running = os.readlink(result_link)
    except OSError:
        running = None
    return NodeEnv(wanted=wanted, running=running)


def log_node_env(env: NodeEnv, system: str) -> None:
    """The one log line about it: at start, and at warning when behind."""
    if env.behind:
        logger.warning("node_env_behind", running=env.running, wanted=env.wanted, system=system)
    elif env.wanted is None or env.running is None:
        logger.info("node_env_unknown", running=env.running, wanted=env.wanted, system=system)
    else:
        logger.info("node_env_current", running=env.running, system=system)


@dataclass(frozen=True, slots=True)
class Answer:
    status: int
    body: str


def answer(env: NodeEnv, driver_error: str | None, store_error: str | None = None) -> Answer:
    """What `/readyz` says, with every reason it is not ready in the body.

    The kubelet's event says only "statuscode: 503". The body is for a
    person who asks the endpoint directly.
    """
    reasons: list[str] = []
    if env.behind:
        reasons.append(f"behind: this node runs {env.running}, and the deployment asks for {env.wanted}")
    if driver_error is not None:
        reasons.append(f"driver: {driver_error}")
    if store_error is not None:
        reasons.append(f"store: {store_error}")
    if reasons:
        return Answer(503, "".join(f"{reason}\n" for reason in reasons))
    return Answer(200, "ok\n")


async def probe_driver(socket: Path = CSI_SOCKET, timeout: float = PROBE_TIMEOUT) -> str | None:
    """Ask the driver's `Probe` over its socket, as livenessprobe does.

    Through the socket and not in process: an unlinked socket leaves the
    driver running and unreachable, and only a connect sees it.

    Returns `None` when the driver is ready, and the reason when it is not.
    """
    channel = Channel(path=str(socket))
    try:
        with anyio.fail_after(timeout):
            reply = await csi_grpc.IdentityStub(channel).Probe(csi_pb2.ProbeRequest())
    except TimeoutError:
        return f"no answer within {timeout:.0f}s"
    except (OSError, GRPCError, ProtocolError, StreamTerminatedError) as error:
        return f"{type(error).__name__}: {error}"
    finally:
        channel.close()
    # CSI: "If not present, the caller SHALL assume that the plugin is in a
    # ready state".
    if reply.HasField("ready") and not reply.ready.value:
        return "Probe answered not ready"
    return None


async def probe_store(store: AsyncStore, timeout: float = PROBE_TIMEOUT) -> str | None:
    """Whether the node's store server answers: a daemon-protocol handshake.

    `RemoteStore::getVersion` takes a connection to answer, so a server that
    has not bound its socket yet, or died, fails here. The CSI Probe alone
    reported Ready before the store socket existed (measured in the guest).

    Returns `None` when the store answers, and the reason when it does not.
    """
    try:
        with anyio.fail_after(timeout):
            version = await store.get_version()
    except TimeoutError:
        return f"no answer within {timeout:.0f}s"
    except huggorm.errors.NixError as error:
        return f"{type(error).__name__}: {error}"
    if version is None:
        return "the store server named no Nix version"
    return None


async def _probe_fresh_store(store_uri: str) -> str | None:
    """`probe_store` on a store of its own: a probe that fails must not
    leave the next one a store Nix refuses ("previously failed")."""
    async with open_node_store(store_uri) as store:
        return await probe_store(store)


async def _probes(socket: Path, store_uri: str) -> tuple[str | None, str | None]:
    """Both probes at once: each may take PROBE_TIMEOUT, and a request has REQUEST_TIMEOUT."""
    found: dict[str, str | None] = {}

    async def run(key: str, probe: Callable[[], Awaitable[str | None]]) -> None:
        found[key] = await probe()

    async with anyio.create_task_group() as tg:
        tg.start_soon(run, "driver", functools.partial(probe_driver, socket))
        tg.start_soon(run, "store", functools.partial(_probe_fresh_store, store_uri))
    return found["driver"], found["store"]


def request_path(head: bytes) -> str | None:
    """The path of a `GET` request line, without its query, or `None`."""
    line = head.split(b"\r\n", 1)[0].decode("latin-1")
    parts = line.split(" ")
    if len(parts) != 3 or parts[0] != "GET":
        return None
    return parts[1].split("?", 1)[0]


def encode(result: Answer) -> bytes:
    body = result.body.encode()
    head = (
        f"HTTP/1.1 {result.status} {_REASONS.get(result.status, '')}\r\n"
        "Content-Type: text/plain; charset=utf-8\r\n"
        f"Content-Length: {len(body)}\r\n"
        "Connection: close\r\n"
        "\r\n"
    )
    return head.encode() + body


async def _read_head(stream: SocketStream) -> bytes:
    head = b""
    while b"\r\n\r\n" not in head and len(head) < MAX_REQUEST_HEAD:
        head += await stream.receive(MAX_REQUEST_HEAD)
    return head


async def handle(stream: SocketStream, *, env: NodeEnv, socket: Path, store_uri: str) -> None:
    """Answer one request on `stream`, then close it."""
    async with stream:
        try:
            with anyio.fail_after(REQUEST_TIMEOUT):
                path = request_path(await _read_head(stream))
                if path == PATH:
                    result = answer(env, *(await _probes(socket, store_uri)))
                else:
                    result = Answer(404, "not found\n")
                await stream.send(encode(result))
        # A client that goes away or stalls is its own problem, and must not
        # end the listener: one request's exception would stop readiness.
        except (TimeoutError, OSError, anyio.EndOfStream, anyio.BrokenResourceError, anyio.ClosedResourceError):
            logger.debug("readiness_request_dropped", exc_info=True)


async def serve(env: NodeEnv, port: int, socket: Path = CSI_SOCKET, store_uri: str | None = None) -> None:
    """Serve `/readyz` on every interface until cancelled.

    No address: anyio then binds one dual-stack socket, or one per family on
    a kernel without dual stack.

    A port that cannot be bound is logged and not raised. The pod then never
    reports Ready, which is true, and the driver keeps serving.
    """
    try:
        listener = await anyio.create_tcp_listener(local_port=port)
    except OSError:
        logger.warning("readiness_server_failed", port=port, exc_info=True)
        return
    logger.info("readiness_serving", port=port, path=PATH)
    await listener.serve(functools.partial(handle, env=env, socket=socket, store_uri=store_uri or node_store_uri()))
