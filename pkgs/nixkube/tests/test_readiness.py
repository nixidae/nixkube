# SPDX-License-Identifier: Apache-2.0
"""`/readyz`: NotReady while the node runs another nodeEnv than its
deployment asks for, or while the driver does not answer.

nixidae/nixidae#1220: every node of a DaemonSet rolled onto the image's older
nodeEnv and reported Ready, so the rolling update replaced all five.
"""

from __future__ import annotations

import json
import shutil
import tempfile
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from pathlib import Path

import anyio
import anyio.abc
import pytest
from google.protobuf.wrappers_pb2 import BoolValue
from grpclib.server import Server, Stream

from csi import csi_pb2
from src import readiness
from src.csi.identity import IdentityServicer
from src.readiness import Answer, NodeEnv

pytestmark = pytest.mark.anyio

WANTED = "/nix/store/llwhaaaaaaaaaaaaaaaaaaaaaaaaaaaa-nodeEnv"
OLDER = "/nix/store/bi49aaaaaaaaaaaaaaaaaaaaaaaaaaaa-nodeEnv"
ARM = "/nix/store/armaaaaaaaaaaaaaaaaaaaaaaaaaaaa-nodeEnv"
SPEC = json.dumps({"x86_64-linux": WANTED, "aarch64-linux": ARM})


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


class TestWantedFor:
    def test_the_map_answers_this_system(self) -> None:
        assert readiness.wanted_for(SPEC, "x86_64-linux") == WANTED
        assert readiness.wanted_for(SPEC, "aarch64-linux") == ARM

    def test_a_plain_path_is_every_system(self) -> None:
        assert readiness.wanted_for(f" {WANTED}\n", "aarch64-linux") == WANTED

    def test_unset_and_unlisted_name_nothing(self) -> None:
        assert readiness.wanted_for("", "x86_64-linux") is None
        assert readiness.wanted_for(SPEC, "riscv64-linux") is None

    def test_broken_json_raises(self) -> None:
        with pytest.raises(ValueError):
            readiness.wanted_for('{"x86_64-linux": ', "x86_64-linux")

    def test_a_value_that_is_not_a_path_names_nothing(self) -> None:
        assert readiness.wanted_for('{"x86_64-linux": 1}', "x86_64-linux") is None


class TestNodeEnv:
    def test_another_nodeenv_is_behind(self) -> None:
        assert NodeEnv(wanted=WANTED, running=OLDER).behind

    def test_the_wanted_nodeenv_is_not_behind(self) -> None:
        assert not NodeEnv(wanted=WANTED, running=WANTED).behind

    def test_cannot_tell_is_not_behind(self) -> None:
        assert not NodeEnv(wanted=None, running=OLDER).behind
        assert not NodeEnv(wanted=WANTED, running=None).behind

    def test_running_is_the_result_link(self, tmp_path: Path) -> None:
        """`appstarter run` execs out of `/nix/var/result`, so its target is
        what runs, whatever `state.json` says."""
        link = tmp_path / "result"
        link.symlink_to(OLDER)

        env = readiness.read_node_env(SPEC, "x86_64-linux", link)

        assert env == NodeEnv(wanted=WANTED, running=OLDER)
        assert env.behind

    def test_no_link_and_bad_json_read_as_unknown(self, tmp_path: Path) -> None:
        env = readiness.read_node_env("{not json", "x86_64-linux", tmp_path / "absent")

        assert env == NodeEnv(wanted=None, running=None)


class TestAnswer:
    def test_current_and_driver_up_is_ready(self) -> None:
        assert readiness.answer(NodeEnv(WANTED, WANTED), None) == Answer(200, "ok\n")

    def test_behind_names_both_paths(self) -> None:
        result = readiness.answer(NodeEnv(WANTED, OLDER), None)

        assert result.status == 503
        assert OLDER in result.body
        assert WANTED in result.body

    def test_every_reason_is_in_the_body(self) -> None:
        result = readiness.answer(NodeEnv(WANTED, OLDER), "no answer within 3s")

        assert result.status == 503
        assert result.body.splitlines() == [
            f"behind: this node runs {OLDER}, and the deployment asks for {WANTED}",
            "driver: no answer within 3s",
        ]


