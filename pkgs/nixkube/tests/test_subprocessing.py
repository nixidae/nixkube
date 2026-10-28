# SPDX-License-Identifier: Apache-2.0

import pytest

from src.errors import CommandTimeoutError, SubprocessError
from src.subprocessing import run_captured, try_captured, try_console


@pytest.mark.asyncio
async def test_simple_echo():
    """Test simple echo command."""
    result = await run_captured("echo", "hello world")
    assert result.returncode == 0
    assert "hello world" in result.stdout
    assert result.combined == result.stdout


@pytest.mark.asyncio
async def test_stdout_and_stderr():
    """Test capturing both stdout and stderr."""
    result = await run_captured("sh", "-c", "echo stdout; echo stderr >&2")
    assert result.returncode == 0
    assert "stdout" in result.stdout
    assert "stderr" in result.stderr
    assert "stdout" in result.combined
    assert "stderr" in result.combined


@pytest.mark.asyncio
async def test_non_zero_exit_code():
    """Test that non-zero exit codes are returned without raising."""
    result = await run_captured("sh", "-c", "exit 42")
    assert result.returncode == 42


@pytest.mark.asyncio
async def test_try_captured_raises_on_error():
    """Test that try_captured raises SubprocessError on non-zero exit."""
    with pytest.raises(SubprocessError) as exc_info:
        await try_captured("sh", "-c", "exit 1")
    assert exc_info.value.returncode == 1


@pytest.mark.asyncio
async def test_try_console_raises_on_error():
    """Test that try_console raises SubprocessError on non-zero exit."""
    with pytest.raises(SubprocessError) as exc_info:
        await try_console("sh", "-c", "exit 1")
    assert exc_info.value.returncode == 1


@pytest.mark.asyncio
async def test_combined_output_contains_all_data():
    """Test that combined output contains all data from both streams."""
    result = await run_captured("sh", "-c", "echo a; echo b >&2; echo c")
    lines_set = set(result.combined.split("\n"))
    assert lines_set == {"a", "b", "c"}


@pytest.mark.asyncio
async def test_timing_recorded():
    """Test that elapsed time is recorded."""
    result = await run_captured("echo", "test")
    assert result.elapsed > 0


@pytest.mark.asyncio
async def test_multiple_lines():
    """Test capturing multiple lines of output."""
    result = await run_captured("sh", "-c", "echo line1; echo line2; echo line3")
    assert result.returncode == 0
    assert "line1" in result.stdout
    assert "line2" in result.stdout
    assert "line3" in result.stdout


@pytest.mark.asyncio
async def test_stderr_only():
    """Test capturing stderr-only output."""
    result = await run_captured("sh", "-c", "echo error >&2")
    assert result.returncode == 0
    assert result.stdout == ""
    assert "error" in result.stderr
    assert "error" in result.combined


@pytest.mark.asyncio
async def test_empty_output():
    """Test command with no output."""
    result = await run_captured("true")
    assert result.returncode == 0
    assert result.stdout == ""
    assert result.stderr == ""
    assert result.combined == ""


@pytest.mark.asyncio
async def test_timeout_raised():
    """Test that timeout is raised when command exceeds timeout."""
    with pytest.raises(CommandTimeoutError) as exc_info:
        await run_captured("sleep", "10", timeout=0.1)
    assert exc_info.value.returncode == 124


@pytest.mark.asyncio
async def test_the_caller_survives_a_timeout():
    """The deadline is a cancel scope, and a scope left half-exited keeps the
    caller in a cancelling state: the next `await` raises rather than runs.
    `get_build_args` calls `run_captured` and then goes on to build, so a
    caller that cannot continue past a timed-out cache ping is the failure.
    The assertion above cannot see it -- both paths raise the same error."""
    with pytest.raises(CommandTimeoutError):
        await run_captured("sleep", "10", timeout=0.1)

    result = await run_captured("echo", "after")

    assert result.returncode == 0
    assert result.stdout == "after"


@pytest.mark.asyncio
async def test_large_output():
    """Test handling of large output."""
    # Generate 1000 lines of output
    result = await run_captured("sh", "-c", "for i in $(seq 1 1000); do echo line_$i; done")
    assert result.returncode == 0
    lines = result.stdout.split("\n")
    assert len(lines) >= 1000
    assert "line_1" in result.stdout
    assert "line_1000" in result.stdout


@pytest.mark.asyncio
async def test_unicode_output():
    """Test handling of unicode characters in output."""
    result = await run_captured("echo", "hello 世界 🌍")
    assert result.returncode == 0
    assert "世界" in result.stdout
    assert "🌍" in result.stdout


@pytest.mark.asyncio
async def test_error_message_preserved():
    """Test that error messages are preserved in stderr."""
    result = await run_captured("sh", "-c", "echo important error >&2; exit 1")
    assert result.returncode == 1
    assert "important error" in result.stderr
    assert "important error" in result.combined


@pytest.mark.asyncio
async def test_try_captured_preserves_full_error():
    """Test that try_captured includes full output in error."""
    with pytest.raises(SubprocessError) as exc_info:
        await try_captured("sh", "-c", "echo output; echo error >&2; exit 5")
    error = exc_info.value
    assert error.returncode == 5
    assert "error" in error.stderr
    assert "error" in error.combined


@pytest.mark.asyncio
async def test_a_timeout_error_from_inside_is_not_reported_as_the_deadline():
    """The deadline is the cancel scope that caught it, not `TimeoutError`.

    A `TimeoutError` raised *inside* -- a socket, a transport, a nested
    deadline -- is not this command exceeding its own. Catching the type
    instead of reading the scope reported it as `CommandTimeoutError(124)`,
    naming a cause that did not happen and hiding the one that did.

    `open_process` and not `_read_stream`: the stream readers run in a task
    group, so what leaves them is an `ExceptionGroup` that `except
    TimeoutError` never matched either. This is the shape that told the two
    apart.
    """
    from src import subprocessing

    async def refuse(*_args, **_kwargs):
        raise TimeoutError("the transport gave up")

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(subprocessing.anyio, "open_process", refuse)
        with pytest.raises(TimeoutError) as caught:
            await run_captured("echo", "hello", timeout=30)

    assert not isinstance(caught.value, CommandTimeoutError), (
        "an inner TimeoutError was rewritten as this command timing out"
    )
