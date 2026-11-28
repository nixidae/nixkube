# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import os
import socket
from pathlib import Path
from typing import TYPE_CHECKING

import anyio
import pytest
from prometheus_client import REGISTRY

from src.errors import BuildError
from src.nix import build, build_store_path, fetch_packages, open_node_store
from src.nix.build import out_links
from src.tracing import TRACER

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    import huggorm
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

# The node's store is a chroot store under the test's directory: Nix's own
# client and store, no daemon. Substitution from pynixd is the guest test's.
pytestmark = pytest.mark.anyio

ABSENT = Path("/nix/store/" + "0" * 32 + "-absent")


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
async def store(tmp_path: Path) -> AsyncIterator[huggorm.AsyncStore]:
    async with open_node_store(str(tmp_path / "store")) as opened:
        yield opened


async def _add(store: huggorm.AsyncStore, *names: str) -> list[Path]:
    return [Path(await store.print_store_path(await store.add_to_store(n, n.encode()))) for n in names]


def _auto_roots(store_root: Path) -> set[str]:
    return {link.readlink().as_posix() for link in (store_root / "nix/var/nix/gcroots/auto").iterdir()}


def _builds(kind: str, result: str) -> float:
    return REGISTRY.get_sample_value("nixkube_nix_builds_total", {"kind": kind, "result": result}) or 0.0


def test_links_are_named_as_nix_build_names_them() -> None:
    assert out_links(Path("/r/build"), 3) == [Path("/r/build"), Path("/r/build-1"), Path("/r/build-2")]


async def test_each_package_is_rooted_in_sorted_order(tmp_path: Path, store: huggorm.AsyncStore) -> None:
    b, a = await _add(store, "b", "a")
    first, second = sorted([a, b])
    gc_root = tmp_path / "gcroots/volume"

    await fetch_packages({b, a}, gc_root)

    assert (gc_root / "build").readlink() == first
    assert (gc_root / "build-1").readlink() == second
    # Through `addPermRoot`, as `nix build --out-link` did: an indirect root per link.
    assert {(gc_root / "build").as_posix(), (gc_root / "build-1").as_posix()} <= _auto_roots(tmp_path / "store")


async def test_a_store_path_is_rooted_by_its_name(tmp_path: Path, store: huggorm.AsyncStore) -> None:
    [path] = await _add(store, "primary")
    gc_root = tmp_path / "gcroots/volume"
    gc_root.mkdir(parents=True)

    assert await build_store_path(str(path), gc_root) == path
    assert (gc_root / path.name).readlink() == path


async def test_a_path_nothing_can_substitute_fails_the_build(tmp_path: Path, store: huggorm.AsyncStore) -> None:
    await _add(store, "present")
    gc_root = tmp_path / "gcroots/volume"
    before = _builds("packages", "error")

    with pytest.raises(BuildError, match=r"Failed to fetch 1 packages: .+"):
        await fetch_packages({ABSENT}, gc_root)

    assert _builds("packages", "error") == before + 1
    assert not (gc_root / "build").is_symlink()


CAP = int(os.environ.get("TEST_BUILD_CAP", "2"))


async def test_builds_leave_the_pool_threads_for_other_calls(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Six builds hang on a server that never answers. Under the cap they
    hold two of huggorm's four default threads, and a store call answers.
    `TEST_BUILD_CAP=6` lifts the cap: they hold all four, and the call
    hangs past its cancel (nixidae/nixidae#1672)."""
    monkeypatch.setattr(build, "_slots", anyio.CapacityLimiter(CAP))
    unaccepted = tmp_path / "unaccepted.sock"
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(unaccepted))
    server.listen(32)

    async def publish(index: int) -> None:
        async with open_node_store(f"unix://{unaccepted}"):
            await fetch_packages({ABSENT}, tmp_path / f"gcroots/{index}")

    try:
        async with anyio.create_task_group() as tg:
            for index in range(6):
                tg.start_soon(publish, index)
            await anyio.sleep(0.5)
            with anyio.move_on_after(3) as scope:
                async with open_node_store(str(tmp_path / "store")) as store:
                    await store.get_version()
            assert not scope.cancelled_caught, "a store call waited behind the builds"
            tg.cancel_scope.cancel()
    finally:
        server.close()


async def test_the_build_is_a_span_of_the_publish(
    tmp_path: Path, store: huggorm.AsyncStore, spans: InMemorySpanExporter
) -> None:
    paths = await _add(store, "a", "b")
    with TRACER.start_as_current_span("NodePublishVolume"):
        await fetch_packages(set(paths), tmp_path / "gcroots/volume")

    finished = {span.name: span for span in spans.get_finished_spans()}
    build, publish = finished["store build"], finished["NodePublishVolume"]
    assert build.parent is not None and build.parent.span_id == publish.context.span_id
    assert build.attributes == {"nixkube.build.kind": "packages", "nixkube.build.paths": 2}
