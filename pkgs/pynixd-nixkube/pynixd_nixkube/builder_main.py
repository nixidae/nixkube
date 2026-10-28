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


async def _main() -> None:
    configure_logging()
    log.info("pynixd_nixkube_builder_starting")
    install_nss()

    # `spec.to_store`, not `LocalSocketStore(spec)`. See central_main.py. The
    # volume carries its own `db/db.sqlite`, named in the comment below, so
    # the fast paths have a database to read here too.
    local_store = LocalSocketStoreSpec(
        store_id=StoreId("local"),
        store_path=Path("/"),
        # This Pod runs no nix-daemon, so pynixd starts one.
        #
        # pynixd otherwise derives that from the store path, and reads a
        # store at `/` as the store of the machine: protected, and not
        # safe for an unprivileged private daemon. Here `/` is a
        # copy-on-write volume of this Pod alone, with its own
        # db/db.sqlite and root in its own namespace, so the premise does
        # not hold and every builder died at ensure_daemon.
        #
        # Not fixed by relocating the store. The volume keeps the node's
        # store prefix and path hashes on purpose -- that is what lets a
        # builder share paths with the node, with pynixd and with
        # substituters. See nixkube issue #19.
        managed=True,
        monitor=False,
        extra_args=["--option", "build-dir", "/nix/var/nix/builds"],
    ).to_store(str(StoreId("local")))

    settings = PynixdSettings()

    server = Server(stores={StoreId("local"): local_store}, settings=settings)

    async with server:
        log.info("pynixd_nixkube_builder_running")
        await server.wait_finished()


def main() -> None:
    with contextlib.suppress(KeyboardInterrupt):
        anyio.run(_main, backend="asyncio")
