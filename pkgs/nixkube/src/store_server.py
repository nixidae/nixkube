# SPDX-License-Identifier: Apache-2.0

"""The node's store server: a supervised subprocess on the node's store socket.

`STORE_SERVER` picks the server (nixidae/nixidae#1661). `nix daemon` logs
internal-json, which this module forwards through structlog. pynixd logs
through its own structlog, to the container's stderr. Either one restarts on
exit, with crash-loop detection via CrashLoopTracker.

On a host-store node the server serves the host's /nix on a socket of its own,
`NIX_DAEMON_SOCKET_PATH`, which every nix call in the pod reads too. Before it
starts, `check_host_version` asks the host daemon its Nix version and refuses
a server whose writes the host's Nix cannot read: a newer nix-daemon migrates
the database forward (issue #25), and pynixd states its own rule
(`pynixd shares-store`, nixidae/nixidae#1528).
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Final

import anyio
import huggorm
import structlog

from .constants import DEFAULT_DAEMON_SOCKET, HOST_STORE, STORE_SERVER, StoreServer
from .subprocessing import iter_lines, run_captured
from .supervision import CrashLoopTracker

if TYPE_CHECKING:
    from collections.abc import Mapping

    from anyio.abc import ByteReceiveStream

logger = structlog.get_logger("nixkube.store_server")
# What `nix daemon` itself says, apart from the supervisor's own events, so a
# level can quiet one and keep the other.
nix_logger = structlog.get_logger("nixkube.store_server.nix")

# ANSI escape sequence pattern for stripping terminal colour codes
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


# Nix verbosity: 0=error, 1=warn, 2=notice, 3=info, 4=talkative, 5=chatty, 6=debug, 7=vomit
def _nix_level_to_structlog(verbosity: int) -> str:
    if verbosity <= 0:
        return "error"
    if verbosity == 1:
        return "warning"
    if verbosity <= 3:
        return "info"
    return "debug"


# The host's own daemon, at Nix's default socket for /nix.
HOST_DAEMON: Final = f"unix://{DEFAULT_DAEMON_SOCKET}"

_VERSION_RE = re.compile(r"(\d+)\.(\d+)")


class HostStoreRefusedError(RuntimeError):
    pass


def minor_version(text: str) -> tuple[int, int] | None:
    """(major, minor) of the first version in `text`: `nix (Nix) 2.35.2`."""
    found = _VERSION_RE.search(text)
    return (int(found[1]), int(found[2])) if found else None


def host_version_refusal(ours: str, host: str) -> str | None:
    """Why nixkube's nix-daemon must not open the host's store, or None. Nix
    migrates the database only forward, so only a newer Nix of ours can
    leave it unreadable for the host."""
    mine, theirs = minor_version(ours), minor_version(host)
    if mine is None or theirs is None or mine <= theirs:
        return None
    return f"nixkube's Nix ({ours.strip()}) is newer than the host daemon's ({host.strip()})"


async def host_daemon_version(uri: str = HOST_DAEMON) -> str | None:
    """The host daemon's Nix version from its handshake, or None when no
    daemon answers at `uri`. The one call the node makes to the host's daemon."""
    store = huggorm.AsyncStore(uri)
    try:
        return await store.get_version()
    except huggorm.errors.NixError as e:
        logger.warning("host_daemon_version_unknown", store=uri, error=str(e))
        return None
    finally:
        await store.aclose()


@dataclass(frozen=True, slots=True)
class PynixdVerdict:
    """The answer of `pynixd shares-store`. pynixd states the rule; nixkube keeps no copy of it."""

    pynixd_nix: str
    refusal: str | None


def parse_pynixd_verdict(stdout: str) -> PynixdVerdict:
    answer = json.loads(stdout)
    return PynixdVerdict(pynixd_nix=str(answer["pynixd_nix"]), refusal=answer["refusal"])


async def pynixd_verdict(host: str) -> PynixdVerdict:
    """Whether the node's pynixd may write the database that the host's Nix of
    version `host` opens. Exit 1 is a refusal, with its answer on stdout."""
    result = await run_captured("pynixd", "shares-store", host, timeout=60)
    if result.returncode not in (0, 1) or not result.stdout.strip():
        raise RuntimeError(f"`pynixd shares-store {host}` failed with {result.returncode}: {result.stderr.strip()}")
    return parse_pynixd_verdict(result.stdout)


