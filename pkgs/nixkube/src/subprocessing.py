# SPDX-License-Identifier: Apache-2.0

import logging  # for log level constants (logging.DEBUG, logging.NOTSET, etc.)
import shlex
import subprocess
import time
from collections.abc import AsyncIterator
from typing import NamedTuple

import anyio
import structlog
from anyio.abc import ByteReceiveStream

from .errors import CommandTimeoutError, SubprocessError
from .metrics import SUBPROCESS_CALLS, SUBPROCESS_DURATION, command_label

logger = structlog.get_logger("nixkube.subprocessing")


class SubprocessResult(NamedTuple):
    returncode: int
    stdout: str
    stderr: str
    combined: str
    elapsed: float


async def try_captured(*args, timeout: float | None = None) -> SubprocessResult:
    """Run a command, capture output, and raise SubprocessError if it fails.

    Args:
        *args: Command and arguments
        timeout: Optional timeout in seconds (raises CommandTimeoutError on timeout)

    Returns:
        SubprocessResult with stdout, stderr, combined output, and elapsed time

    Raises:
        SubprocessError: If the command returns non-zero exit code
        CommandTimeoutError: If timeout is exceeded
    """
    result = await run_captured(*args, timeout=timeout)
    if result.returncode != 0:
        raise SubprocessError(
            result.returncode,
            result.stdout,
            result.stderr,
            result.combined,
            list(args),
        )
    return result


async def try_console(*args, log_level: int = logging.DEBUG, timeout: float | None = None) -> SubprocessResult:
    """Run a command with output forwarded to logs, and raise SubprocessError if it fails.

    Args:
        *args: Command and arguments
        log_level: Logging level for output (default DEBUG)
        timeout: Optional timeout in seconds (raises CommandTimeoutError on timeout)

    Returns:
        SubprocessResult with stdout, stderr, combined output, and elapsed time

    Raises:
        SubprocessError: If the command returns non-zero exit code
        CommandTimeoutError: If timeout is exceeded
    """
    result = await run_console(*args, log_level=log_level, timeout=timeout)
    if result.returncode != 0:
        raise SubprocessError(
            result.returncode,
            result.stdout,
            result.stderr,
            result.combined,
            list(args),
        )
    return result


async def run_captured(*args, timeout: float | None = None) -> SubprocessResult:
    """Run a command and capture output without raising on non-zero exit.

    Args:
        *args: Command and arguments
        timeout: Optional timeout in seconds (raises CommandTimeoutError on timeout)

    Returns:
        SubprocessResult with stdout, stderr, combined output, returncode, and elapsed time
    """
    return await run_console(*args, log_level=logging.NOTSET, timeout=timeout)


async def run_console(*args, log_level: int = logging.DEBUG, timeout: float | None = None) -> SubprocessResult:
    """Run a command with output forwarded to logs, without raising on non-zero exit.

    Args:
        *args: Command and arguments
        log_level: Logging level for output (default DEBUG)
        timeout: Optional timeout in seconds (raises CommandTimeoutError on timeout)

    Returns:
        SubprocessResult with stdout, stderr, combined output, returncode, and elapsed time
    """
    start_time = time.perf_counter()
    log_command(*args, log_level=log_level)
    label = command_label(args)

    stdout_data: list[str] = []
    stderr_data: list[str] = []
    combined_data: list[str] = []

    returncode: int | None = None
    # `move_on_after` and not `fail_after`: the deadline is then identified by
    # the scope that caught it, not by catching `TimeoutError`. Anything
    # inside that raises its own `TimeoutError` -- a socket, a nested
    # deadline -- used to be reported here as this command timing out, with
    # return code 124 and a story that never happened.
    with anyio.move_on_after(timeout) as scope:
        async with await anyio.open_process(
            [str(arg) for arg in args],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        ) as proc:
            # Both are PIPE above, so neither is None.
            assert proc.stdout is not None and proc.stderr is not None
            # Read both while the process runs. A pipe that nobody drains
            # fills at 64KB and stops the child there, so this is what lets
            # a chatty command finish at all -- and reading them together is
            # what makes `combined` interleaved rather than concatenated.
            async with anyio.create_task_group() as tg:
                tg.start_soon(_read_stream, proc.stdout, stdout_data, combined_data, log_level)
                tg.start_soon(_read_stream, proc.stderr, stderr_data, combined_data, log_level)
            # Not raised on non-zero: try_captured/try_console decide that.
            returncode = await proc.wait()

    if scope.cancelled_caught:
        # Return code 124 is the conventional one for a timeout.
        SUBPROCESS_CALLS.labels(command=label, result="timeout").inc()
        SUBPROCESS_DURATION.labels(command=label).observe(time.perf_counter() - start_time)
        raise CommandTimeoutError(
            returncode=124,
            stdout="\n".join(stdout_data).strip(),
            stderr="\n".join(stderr_data).strip(),
            combined="\n".join(combined_data).strip(),
            command=list(args),
        )

    assert returncode is not None
    elapsed_time = time.perf_counter() - start_time
    SUBPROCESS_CALLS.labels(command=label, result="ok" if returncode == 0 else "error").inc()
    SUBPROCESS_DURATION.labels(command=label).observe(elapsed_time)
    cmd_str = shlex.join([str(arg) for arg in args])

    # Log all command timings for profiling (skip if NOTSET = silent capture)
    if log_level != logging.NOTSET:
        logger.log(
            log_level,
            "command_completed",
            elapsed_time=round(elapsed_time, 3),
            returncode=returncode,
            command=cmd_str,
        )

    # Also log slow commands at INFO regardless of caller's log_level
    if elapsed_time > 5:
        logger.info("slow_command", elapsed_time=round(elapsed_time, 3), command=cmd_str)

    return SubprocessResult(
        returncode,
        "\n".join(stdout_data).strip(),
        "\n".join(stderr_data).strip(),
        "\n".join(combined_data).strip(),
        elapsed_time,
    )


async def iter_lines(stream: ByteReceiveStream) -> AsyncIterator[str]:
    """The stream's lines, split here and with no length limit.

    `nix path-info --all --json` writes the whole store as one line, and
    300,000 characters is an ordinary size for it. A reader that caps a line
    -- asyncio's `StreamReader` raises `ValueError` past 64KB -- has to drain
    and rejoin the rest, which is a second code path that only the giant line
    ever takes.
    """
    pending = b""
    async for chunk in stream:
        pending += chunk
        *lines, pending = pending.split(b"\n")
        for raw in lines:
            yield raw.decode(errors="replace")
    if pending:
        yield pending.decode(errors="replace")


async def _read_stream(
    stream: ByteReceiveStream,
    stream_buffer: list[str],
    combined_buffer: list[str],
    log_level: int,
) -> None:
    """Read lines from a stream and append to both stream-specific and combined buffers."""
    try:
        async for line in iter_lines(stream):
            decoded = line.strip()
            stream_buffer.append(decoded)
            combined_buffer.append(decoded)
            if log_level != logging.NOTSET:
                logger.log(log_level, "subprocess_output", line=decoded)
    except Exception:
        logger.exception("stream_read_error")


def log_command(*args, log_level: int) -> None:
    """Log a command with its arguments at the specified log level.

    Args:
        *args: Command and arguments to log
        log_level: Logging level (e.g., logging.DEBUG, logging.INFO). NOTSET (0) suppresses logging.
    """
    if log_level == logging.NOTSET:
        return
    logger.log(
        log_level,
        "running_command",
        command=shlex.join([str(arg) for arg in args]),
    )
