# SPDX-License-Identifier: Apache-2.0

import contextlib

import anyio
import structlog
from pynixd.config import PynixdSettings
from pynixd.instance import Server

from .central_store import LOCAL, builder_local_spec
from .setup import configure_logging, install_nss

log = structlog.get_logger(__name__)


async def _main() -> None:
    configure_logging()
    log.info("pynixd_nixkube_builder_starting")
    install_nss()

    settings = PynixdSettings()
    # `spec.to_store`, not `LocalSocketStore(spec)`. See central_main.py. The
    # volume carries its own `db/db.sqlite`, so the fast paths have a database
    # to read here too.
    local_store = builder_local_spec(settings).to_store(str(LOCAL))

    server = Server(stores={LOCAL: local_store}, settings=settings)

    async with server:
        log.info("pynixd_nixkube_builder_running")
        await server.wait_finished()


def main() -> None:
    with contextlib.suppress(KeyboardInterrupt):
        anyio.run(_main, backend="asyncio")
