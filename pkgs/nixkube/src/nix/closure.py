# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import anyio
import huggorm

from ..constants import NIX_PATH_INFO_TIMEOUT
from ..errors import StorePathClosureError

if TYPE_CHECKING:
    from huggorm import AsyncStore


async def get_closure_paths(store: AsyncStore, package_paths: set[Path]) -> set[Path]:
    """Every store path in the closure of the given packages.

    What `nix-store --query --requisites` answers. A path the store does not
    hold fails the call, as it fails `nix path-info --recursive`.

    Deadlined: this runs inside `NodePublishVolume` and inside the NRI build
    task, and neither has anything above it that would give up.
    """
    try:
        with anyio.fail_after(NIX_PATH_INFO_TIMEOUT):
            roots = [await store.parse_store_path(str(path)) for path in package_paths]
            closure = await store.compute_fs_closure(roots)
            store_dir = Path(await store.store_dir())
    except TimeoutError as e:
        raise StorePathClosureError(
            f"Store path closure timeout after {NIX_PATH_INFO_TIMEOUT}s",
            logs=str(e),
        ) from e
    except huggorm.errors.NixError as e:
        raise StorePathClosureError("Failed to get store path closure", logs=str(e)) from e
    return {store_dir / path.to_string() for path in closure}
