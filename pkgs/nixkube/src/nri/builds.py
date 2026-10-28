# SPDX-License-Identifier: Apache-2.0

"""The builds that are still filling a container's volume."""

from collections.abc import Iterator
from dataclasses import dataclass, field

import anyio
import structlog

logger = structlog.get_logger("nixkube.nri.builds")


@dataclass
class _Build:
    done: anyio.Event = field(default_factory=anyio.Event)
    scope: anyio.CancelScope | None = None
    stopping: bool = False


class BuildRegistry:
    """Which containers have a build writing into their volume, and how to stop one.

    Membership is the volume sweep's interlock (issue #64). A build walks its
    closure with a checkpoint every 256 entries, so on a large closure it runs
    for minutes and the sweep gets to run in the middle of it. Removing the
    directory then deletes the destination mid-link, and the `FileNotFoundError`
    that follows names the *source* store path, so it reads as a corrupt store.

    Stopping is the other half. A container's removal arrives while that
    container's own build is running, and a sweep told to skip the volume
    leaves it until some later removal triggers another sweep -- on an idle
    node, indefinitely. `stop` cancels the build and waits for it to unwind,
    so the directory goes now.

    It keeps the set API (`add`, `discard`, `in`) that the ZeroMQ status query
    and the sweep already use.
    """

    def __init__(self) -> None:
        self._builds: dict[str, _Build] = {}

    def __contains__(self, container_id: object) -> bool:
        return container_id in self._builds

    def __iter__(self) -> Iterator[str]:
        return iter(tuple(self._builds))

    def __len__(self) -> int:
        return len(self._builds)

    def add(self, container_id: str) -> None:
        self._builds.setdefault(container_id, _Build())

    def discard(self, container_id: str) -> None:
        """Record that the build has left, releasing anything inside `stop`."""
        build = self._builds.pop(container_id, None)
        if build is not None:
            build.done.set()

    def attach(self, container_id: str, scope: anyio.CancelScope) -> None:
        """Give the registry the running build's cancel scope.

        Separate from `add` because the id is registered by the NRI handler
        and the scope only exists once the detached task starts running. A
        `stop` that lands in that window sets `stopping`, and this honours it.
        """
        build = self._builds.get(container_id)
        if build is None:
            return
        build.scope = scope
        if build.stopping:
            scope.cancel()

    async def stop(self, container_id: str, timeout: float) -> bool:
        """Cancel the build for `container_id`, and wait for it to leave.

        Returns whether it left. False means the volume is still being written
        into and the caller must not remove it, whatever else it knows.
        """
        build = self._builds.get(container_id)
        if build is None:
            return True

        build.stopping = True
        if build.scope is not None:
            build.scope.cancel()
        logger.info("build_cancel_requested", container_id=container_id)

        with anyio.move_on_after(timeout):
            await build.done.wait()

        if not build.done.is_set():
            logger.warning("build_cancel_timeout", container_id=container_id, timeout=timeout)
            return False
        return True
