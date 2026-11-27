# SPDX-License-Identifier: Apache-2.0

"""The node's store, as a huggorm client of the node socket.

One `AsyncStore` per process: `async_main` opens it around every task, and
each call into the store takes it from `node_store()`. Nix's `RemoteStore`
keeps a pool of connections and opens a new one when one fails, so a store
server that restarts costs the calls in flight, not the client.
nixidae/nixidae#1661, `docs/node-store.md`.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

import huggorm

from ..constants import NODE_STORE_SOCKET

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

_store: huggorm.AsyncStore | None = None


def node_store_uri(socket: Path = NODE_STORE_SOCKET) -> str:
    """The store URI of the server on `socket`. Explicit, not `daemon`, so
    the client dials what the manifest named and not what a nix.conf says."""
    return f"unix://{socket}"


@asynccontextmanager
async def open_node_store(uri: str | None = None) -> AsyncIterator[huggorm.AsyncStore]:
    """Hold the node's store open for the block, and close it after."""
    global _store  # noqa: PLW0603 -- one store per process, opened and closed here only
    if _store is not None:
        raise RuntimeError("the node's store is already open")
    store = huggorm.AsyncStore(uri or node_store_uri())
    _store = store
    try:
        yield store
    finally:
        _store = None
        await store.aclose()


def node_store() -> huggorm.AsyncStore:
    """The store `open_node_store` holds open."""
    if _store is None:
        raise RuntimeError("the node's store is not open: async_main opens it")
    return _store
