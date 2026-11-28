# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from typing import TYPE_CHECKING

import anyio
import huggorm
import pytest

from src.nix import node_store, open_node_store

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


async def test_the_node_store_exists_only_inside_an_operation(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="no node store is open"):
        node_store()
    async with open_node_store(str(tmp_path / "store")) as store:
        assert node_store() is store
    with pytest.raises(RuntimeError, match="no node store is open"):
        node_store()


async def test_a_failed_connect_costs_one_operation(tmp_path: Path) -> None:
    """Nix's `RemoteStore` refuses every call after one failed connect. A
    store server that is not up yet must cost the operation that met it,
    not every operation after."""
    missing = f"unix://{tmp_path / 'no-server.sock'}"
    for _ in range(2):
        async with open_node_store(missing):
            with pytest.raises(huggorm.errors.NixError, match="cannot connect"):
                await node_store().get_version()


async def test_concurrent_operations_hold_stores_of_their_own(tmp_path: Path) -> None:
    seen: list[huggorm.AsyncStore] = []

    async def operation(name: str) -> None:
        async with open_node_store(str(tmp_path / name)):
            await anyio.lowlevel.checkpoint()
            seen.append(node_store())

    async with anyio.create_task_group() as tg:
        tg.start_soon(operation, "a")
        tg.start_soon(operation, "b")
    assert len({id(store) for store in seen}) == 2
