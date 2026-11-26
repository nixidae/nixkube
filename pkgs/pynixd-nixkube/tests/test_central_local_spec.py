# SPDX-License-Identifier: Apache-2.0

"""The central pod's store is the PVC's, with `stores.local` from the settings laid over it.

The pod dropped `stores.local` from its settings, so no deployment could set
`op_routes`, `pynixd_writes` or `daemon` on it: the steps that take a store
off its Nix daemon (pynixd `docs/design/daemonless.md`).
"""

from pathlib import Path

import pytest
from pynixd.config import PynixdSettings
from pynixd.op_routes import OpRoute

from pynixd_nixkube.central_store import BUILD_DIR, CENTRAL_STORE, builder_local_spec, central_local_spec


def _settings(local: dict[str, object] | None) -> PynixdSettings:
    return PynixdSettings.model_validate({"stores": {} if local is None else {"local": local}})


def test_no_local_store_in_the_settings_serves_the_pvc() -> None:
    spec = central_local_spec(_settings(None))
    assert spec.store_path == CENTRAL_STORE
    assert spec.monitor is False


def test_the_settings_route_the_pvcs_store() -> None:
    spec = central_local_spec(
        _settings(
            {
                "type": "local-socket",
                "pynixd_writes": True,
                "op_routes": {"NarFromPath": "shadow", "AddToStoreNar": "pynixd"},
            }
        )
    )
    assert spec.store_path == CENTRAL_STORE, "a key the settings leave out keeps the PVC's default"
    assert spec.monitor is False
    assert spec.pynixd_writes
    assert spec.op_routes == {"NarFromPath": OpRoute.shadow, "AddToStoreNar": OpRoute.pynixd}


def test_the_settings_take_the_daemon_away() -> None:
    spec = central_local_spec(_settings({"type": "local-socket", "pynixd_writes": True, "daemon": False}))
    assert spec.daemon is False
    assert spec.store_path == CENTRAL_STORE


def test_a_key_the_settings_name_wins() -> None:
    spec = central_local_spec(_settings({"type": "local-socket", "store_path": "/elsewhere"}))
    assert spec.store_path == Path("/elsewhere")


def test_a_local_store_of_another_type_is_refused() -> None:
    with pytest.raises(ValueError, match="local-socket"):
        central_local_spec(_settings({"type": "http-binary-cache", "url": "https://cache.example"}))


def test_a_builder_s_store_is_its_volume_and_the_settings_take_its_daemon_away() -> None:
    """A builder ran Nix's daemon whatever its settings said: its store was fixed in the code."""
    stock = builder_local_spec(_settings(None))
    assert (stock.store_path, stock.managed, stock.daemon) == (Path("/"), True, True)
    assert stock.extra_args == ["--option", "build-dir", BUILD_DIR]
    daemonless = builder_local_spec(_settings({"type": "local-socket", "pynixd_writes": True, "daemon": False}))
    assert (daemonless.store_path, daemonless.managed, daemonless.daemon) == (Path("/"), True, False)
    assert daemonless.pynixd_writes
