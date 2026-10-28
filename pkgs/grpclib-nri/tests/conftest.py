# SPDX-License-Identifier: Apache-2.0
"""Pytest fixtures for grpclib-nri testing."""

import asyncio
import shutil
import tempfile
from collections.abc import AsyncGenerator, Generator
from pathlib import Path

import pytest
import pytest_asyncio
import structlog
from grpclib_nri import NriServer

from .dummy_plugin import DummyPlugin

# How long the RegisterPlugin -> Configure round trip may take, and how often
# to look. The handshake is two frames over a local Unix socket; the budget is
# for a loaded runner, not for the protocol.
HANDSHAKE_TIMEOUT = 10.0
_POLL = 0.02


async def await_configure(plugin: DummyPlugin) -> bool:
    """Wait until the runtime has called the plugin's Configure handler.

    The runtime calls Configure only after RegisterPlugin succeeds, so this
    flag is the first observable that says the connection is usable. Waiting
    for it beats sleeping: a sleep is slow when the handshake is fast, and
    silent when it never happens.
    """
    for _ in range(int(HANDSHAKE_TIMEOUT / _POLL)):
        if plugin.configure_called:
            return True
        await asyncio.sleep(_POLL)
    return False


@pytest.fixture(scope="session")
def test_server_bin() -> Path:
    """Get the path to the nri-test-server binary from NRI_TEST_SERVER env var."""
    import os

    bin_path = os.environ.get("NRI_TEST_SERVER")
    if not bin_path:
        pytest.fail("NRI_TEST_SERVER environment variable not set")

    path = Path(bin_path)
    if not path.exists():
        pytest.fail(f"NRI_TEST_SERVER binary does not exist: {bin_path}")

    return path


@pytest.fixture
def socket_path() -> Generator[Path]:
    """Create a unique socket path for each test.

    Uses /tmp directly instead of pytest's tmp_path because Unix sockets have
    a 108-byte path limit, and Nix build sandboxes set TMPDIR to a long path
    that causes bind() to fail with EINVAL.
    """
    tmp_dir = Path(tempfile.mkdtemp(prefix="nri", dir="/tmp"))
    try:
        yield tmp_dir / "s"
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


@pytest_asyncio.fixture
async def test_server_process(test_server_bin: Path, socket_path: Path) -> AsyncGenerator[asyncio.subprocess.Process]:
    """Start the Go NRI test server and yield the process.

    Waits for the server to be ready by checking if the socket exists.

    A server that does not come up is a failure, never a skip. The binary is
    a store path the derivation names, so there is no environment in which it
    is legitimately absent. `test_nri_server_lifecycle` starts its own copy
    and already calls `pytest.fail` for the same two cases.
    """
    logger = structlog.get_logger("test.server_process")
    logger.info("starting_test_server", path=str(test_server_bin))

    proc = await asyncio.create_subprocess_exec(
        str(test_server_bin),
        "-socket",
        str(socket_path),
        "-timeout",
        "5s",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    # Wait for socket to be created (max 5 seconds)
    max_retries = 50
    for _ in range(max_retries):
        if socket_path.exists():
            logger.info("server_ready", socket=str(socket_path))
            break
        if proc.returncode is not None:
            stdout, stderr = await proc.communicate()
            logger.error(
                "server_exited_prematurely",
                returncode=proc.returncode,
                stdout=stdout.decode(),
                stderr=stderr.decode(),
            )
            pytest.fail(
                f"Test server exited prematurely (code {proc.returncode})\n"
                f"stdout: {stdout.decode()}\nstderr: {stderr.decode()}"
            )
        await asyncio.sleep(0.1)
    else:
        proc.terminate()
        await proc.wait()
        pytest.fail(f"Test server did not create {socket_path} within 5s")

    yield proc

    # Cleanup
    if proc.returncode is None:
        proc.terminate()
        try:
            await asyncio.wait_for(proc.wait(), timeout=2)
        except TimeoutError:
            logger.warning("server_terminate_timeout")
            proc.kill()
            await proc.wait()


@pytest_asyncio.fixture
async def nri_server(test_server_process: asyncio.subprocess.Process, socket_path: Path) -> AsyncGenerator[NriServer]:
    """Create and start an NriServer connected to the test server.

    The test server is already running and listening on socket_path.
    We create a dummy plugin and start the NriServer to connect to it.
    """
    logger = structlog.get_logger("test.nri_server")

    assert test_server_process.returncode is None, "Test server should still be running"
    plugin = DummyPlugin()
    server = NriServer(
        plugin,
        socket_path,
        plugin_name="test-plugin",
        plugin_idx="42",
    )

    logger.info("starting_nri_server", socket=str(socket_path))

    # Start server in background task
    server_task = asyncio.create_task(server.start())

    if not await await_configure(plugin):
        await server.close()
        pytest.fail(f"the runtime never called Configure within {HANDSHAKE_TIMEOUT}s")
    logger.info("handshake_done")

    try:
        yield server
    finally:
        logger.info("closing_nri_server")
        await server.close()

        # Give background task time to exit
        try:
            await asyncio.wait_for(server_task, timeout=2)
        except (TimeoutError, asyncio.CancelledError):
            logger.warning("server_task_exit_timeout")
