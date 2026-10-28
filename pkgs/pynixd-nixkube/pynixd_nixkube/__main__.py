# SPDX-License-Identifier: Apache-2.0

import contextlib
from pathlib import Path

import anyio
import structlog
from pynixd.config import LocalSocketStoreSpec, PynixdSettings
from pynixd.instance import Server
from pynixd.serde import StoreId

from .setup import configure_logging, install_nss

log = structlog.get_logger(__name__)


async def _main():
    configure_logging()
    log.info("pynixd_nixkube_starting")
    install_nss()

    # `spec.to_store`, not `LocalSocketStore(spec)`. See central_main.py.
    local_store = LocalSocketStoreSpec(
        store_id=StoreId("local"),
        store_path=Path("/"),
        monitor=False,
    ).to_store(str(StoreId("local")))

    settings = PynixdSettings()

    server = Server(stores={StoreId("local"): local_store}, settings=settings)

    async with server:
        log.info("pynixd_nixkube_running")
        await server.wait_finished()


def main():
    with contextlib.suppress(KeyboardInterrupt):
        anyio.run(_main, backend="asyncio")
