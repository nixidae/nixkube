# SPDX-License-Identifier: Apache-2.0

"""The node's store, as a huggorm client of the node socket.

One `AsyncStore` per operation: a publish, an NRI build, a readiness probe.
`open_node_store` opens it for the block, and each call inside takes it from
`node_store()`. Not one per process: Nix's `RemoteStore` refuses every call
after one failed connect ("previously failed", `remote-store.cc`
`openConnectionWrapper`), so a store server that is not up yet, or restarts,
would cost the node its store for good. A fresh store costs about 5 ms.

One store spans the whole operation, so the connection that substituted a
path is still open when the path is rooted. nixidae/nixidae#1661,
`docs/node-store.md`.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from contextvars import ContextVar
from typing import TYPE_CHECKING

import anyio
import huggorm

from ..constants import NODE_STORE_SOCKET

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

_current: ContextVar[huggorm.AsyncStore | None] = ContextVar("node_store", default=None)


def node_store_uri(socket: Path = NODE_STORE_SOCKET) -> str:
    """The store URI of the server on `socket`. Explicit, not `daemon`, so
    the client dials what the manifest named and not what a nix.conf says."""
    return f"unix://{socket}"


@asynccontextmanager
async def open_node_store(uri: str | None = None) -> AsyncIterator[huggorm.AsyncStore]:
    """A store of its own for the block, closed after it."""
    store = huggorm.AsyncStore(uri or node_store_uri())
    token = _current.set(store)
    try:
        yield store
    finally:
        _current.reset(token)
        # Shielded: a block that ends by its deadline is cancelled, and an
        # unshielded close would be cancelled with it and leak the store.
        with anyio.CancelScope(shield=True):
            await store.aclose()


def node_store() -> huggorm.AsyncStore:
    """The store the enclosing `open_node_store` holds open."""
    store = _current.get()
    if store is None:
        raise RuntimeError("no node store is open here: wrap the operation in open_node_store()")
    return store
