# SPDX-License-Identifier: Apache-2.0

"""The builder manager reads pynixd's real `QueuedBuild`. Issue #78.

It read `build.platform` after pynixd removed that field, and every queue
pass raised. A fake build would have kept the stale field alive, so this
runs against pynixd's own queue.
"""

import pytest
from pynixd.build_queue import BuildQueue
from pynixd.serde import BasicDerivation, BuildDerivationRequest, BuildMode, StorePath

from pynixd_nixkube.builder_manager import pending_systems


def _request(name: str, platform: str) -> BuildDerivationRequest:
    return BuildDerivationRequest(
        drv_path=StorePath(path=f"/nix/store/{name * 32}-{name}.drv"),
        derivation=BasicDerivation(platform=platform, builder=""),
        build_mode=BuildMode.NORMAL,
    )


@pytest.mark.asyncio
async def test_pending_builds_name_the_systems_a_builder_can_serve() -> None:
    queue = BuildQueue()
    await queue.enqueue(_request("a", "x86_64-linux"))
    await queue.enqueue(_request("b", "aarch64-linux"))
    await queue.enqueue(_request("c", "riscv64-linux"))

    assert pending_systems(queue.queue, {"x86_64-linux", "aarch64-linux"}) == {
        "x86_64-linux",
        "aarch64-linux",
    }
