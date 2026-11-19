# SPDX-License-Identifier: Apache-2.0

from pathlib import Path

from pynixd.config import LocalSocketStoreSpec, PynixdSettings
from pynixd.serde import StoreId

LOCAL = StoreId("local")
CENTRAL_STORE = Path("/data")
"""The chroot store the PVC holds; appstarter's initContainer fills it."""


def central_local_spec(settings: PynixdSettings) -> LocalSocketStoreSpec:
    """The PVC's store, with what `stores.local` in the settings says laid over it.

    Only the keys the settings name: `op_routes`, `pynixd_writes` and `daemon`
    take the store off its Nix daemon step by step (pynixd
    `docs/design/daemonless.md`), and a key left out keeps the PVC's default.
    """
    configured = settings.stores.get(str(LOCAL))
    if configured is not None and not isinstance(configured, LocalSocketStoreSpec):
        raise ValueError(f"stores.local is the PVC's store: type local-socket, not {configured.type}")
    given = configured.model_dump(exclude_unset=True, exclude={"settings"}) if configured is not None else {}
    return LocalSocketStoreSpec.model_validate(
        {"store_path": CENTRAL_STORE, "monitor": False, **given, "store_id": LOCAL, "type": "local-socket"}
    )
