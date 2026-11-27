# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from pathlib import Path

import huggorm
import pytest

from src.errors import VerifyStorePathsError
from src.nix import verify_store_paths

# A chroot store under the test's directory: Nix's own client and store, no daemon.
pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


async def test_an_intact_closure_verifies(tmp_path: Path) -> None:
    store = huggorm.AsyncStore(str(tmp_path / "store"))
    try:
        leaf = await store.add_to_store("leaf", b"leaf\n")
        top = await store.add_to_store("top", b"top\n", references=[leaf])
        await verify_store_paths(store, {Path(await store.print_store_path(top))})
    finally:
        await store.aclose()


async def test_a_changed_file_in_the_closure_fails(tmp_path: Path) -> None:
    store = huggorm.AsyncStore(str(tmp_path / "store"))
    try:
        leaf = await store.add_to_store("leaf", b"leaf\n")
        top = await store.add_to_store("top", b"top\n", references=[leaf])
        # The reference, not the root: the check has to walk the closure to see it.
        on_disk = Path(await store.real_path(leaf))
        on_disk.chmod(0o644)
        on_disk.write_bytes(b"changed\n")

        with pytest.raises(VerifyStorePathsError) as raised:
            await verify_store_paths(store, {Path(await store.print_store_path(top))})
        assert raised.value.logs is not None
        assert leaf.to_string() in raised.value.logs
        assert top.to_string() not in raised.value.logs
    finally:
        await store.aclose()
