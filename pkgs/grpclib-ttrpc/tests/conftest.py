# SPDX-License-Identifier: Apache-2.0
"""Pytest configuration and fixtures for grpclib_ttrpc tests."""

import asyncio
import os
import shutil
import sys
import tempfile
from collections.abc import AsyncGenerator, Generator
from pathlib import Path

import pytest
import pytest_asyncio
import structlog

# Ensure tests/ is on sys.path so helpers.py and dummy_pb2.py are importable.
_here = os.path.dirname(os.path.abspath(__file__))
if _here not in sys.path:
    sys.path.insert(0, _here)


@pytest.fixture(scope="session")
def test_server_bin() -> Path:
    """Get the path to the ttrpc-test-server binary from TTRPC_TEST_SERVER env var."""
    bin_path = os.environ.get("TTRPC_TEST_SERVER")
    if not bin_path:
        pytest.fail("TTRPC_TEST_SERVER environment variable not set")

    path = Path(bin_path)
    if not path.exists():
        pytest.fail(f"TTRPC_TEST_SERVER binary does not exist: {bin_path}")

    return path


@pytest.fixture
def socket_path() -> Generator[Path]:
    """Create a unique socket path for each test.

    Uses /tmp to avoid "AF_UNIX path too long" error when running in Nix build
    (pytest's tmp_path is too deep in the Nix store, exceeding 108-byte limit).

    A directory per test, because one path per process is not unique: every
    test bound the same name, so a Go server still exiting from the previous
    test owned the socket the next one connected to. Same shape as
    grpclib-nri's fixture.
    """
    tmp_dir = Path(tempfile.mkdtemp(prefix="ttrpc", dir="/tmp"))
    try:
        yield tmp_dir / "s"
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


@pytest_asyncio.fixture
async def test_server_process(test_server_bin: Path, socket_path: Path) -> AsyncGenerator[asyncio.subprocess.Process]:
    """Start the Go TTRPC test server and yield the process.

    Waits for the server to be ready by checking if the socket exists.

    A server that does not come up is a failure, never a skip. The binary is
    a store path the derivation names, so there is no environment in which
    it is legitimately absent. Measured: with `TTRPC_TEST_SERVER` pointing at
    a binary that exits 1, a skip here gave `41 passed, 21 skipped` and the
    derivation succeeded -- the whole Go-interop file disappeared and CI
    stayed green.
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
