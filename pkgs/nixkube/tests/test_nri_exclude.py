# SPDX-License-Identifier: Apache-2.0
"""`CreateContainer` stands down for a container that asked to be left alone.

`parse_nix_exclude` is tested next door, and a correct parser wired in too
late is the failure this file exists for: the exclusion has to happen *before*
anything reads the container's environment, because reading it is what leads
to `fetch_packages` realising a closure into the node's store.

The case that forced it: `appstarter-init` mounts its store at `/nix-volume`,
so the `/nix` check above it never matches, and its `APPSTARTER_WANTED`
environment named the closure of the very pynixd that would have had to serve
it. nixidae/nixidae#493, nixidae/nixidae#363.
"""

from __future__ import annotations

import pytest
from nri.nri_pb2 import CreateContainerRequest, CreateContainerResponse

from src.nri import server as nri_server


class _Stream:
    """The two calls `CreateContainer` makes on a grpclib stream."""

    def __init__(self, request: CreateContainerRequest) -> None:
        self._request = request
        self.sent: list[CreateContainerResponse] = []

    async def recv_message(self) -> CreateContainerRequest:
        return self._request

    async def send_message(self, message: CreateContainerResponse) -> None:
        self.sent.append(message)


def _request(
    annotations: dict[str, str], *, container: str = "appstarter-init"
) -> CreateContainerRequest:
    request = CreateContainerRequest()
    request.pod.namespace = "nixkube"
    request.pod.name = "pynixd-0"
    request.pod.annotations.update(annotations)
    request.container.name = container
    request.container.id = "c0"
    return request


async def _create(monkeypatch, request: CreateContainerRequest) -> _Stream:
    """Drive `CreateContainer` without a plugin instance.

    The early returns under test read the request and module-level helpers,
    never `self`, so a stand-in is enough to reach them -- and a real
    `NriPlugin` would want a ZeroMQ server and a CRI socket.
    """
    monkeypatch.setattr(nri_server, "get_current_system", lambda: "x86_64-linux")
    stream = _Stream(request)
    await nri_server.NriPlugin.CreateContainer(object(), stream)
    return stream


@pytest.mark.asyncio
class TestTheExclusion:
    async def test_an_excluded_container_is_answered_with_an_empty_adjustment(
        self, monkeypatch
    ) -> None:
        stream = await _create(
            monkeypatch, _request({"nixkube/appstarter-init-exclude": "true"})
        )

        assert len(stream.sent) == 1
        adjustment = stream.sent[0].adjust
        assert not adjustment.mounts
        assert not adjustment.env

    async def test_the_environment_is_never_read(self, monkeypatch) -> None:
        """The reason the check sits where it does. Extracting the store paths
        is what leads to realising them on the node, and for this container
        the only substituter for that closure is the workload it is starting."""
        called: list[object] = []
        monkeypatch.setattr(
            nri_server,
            "extract_container_store_paths",
            lambda *args: called.append(args) or set(),
        )

        await _create(
            monkeypatch, _request({"nixkube/appstarter-init-exclude": "true"})
        )

        assert called == []

    async def test_a_container_nobody_excluded_is_still_read(self, monkeypatch) -> None:
        """The negative control. Without it a stand-down that fired for every
        container would pass every test above."""
        called: list[object] = []
        monkeypatch.setattr(
            nri_server,
            "extract_container_store_paths",
            lambda *args: called.append(args) or set(),
        )

        await _create(monkeypatch, _request({}, container="pynixd"))

        assert called, "the exclusion fired for a container that asked for nothing"

    async def test_a_mounted_nix_still_stands_down(self, monkeypatch) -> None:
        """The check that was there first, and that this one had to be placed
        before: a container carrying its own `/nix` is left alone whatever its
        annotations say."""
        request = _request({})
        request.container.mounts.add(destination="/nix", source="/nix")

        stream = await _create(monkeypatch, request)

        assert len(stream.sent) == 1
        assert not stream.sent[0].adjust.mounts
