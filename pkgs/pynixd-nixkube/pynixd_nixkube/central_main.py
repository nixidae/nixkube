# SPDX-License-Identifier: Apache-2.0

import contextlib
from pathlib import Path

import anyio
import structlog
from pynixd.config import LocalSocketStoreSpec, PynixdSettings
from pynixd.instance import Server
from pynixd.serde import StoreId
from pynixd.store import Store

from .builder_manager import BuilderManager
from .config import NixkubeCentralSettings
from .setup import configure_logging, install_nss

log = structlog.get_logger(__name__)


async def _main() -> None:
    configure_logging()
    log.info("pynixd_nixkube_central_starting")
    install_nss()

    # `spec.to_store`, not `LocalSocketStore(spec)`. That name is a legacy
    # re-export of `LocalStore`, so naming the class here picks the store
    # without the SQLite fast paths and makes `use_db` inert -- the argument
    # was read by nothing. Every pod logged `local_store_db_disabled` and
    # answered every QueryValidPaths over the daemon socket.
    local_store = LocalSocketStoreSpec(
        store_id=StoreId("local"),
        store_path=Path("/data"),
        monitor=False,
    ).to_store(str(StoreId("local")))

    pynixd_settings = PynixdSettings()
    settings = NixkubeCentralSettings()

    stores: dict[StoreId, Store] = {StoreId("local"): local_store}
    if pynixd_settings.stores:
        config_stores = pynixd_settings.to_stores()
        for store_id, store in config_stores.items():
            if store_id != StoreId("local"):
                stores[store_id] = store

    server = Server(stores=stores, settings=pynixd_settings)

    async with server, contextlib.AsyncExitStack() as stack:
        # A stack, because the manager is conditional and its lifetime is a
        # block.
        if settings.kube_namespace:
            await stack.enter_async_context(
                BuilderManager(
                    server=server,
                    namespace=settings.kube_namespace,
                    max_builders=settings.builder_max,
                    min_builders=settings.builder_min,
                    idle_timeout=settings.idle_timeout,
                    systems=[
                        s.strip() for s in settings.systems.split(",") if s.strip()
                    ],
                    startup_timeout=settings.builder_startup_timeout,
                    backoff_cap=settings.builder_backoff_cap,
                    max_age=settings.builder_max_age,
                ).running()
            )

        log.info("pynixd_nixkube_central_running")
        await server.wait_finished()


def main() -> None:
    with contextlib.suppress(KeyboardInterrupt):
        anyio.run(_main, backend="asyncio")
