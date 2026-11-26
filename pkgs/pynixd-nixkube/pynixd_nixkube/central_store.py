# SPDX-License-Identifier: Apache-2.0

from pathlib import Path

from pynixd.config import LocalSocketStoreSpec, PynixdSettings
from pynixd.serde import StoreId

LOCAL = StoreId("local")
CENTRAL_STORE = Path("/data")
"""The chroot store the PVC holds; appstarter's initContainer fills it."""
BUILD_DIR = "/nix/var/nix/builds"
"""A builder's build directories, on its store volume."""


def _laid_over(settings: PynixdSettings, base: dict[str, object]) -> LocalSocketStoreSpec:
    """*base*, the pod's own store, with what `stores.local` in the settings says laid over it.

    Only the keys the settings name: `op_routes`, `pynixd_writes` and `daemon`
    take the store off its Nix daemon step by step (pynixd
    `docs/design/daemonless.md`), and a key left out keeps the pod's default.
    """
    configured = settings.stores.get(str(LOCAL))
    if configured is not None and not isinstance(configured, LocalSocketStoreSpec):
        raise ValueError(f"stores.local is the pod's own store: type local-socket, not {configured.type}")
    given = configured.model_dump(exclude_unset=True, exclude={"settings"}) if configured is not None else {}
    return LocalSocketStoreSpec.model_validate({**base, **given, "store_id": LOCAL, "type": "local-socket"})


def central_local_spec(settings: PynixdSettings) -> LocalSocketStoreSpec:
    """The PVC's store."""
    return _laid_over(settings, {"store_path": CENTRAL_STORE, "monitor": False})


def builder_local_spec(settings: PynixdSettings) -> LocalSocketStoreSpec:
    """A builder pod's store at `/`: a copy-on-write volume of the pod alone.

    `managed`: pynixd otherwise reads a store at `/` as the machine's, protected, and refuses to run a private
    daemon there; every builder died at ensure_daemon. The volume keeps the node's store prefix and path hashes,
    which lets a builder share paths with the node and with substituters (nixkube issue #19).
    """
    return _laid_over(
        settings,
        {
            "store_path": Path("/"),
            "managed": True,
            "monitor": False,
            "extra_args": ["--option", "build-dir", BUILD_DIR],
        },
    )