async def check_host_version(server: StoreServer = STORE_SERVER) -> None:
    """Refuse to start `server` on the host's store when the host's Nix cannot
    read what the server writes. A host with no daemon at the default socket
    gives nothing to compare.

    nix-daemon: no Nix newer than the host's. pynixd: pynixd's own rule. A
    pynixd with a nix-daemon under it takes both, because that daemon
    migrates the database too. The server's own version comes from its
    binary: huggorm's `nix_version` names the Nix that huggorm links, which
    need not be the one that serves."""
    host = await host_daemon_version()
    if host is None:
        return
    refusals: list[str] = []
    if server is StoreServer.NIX_DAEMON:
        # `nix (Nix) 2.35.2`: the last word is `nixVersion`, what a handshake sends.
        printed = (await run_captured("nix", "--version", timeout=30)).stdout.split()
        ours = printed[-1] if printed else ""
    else:
        verdict = await pynixd_verdict(host)
        ours = verdict.pynixd_nix
        if verdict.refusal is not None:
            refusals.append(verdict.refusal)
    if server is not StoreServer.PYNIXD_DAEMONLESS:
        refusal = host_version_refusal(ours, host)
        if refusal is not None:
            refusals.append(refusal)
    logger.info("host_daemon_version", server=server.value, ours=ours, host=host, refused=bool(refusals))
    if refusals:
        raise HostStoreRefusedError(
            f"{server.value} with Nix {ours} refuses the host daemon's Nix {host}: {'; '.join(refusals)}"
        )


def prepare_socket(path: Path) -> None:
    """Nix binds the socket and creates no directory for it, and a socket
    file left by the last daemon makes the bind fail."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.unlink(missing_ok=True)


def server_command(server: StoreServer) -> list[str]:
    """The argv of `server`.

    The manifest configures each server, from the same option that sets
    `STORE_SERVER`: Nix's daemon reads `NIX_DAEMON_SOCKET_PATH` and nix.conf,
    pynixd reads `PYNIXD_UNIX_PATH` and `PYNIXD_CONFIG`, which says whether a
    Nix daemon runs under it. Both inherit this process's environment.
    """
    if server is StoreServer.NIX_DAEMON:
        return ["nix", "daemon", "--store", "local", "--log-format", "internal-json", "--debug"]
    return ["pynixd", "daemon"]


def server_socket(server: StoreServer, environ: Mapping[str, str]) -> Path:
    """The socket `server` listens on, as the manifest set it."""
    name = "NIX_DAEMON_SOCKET_PATH" if server is StoreServer.NIX_DAEMON else "PYNIXD_UNIX_PATH"
    value = environ.get(name)
    if value:
        return Path(value)
    if server is StoreServer.NIX_DAEMON:
        return DEFAULT_DAEMON_SOCKET
    raise RuntimeError(f"STORE_SERVER={server.value} needs PYNIXD_UNIX_PATH: pynixd's default is not the node's socket")


async def supervise_store_server() -> None:
    """Supervised loop for the store server subprocess.

    Launches the server, forwards nix daemon's logs, and restarts on exit.
    Raises CrashLoopError after too many rapid restarts.
    """
    tracker = CrashLoopTracker(max_restarts=5, window=60.0, name=STORE_SERVER.value)
    if HOST_STORE:
        if not os.environ.get("NIX_DAEMON_SOCKET_PATH"):
            raise RuntimeError("a host-store node needs NIX_DAEMON_SOCKET_PATH: the default socket is the host's")
        await check_host_version()
    socket = server_socket(STORE_SERVER, os.environ)

    while True:
        prepare_socket(socket)
        logger.info("store_server_starting", server=STORE_SERVER.value, socket=str(socket))
        if STORE_SERVER is StoreServer.NIX_DAEMON:
            async with await anyio.open_process(
                server_command(STORE_SERVER),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            ) as proc:
                assert proc.stdout is not None and proc.stderr is not None
                async with anyio.create_task_group() as tg:
                    tg.start_soon(_pipe_nix_logs, proc.stdout)
                    tg.start_soon(_pipe_nix_logs, proc.stderr)
                rc = await proc.wait()
        else:
            async with await anyio.open_process(server_command(STORE_SERVER), stdout=None, stderr=None) as proc:
                rc = await proc.wait()

        logger.warning("store_server_exited", server=STORE_SERVER.value, returncode=rc)
        tracker.record_and_check()
        logger.info("store_server_restarting", backoff_seconds=1)
        await anyio.sleep(1)


async def _pipe_nix_logs(stream: ByteReceiveStream) -> None:
    """Forward nix daemon log lines through structlog.

    Lines prefixed with `@nix ` carry internal-json structured data.
    Other lines are logged as debug.
    """
    try:
        async for raw_line in iter_lines(stream):
            line = raw_line.rstrip()
            if line.startswith("@nix "):
                payload = line.removeprefix("@nix ")
                try:
                    data = json.loads(payload)
                except json.JSONDecodeError:
                    nix_logger.debug("nix_daemon_log", line=line)
                    continue

                action = data.get("action", "")
                if action == "msg":
                    verbosity = data.get("verbosity", 2)
                    level = _nix_level_to_structlog(verbosity)
                    msg = _ANSI_RE.sub("", data.get("msg", "")).strip()
                    getattr(nix_logger, level)("nix_daemon", msg=msg)
                else:
                    # start / stop / result activity traces
                    nix_logger.debug(
                        "nix_daemon_activity",
                        **{k: v for k, v in data.items() if k != "action"},
                        action=action,
                    )
            else:
                nix_logger.debug("nix_daemon_log", line=line)
    except Exception:
        logger.exception("nix_daemon_log_error")
