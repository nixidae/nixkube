# SPDX-License-Identifier: Apache-2.0

"""What nri-wait does while it waits.

A build is slower than any timeout somebody picks, so the hook does not
time the build. It times the daemon's silence. These tests hold it to that,
because the bug they follow made the heartbeat decoration: a second,
absolute deadline of the same length ran beside the resettable one and
nothing reset it, so no build could last longer than NRI_TIMEOUT.
"""

from __future__ import annotations

import contextlib
import json
import tempfile
import threading
import time
from collections.abc import Generator, Iterator
from pathlib import Path

import pytest
import zmq
from nri_wait import connect_updates, wait_for_completion

CONTAINER = "0123456789abcdef"

# Shorter than the pauses below, so a test that hangs fails in a second
# rather than sitting there.
TIMEOUT = 1


def _socket_dir() -> str:
    """A directory short enough to hold a unix socket path.

    A unix socket path cannot exceed 107 bytes, and pytest's `tmp_path` is
    nowhere near short enough on its own: it carries the test name and a
    session number under TMPDIR.

    That is how this suite hung CI for a day. Nix's daemon builds in `/build`,
    so the path came to 77 bytes and everything passed. The CI runner installs
    nix single-user with no daemon, so a build happens under

        /home/runner/work/_temp/nix-build-nri-wait-0.1.0.drv-0/

    and the same socket came to 125 bytes. bind() then raised, the fixture
    never reached its teardown, and the leaked context wedged the interpreter
    at exit -- after pytest had already reported. Both builders sat there until
    the job timeout killed them.

    So the socket goes in a directory of its own, named as briefly as
    tempfile will, and never under `tmp_path`. TMPDIR is tried first, because
    that is the one place a sandboxed build may write. `/tmp` is the fallback
    for a TMPDIR too long to hold a socket at all, which no sandbox has and a
    deep checkout can.
    """
    for base in (None, "/tmp"):
        made = tempfile.mkdtemp(prefix="nw", dir=base)
        if len(made) + len("/p.sock") < zmq.IPC_PATH_MAX_LEN:
            return made
    return made


@pytest.fixture
def pub() -> Generator[tuple[zmq.Socket[bytes], str]]:
    """A PUB socket standing in for the build daemon, and its path.

    try/finally, not a bare yield. A fixture that raises before it yields runs
    no teardown, so a failing bind used to leak the socket and the context --
    and `zmq_ctx_term` blocks for ever on a context whose sockets are still
    open. destroy(linger=0) closes them first, so a failure here is a failure
    and never a hang.
    """
    context: zmq.Context[zmq.Socket[bytes]] = zmq.Context()
    try:
        socket = context.socket(zmq.PUB)
        path = Path(_socket_dir()) / "p.sock"
        assert len(str(path)) < zmq.IPC_PATH_MAX_LEN, (
            f"{path} is {len(str(path))} bytes, and a unix socket path holds {zmq.IPC_PATH_MAX_LEN}"
        )
        socket.bind(f"ipc://{path}")
        yield socket, str(path)
    finally:
        context.destroy(linger=0)


@contextlib.contextmanager
def subscriber(path: str) -> Iterator[zmq.Socket[bytes]]:
    """A SUB socket joined to `path`, and a context that always goes away.

    Every test used to build these by hand and end with `context.term()`. That
    line is unreachable whenever the body above it raises, and a context with
    a live socket makes `term` block for ever. See the `pub` fixture.
    """
    context: zmq.Context[zmq.Socket[bytes]] = zmq.Context()
    try:
        yield connect_updates(context, path)
    finally:
        context.destroy(linger=0)


def message(status: str, container_id: str = CONTAINER) -> bytes:
    return json.dumps({"container_id": container_id, "status": status}).encode()


def never() -> bool:
    """A recheck that says the build is not done."""
    return False


