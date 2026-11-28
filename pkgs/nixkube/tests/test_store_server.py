# SPDX-License-Identifier: Apache-2.0

from pathlib import Path

import anyio
import pytest

from src import store_server
from src.constants import DEFAULT_DAEMON_SOCKET, StoreServer
from src.errors import BuildError
from src.store_server import (
    HostStoreRefusedError,
    PynixdVerdict,
    check_host_version,
    host_daemon_version,
    host_version_refusal,
    minor_version,
    parse_pynixd_verdict,
    prepare_socket,
    server_command,
    server_socket,
)
from src.subprocessing import SubprocessResult

NODE_SOCKET = "/nix/var/nix-csi/daemon-socket/socket"


def test_nix_daemon_listens_on_nix_socket_or_its_default() -> None:
    assert server_command(StoreServer.NIX_DAEMON)[:2] == ["nix", "daemon"]
    assert server_socket(StoreServer.NIX_DAEMON, {}) == DEFAULT_DAEMON_SOCKET
    assert server_socket(StoreServer.NIX_DAEMON, {"NIX_DAEMON_SOCKET_PATH": NODE_SOCKET}) == Path(NODE_SOCKET)


@pytest.mark.parametrize("server", [StoreServer.PYNIXD, StoreServer.PYNIXD_DAEMONLESS])
def test_pynixd_listens_where_the_manifest_says(server: StoreServer) -> None:
    assert server_command(server) == ["pynixd", "daemon"]
    assert server_socket(server, {"PYNIXD_UNIX_PATH": NODE_SOCKET}) == Path(NODE_SOCKET)


@pytest.mark.parametrize("server", [StoreServer.PYNIXD, StoreServer.PYNIXD_DAEMONLESS])
def test_pynixd_without_a_socket_refuses_to_start(server: StoreServer) -> None:
    with pytest.raises(RuntimeError, match="PYNIXD_UNIX_PATH"):
        server_socket(server, {"NIX_DAEMON_SOCKET_PATH": NODE_SOCKET})


@pytest.mark.parametrize(
    ("text", "version"),
    [
        ("nix (Nix) 2.35.2\n", (2, 35)),
        ("2.35.3", (2, 35)),
        ("nix (Determinate Nix 3.8.1) 2.30.0", (3, 8)),
        ("no version here", None),
    ],
)
def test_minor_version(text: str, version: tuple[int, int] | None) -> None:
    assert minor_version(text) == version


@pytest.mark.parametrize(
    ("ours", "host", "refused"),
    [
        # sg1, 2026-11-19: nixkube 2.35.2 on hosts with 2.35.3.
        ("nix (Nix) 2.35.2", "2.35.3", False),
        ("nix (Nix) 2.35.9", "2.35.3", False),
        ("nix (Nix) 2.34.0", "2.35.3", False),
        ("nix (Nix) 2.36.0", "2.35.3", True),
        ("nix (Nix) 3.0.0", "2.35.3", True),
        # Nothing to compare: no refusal on a guess.
        ("nix (Nix) 2.36.0", "", False),
    ],
)
def test_only_a_newer_nix_than_the_hosts_is_refused(ours: str, host: str, refused: bool) -> None:
    assert (host_version_refusal(ours, host) is not None) is refused


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def test_the_pynixd_verdict_is_read_from_its_answer() -> None:
    answer = '{"pynixd_nix": "2.35.2", "other_nix": "2.34.8", "refusal": "across 2.35"}\n'
    assert parse_pynixd_verdict(answer) == PynixdVerdict(pynixd_nix="2.35.2", refusal="across 2.35")
    assert parse_pynixd_verdict('{"pynixd_nix": "2.35.2", "other_nix": "2.35.3", "refusal": null}').refusal is None


@pytest.mark.anyio
async def test_no_daemon_at_the_hosts_socket_gives_no_version(tmp_path: Path) -> None:
    assert await host_daemon_version(f"unix://{tmp_path / 'socket'}") is None


