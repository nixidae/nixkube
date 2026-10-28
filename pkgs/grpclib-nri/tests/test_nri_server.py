# SPDX-License-Identifier: Apache-2.0
"""Integration tests for grpclib-nri against the Go test server.

What is covered: the RegisterPlugin handshake, through to the runtime
calling Configure, and a clean start/close of `NriServer`.

What is not: Synchronize, CreateContainer and the rest of the plugin
lifecycle. `DummyPlugin` implements them, but the Go stub closes the
connection once Configure answers, so nothing here can reach them.
"""

import asyncio

import pytest
import structlog
from grpclib_nri import NriServer

from .conftest import HANDSHAKE_TIMEOUT, await_configure
from .dummy_plugin import DummyPlugin


@pytest.mark.asyncio
async def test_plugin_configure_called(nri_server: NriServer) -> None:
    """Test that Configure handler is called during registration."""
    logger = structlog.get_logger("test.plugin_configure")

    # Extract plugin from server
    plugin = nri_server.plugin
    assert isinstance(plugin, DummyPlugin)

    logger.info("configure_called", value=plugin.configure_called)

    # Configure should have been called during the handshake
    assert plugin.configure_called, "Configure handler was not called during registration"


@pytest.mark.asyncio
async def test_nri_server_lifecycle(
    test_server_bin,
    socket_path,
) -> None:
    """Test that NriServer can be started and stopped cleanly."""
    logger = structlog.get_logger("test.nri_lifecycle")

    # A fresh test server, not the fixture's: this test owns the close.
    proc = await asyncio.create_subprocess_exec(
        str(test_server_bin),
        "-socket",
        str(socket_path),
        "-timeout",
        "5s",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    # Wait for socket
    for _ in range(50):
        if socket_path.exists():
            break
        if proc.returncode is not None:
            stdout, stderr = await proc.communicate()
            pytest.fail(
                f"Test server exited prematurely (code {proc.returncode})\n"
                f"stdout: {stdout.decode()}\nstderr: {stderr.decode()}"
            )
        await asyncio.sleep(0.1)
    else:
        if proc.returncode is None:
            proc.terminate()
            await proc.wait()
        pytest.fail("Test server socket not created")

    # Create and start server
    plugin = DummyPlugin()
    server = NriServer(
        plugin,
        socket_path,
        plugin_name="test-lifecycle",
        plugin_idx="99",
    )

    logger.info("starting_server")
    server_task = asyncio.create_task(server.start())

    # Close only after the handshake really finished. Closing mid-handshake
    # tests a different thing, and a sleep decides which one at random.
    if not await await_configure(plugin):
        pytest.fail(f"no Configure within {HANDSHAKE_TIMEOUT}s")

    # Close it
    logger.info("closing_server")
    await server.close()

    # Wait for task to complete
    try:
        await asyncio.wait_for(server_task, timeout=2)
    except (TimeoutError, asyncio.CancelledError):
        logger.warning("server_task_exit_timeout")

    # Cleanup subprocess
    proc.terminate()
    try:
        await asyncio.wait_for(proc.wait(), timeout=2)
    except TimeoutError:
        proc.kill()
        await proc.wait()

    logger.info("server_lifecycle_ok")