def test_the_socket_directory_stays_short_under_a_long_tmpdir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The regression this suite hung CI for a day without.

    tempfile caches the answer in `tempfile.tempdir`, so setting TMPDIR in the
    environment after the first call changes nothing. The attribute is what
    has to move.
    """
    deep = tmp_path / ("d" * 120)
    deep.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(deep))

    made = _socket_dir()

    assert len(made) + len("/p.sock") < zmq.IPC_PATH_MAX_LEN


class TestHeartbeat:
    """The daemon says it is alive. The hook keeps waiting."""

    def test_progress_outlives_the_timeout(self, pub: tuple[zmq.Socket[bytes], str]) -> None:
        """A build three times longer than TIMEOUT still completes.

        The publisher sends progress from the start, so the test never has
        to guess when the subscription arrives -- ZeroMQ drops what nobody
        has joined for yet, and a later heartbeat lands either way.
        """
        socket, path = pub
        stop = threading.Event()

        def daemon() -> None:
            deadline = time.time() + 3 * TIMEOUT
            while time.time() < deadline and not stop.is_set():
                socket.send(message("progress"))
                time.sleep(TIMEOUT / 10)
            socket.send(message("done"))

        thread = threading.Thread(target=daemon)
        thread.start()
        try:
            with subscriber(path) as sub:
                started = time.time()
                wait_for_completion(sub, CONTAINER, TIMEOUT, never)
                waited = time.time() - started
        finally:
            stop.set()
            thread.join()

        assert waited > TIMEOUT, "the heartbeat did not extend the wait"

    def test_another_container_does_not_count(self, pub: tuple[zmq.Socket[bytes], str]) -> None:
        """Heartbeats for a different container are not this one's."""
        socket, path = pub
        stop = threading.Event()

        def daemon() -> None:
            while not stop.is_set():
                socket.send(message("progress", "some-other-container"))
                time.sleep(TIMEOUT / 10)

        thread = threading.Thread(target=daemon)
        thread.start()
        try:
            with (
                subscriber(path) as sub,
                pytest.raises(SystemExit) as exit_info,
            ):
                wait_for_completion(sub, CONTAINER, TIMEOUT, never)
        finally:
            stop.set()
            thread.join()

        assert exit_info.value.code == 1


class TestFailure:
    """The daemon says the build failed. The hook stops at once, with the reason."""

    def test_failure_ends_the_wait_with_its_reason(
        self, pub: tuple[zmq.Socket[bytes], str], capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Sent repeatedly, for the same reason as the heartbeat test: a
        message sent before the subscription arrives reaches nobody."""
        socket, path = pub
        stop = threading.Event()
        reason = "the container's rootfs is not reachable through pid 7237"

        def daemon() -> None:
            payload = {"container_id": CONTAINER, "status": "failed", "reason": reason}
            while not stop.is_set():
                socket.send(json.dumps(payload).encode())
                time.sleep(TIMEOUT / 10)

        thread = threading.Thread(target=daemon)
        thread.start()
        started = time.time()
        try:
            with (
                subscriber(path) as sub,
                pytest.raises(SystemExit) as exit_info,
            ):
                wait_for_completion(sub, CONTAINER, 10 * TIMEOUT, never)
        finally:
            waited = time.time() - started
            stop.set()
            thread.join()

        assert exit_info.value.code == 1
        assert waited < TIMEOUT, "the hook waited out its timeout instead"
        assert reason in capsys.readouterr().err


class TestSilence:
    """The daemon says nothing. That is the only failure."""

    def test_silence_fails(self, pub: tuple[zmq.Socket[bytes], str]) -> None:
        _, path = pub
        with subscriber(path) as sub, pytest.raises(SystemExit) as exit_info:
            wait_for_completion(sub, CONTAINER, TIMEOUT, never)
        assert exit_info.value.code == 1

    def test_the_query_socket_has_the_last_word(self, pub: tuple[zmq.Socket[bytes], str]) -> None:
        """A "done" the hook never heard still counts.

        PUB is the fast path and REP is the truth, so silence asks once
        more before it gives up.
        """
        _, path = pub
        with subscriber(path) as sub:
            wait_for_completion(sub, CONTAINER, TIMEOUT, lambda: True)