def _stub_check(monkeypatch: pytest.MonkeyPatch, host: str | None, verdict: PynixdVerdict, nix: str) -> None:
    async def version() -> str | None:
        return host

    async def pynixd(_host: str) -> PynixdVerdict:
        return verdict

    async def captured(*_args: object, timeout: float | None = None) -> SubprocessResult:
        return SubprocessResult(returncode=0, stdout=nix, stderr="", combined=nix, elapsed=0.0)

    monkeypatch.setattr(store_server, "host_daemon_version", version)
    monkeypatch.setattr(store_server, "pynixd_verdict", pynixd)
    monkeypatch.setattr(store_server, "run_captured", captured)


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("server", "host", "verdict", "refused"),
    [
        # nix-daemon keeps its own rule and never reads pynixd's.
        (StoreServer.NIX_DAEMON, "2.35.3", PynixdVerdict("9.9", "never read"), False),
        (StoreServer.NIX_DAEMON, "2.34.8", PynixdVerdict("9.9", None), True),
        # Daemonless pynixd: pynixd's rule alone.
        (StoreServer.PYNIXD_DAEMONLESS, "2.34.8", PynixdVerdict("2.35.2", "across 2.35"), True),
        (StoreServer.PYNIXD_DAEMONLESS, "2.35.1", PynixdVerdict("2.35.2", None), False),
        # pynixd over a nix-daemon: both rules.
        (StoreServer.PYNIXD, "2.35.3", PynixdVerdict("2.35.2", None), False),
        (StoreServer.PYNIXD, "2.35.3", PynixdVerdict("2.36.0", None), True),
        (StoreServer.PYNIXD, "2.35.3", PynixdVerdict("2.35.2", "refused"), True),
        # No daemon at the host's socket: a warning, no refusal.
        (StoreServer.PYNIXD_DAEMONLESS, None, PynixdVerdict("2.35.2", "never read"), False),
    ],
)
async def test_each_server_takes_its_rule(
    monkeypatch: pytest.MonkeyPatch, server: StoreServer, host: str | None, verdict: PynixdVerdict, refused: bool
) -> None:
    _stub_check(monkeypatch, host, verdict, "nix (Nix) 2.35.2\n")
    if refused:
        with pytest.raises(HostStoreRefusedError, match=rf"{server.value} with Nix \S+ refuses the host daemon's Nix"):
            await check_host_version(server)
    else:
        await check_host_version(server)


def test_prepare_socket_makes_the_directory_and_drops_a_stale_socket(tmp_path: Path) -> None:
    socket = tmp_path / "nix-csi" / "daemon-socket" / "socket"
    prepare_socket(socket)
    assert socket.parent.is_dir()
    socket.write_text("stale")
    prepare_socket(socket)
    assert not socket.exists()


@pytest.fixture
def no_server_yet(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(store_server, "_answering", None)


@pytest.mark.anyio
@pytest.mark.usefixtures("no_server_yet")
async def test_a_build_waits_until_the_server_answers(tmp_path: Path) -> None:
    """sg1, 2026-11-28: NRI builds ran in the 1.5 s before the store server
    listened, and failed with "cannot connect"."""
    with anyio.move_on_after(0.3) as early:
        await store_server.wait_for_store_server()
    assert early.cancelled_caught, "a build went ahead before any server answered"

    async with anyio.create_task_group() as tg:
        tg.start_soon(store_server.announce_when_answering, str(tmp_path / "store"))
        with anyio.fail_after(5):
            await store_server.wait_for_store_server()


@pytest.mark.anyio
@pytest.mark.usefixtures("no_server_yet")
async def test_no_answer_within_the_deadline_is_a_build_error(tmp_path: Path) -> None:
    async with anyio.create_task_group() as tg:
        tg.start_soon(store_server.announce_when_answering, f"unix://{tmp_path / 'absent.sock'}")
        with pytest.raises(BuildError, match="did not answer"):
            await store_server.wait_for_store_server(timeout=0.3)
        tg.cancel_scope.cancel()


@pytest.mark.anyio
@pytest.mark.usefixtures("no_server_yet")
async def test_a_server_that_exits_is_waited_for_again(tmp_path: Path) -> None:
    await store_server.announce_when_answering(str(tmp_path / "store"))
    with anyio.fail_after(1):
        await store_server.wait_for_store_server()
    store_server.withdraw_answering()
    with anyio.move_on_after(0.3) as waited:
        await store_server.wait_for_store_server()
    assert waited.cancelled_caught, "a build went ahead while no server ran"