class TestRequestPath:
    def test_a_get(self) -> None:
        assert readiness.request_path(b"GET /readyz?x=1 HTTP/1.1\r\nHost: a\r\n\r\n") == "/readyz"

    def test_not_a_get(self) -> None:
        assert readiness.request_path(b"POST /readyz HTTP/1.1\r\n\r\n") is None
        assert readiness.request_path(b"\r\n\r\n") is None


class NotReadyIdentity(IdentityServicer):
    async def Probe(self, stream: Stream[csi_pb2.ProbeRequest, csi_pb2.ProbeResponse]) -> None:
        await stream.recv_message()
        await stream.send_message(csi_pb2.ProbeResponse(ready=BoolValue(value=False)))


@pytest.fixture
def socket_dir() -> Iterator[Path]:
    # A unix socket path is limited to 108 bytes, and `tmp_path` can exceed it.
    path = Path(tempfile.mkdtemp(prefix="rz"))
    yield path
    shutil.rmtree(path)


@asynccontextmanager
async def _driver(socket: Path, servicer: IdentityServicer) -> AsyncIterator[None]:
    server = Server([servicer])
    await server.start(path=str(socket))
    try:
        yield
    finally:
        server.close()
        await server.wait_closed()


@pytest.fixture
async def driver(socket_dir: Path) -> AsyncIterator[Path]:
    socket = socket_dir / "csi.sock"
    async with _driver(socket, IdentityServicer()):
        yield socket


async def _get(env: NodeEnv, socket: Path, path: str = readiness.PATH) -> tuple[int, str]:
    """One request through a real listener, as the kubelet sends it."""
    listener = await anyio.create_tcp_listener(local_host="127.0.0.1", local_port=0)
    port = listener.extra(anyio.abc.SocketAttribute.local_port)

    async def serve_one(stream: anyio.abc.SocketStream) -> None:
        await readiness.handle(stream, env=env, socket=socket)

    async with anyio.create_task_group() as tg:
        tg.start_soon(listener.serve, serve_one, tg)
        async with await anyio.connect_tcp("127.0.0.1", port) as client:
            await client.send(f"GET {path} HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n".encode())
            reply = b""
            try:
                while True:
                    reply += await client.receive()
            except anyio.EndOfStream:
                pass
        tg.cancel_scope.cancel()
    await listener.aclose()
    head, _, body = reply.decode().partition("\r\n\r\n")
    return int(head.split(" ")[1]), body


class TestServe:
    async def test_ready(self, driver: Path) -> None:
        assert await _get(NodeEnv(WANTED, WANTED), driver) == (200, "ok\n")

    async def test_behind_is_not_ready_while_the_driver_answers(self, driver: Path) -> None:
        """The case of #1220: the node serves, and must not say Ready."""
        status, body = await _get(NodeEnv(WANTED, OLDER), driver)

        assert status == 503
        assert body.startswith(f"behind: this node runs {OLDER}")
        assert "driver" not in body

    async def test_a_missing_socket_is_not_ready(self, socket_dir: Path) -> None:
        """An unlinked socket: the driver runs, and nothing reaches it."""
        status, body = await _get(NodeEnv(WANTED, WANTED), socket_dir / "absent.sock")

        assert status == 503
        assert body.startswith("driver: ")

    async def test_a_not_ready_probe_is_not_ready(self, socket_dir: Path) -> None:
        socket = socket_dir / "csi.sock"
        async with _driver(socket, NotReadyIdentity()):
            assert await _get(NodeEnv(WANTED, WANTED), socket) == (503, "driver: Probe answered not ready\n")

    async def test_another_path_is_not_found(self, driver: Path) -> None:
        status, _ = await _get(NodeEnv(WANTED, WANTED), driver, "/healthz")

        assert status == 404
