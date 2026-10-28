# SPDX-License-Identifier: Apache-2.0
"""`nix-store --dump-db | nix-store --load-db`, without a shell to hold the pipe.

The failure this file exists for is silent and has no deadline around it: the
consumer reads until EOF, and EOF arrives only when every writing descriptor
is shut. The parent holds one of those. Keep it open and `init_database` never
returns, which stops the mount that called it.

Real processes and a real pipe. A fake reader answers EOF whenever it likes,
so it cannot fail the way the kernel does.
"""

from __future__ import annotations

import sys
from pathlib import Path

import anyio
import pytest

from src.nix.database import pipe_commands

# `sys.executable`, not "python3": the environment a test runs in is not
# guaranteed to have one on PATH, and the env case below replaces PATH.
PY = sys.executable
WRITE_STDIN_TO = "import sys, pathlib; pathlib.Path(sys.argv[1]).write_text(sys.stdin.read())"
READ_ENV_TO = "import os, sys, pathlib; sys.stdin.read(); pathlib.Path(sys.argv[1]).write_text(os.environ['PROBE'])"


@pytest.mark.asyncio
class TestThePipe:
    async def test_the_consumer_reads_what_the_producer_wrote(self, tmp_path: Path) -> None:
        out = tmp_path / "out.txt"

        with anyio.fail_after(30):
            codes = await pipe_commands(
                [PY, "-c", "print('hello')"],
                [PY, "-c", WRITE_STDIN_TO, str(out)],
                timeout=30,
            )

        assert codes == (0, 0)
        assert out.read_text() == "hello\n"

    async def test_a_payload_larger_than_the_pipe_buffer_still_ends(self, tmp_path: Path) -> None:
        """A pipe holds 64KB. Anything past it needs both ends running at once,
        which is what makes this a pipe and not a buffer with two steps."""
        out = tmp_path / "big.txt"

        with anyio.fail_after(30):
            codes = await pipe_commands(
                [PY, "-c", "print('x' * 300000)"],
                [PY, "-c", WRITE_STDIN_TO, str(out)],
                timeout=30,
            )

        assert codes == (0, 0)
        assert out.stat().st_size == 300001

    async def test_the_consumer_takes_the_environment_it_is_given(self, tmp_path: Path) -> None:
        """`init_database` puts `NIX_STATE_DIR` here, and that is the whole
        reason the second command writes somewhere other than the node's own
        store."""
        out = tmp_path / "env.txt"

        with anyio.fail_after(30):
            codes = await pipe_commands(
                [PY, "-c", "print('ignored')"],
                [
                    PY,
                    "-c",
                    READ_ENV_TO,
                    str(out),
                ],
                timeout=30,
                consumer_env={"PROBE": "reached"},
            )

        assert codes == (0, 0)
        assert out.read_text() == "reached"

    async def test_a_failing_producer_is_reported_and_not_swallowed(self, tmp_path: Path) -> None:
        """The consumer still exits 0: it read an empty stream and was happy.
        Only the producer's code says the dump never happened."""
        out = tmp_path / "empty.txt"

        with anyio.fail_after(30):
            codes = await pipe_commands(
                [PY, "-c", "raise SystemExit(3)"],
                [PY, "-c", WRITE_STDIN_TO, str(out)],
                timeout=30,
            )

        assert codes == (3, 0)
        assert out.read_text() == ""

    async def test_a_pipe_that_never_ends_raises_rather_than_hanging(self, tmp_path: Path) -> None:
        """The deadline `init_database` needs.

        This runs inside `NodePublishVolume` and nothing above it gives up,
        so without a bound a stuck `nix-store` leaves the pod waiting for
        ever and kubelet with nothing to retry.
        """
        with anyio.fail_after(30):
            with pytest.raises(TimeoutError):
                await pipe_commands(
                    [PY, "-c", "import time; time.sleep(60)"],
                    [PY, "-c", "import sys; sys.stdin.read()"],
                    timeout=0.2,
                )
