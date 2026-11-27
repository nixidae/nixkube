# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from pathlib import Path

import huggorm
import pytest

from src.errors import StorePathClosureError
from src.nix import get_closure_paths, node_store, open_node_store

# A chroot store under the test's directory: Nix's own client and store, no daemon.
pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


async def test_the_closure_holds_the_path_and_what_it_references(tmp_path: Path) -> None:
    store = huggorm.AsyncStore(str(tmp_path / "store"))
    try:
        leaf = await store.add_to_store("leaf", b"leaf\n")
        top = await store.add_to_store("top", b"top\n", references=[leaf])
        unrelated = await store.add_to_store("unrelated", b"unrelated\n")
        printed = {
            name: Path(await store.print_store_path(p))
            for name, p in (("leaf", leaf), ("top", top), ("unrelated", unrelated))
        }

        assert await get_closure_paths(store, {printed["top"]}) == {printed["top"], printed["leaf"]}
    finally:
        await store.aclose()


async def test_a_path_the_store_lacks_fails_the_closure(tmp_path: Path) -> None:
    store = huggorm.AsyncStore(str(tmp_path / "store"))
    try:
        with pytest.raises(StorePathClosureError):
            await get_closure_paths(store, {Path("/nix/store/" + "0" * 32 + "-absent")})
    finally:
        await store.aclose()


async def test_the_node_store_exists_only_while_it_is_open(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="not open"):
        node_store()
    async with open_node_store(str(tmp_path / "store")) as store:
        assert node_store() is store
    with pytest.raises(RuntimeError, match="not open"):
        node_store()
