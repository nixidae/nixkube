# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from typing import TYPE_CHECKING

import anyio
import huggorm
import structlog

from ..constants import NIX_VERIFY_TIMEOUT
from ..errors import VerifyStorePathsError

if TYPE_CHECKING:
    from pathlib import Path

    from huggorm import AsyncStore, StorePath

logger = structlog.get_logger("nixkube.nix")


async def verify_store_paths(store: AsyncStore, package_paths: set[Path]) -> None:
    """Re-hash every path in the packages' closure against its recorded NAR hash.

    What `nix store verify --recursive --no-trust` checks: the contents, and
    not who signed them. Each path is a task; huggorm's runner bounds how many
    hash at once.

    Deadlined: this runs inside `NodePublishVolume`, so without a bound a store
    on a stuck filesystem leaves the pod waiting for ever and kubelet with
    nothing to retry.
    """
    # One line per path that failed. Collected, not raised, so the task group
    # ends with no exception and a caller catches one VerifyStorePathsError.
    failures: list[str] = []

    async def verify(path: StorePath) -> None:
        try:
            recorded = (await store.query_path_info(path)).nar_hash()
            actual = await store.compute_nar_hash(path, recorded.algorithm())
        except huggorm.errors.NixError as e:
            failures.append(f"{path.to_string()}: {e}")
            return
        if actual != recorded:
            failures.append(f"{path.to_string()}: recorded {recorded}, contents hash to {actual}")

    logger.debug("verify_store_paths", paths=package_paths)
    try:
        with anyio.fail_after(NIX_VERIFY_TIMEOUT):
            roots = [await store.parse_store_path(str(path)) for path in package_paths]
            closure = await store.compute_fs_closure(roots)
            async with anyio.create_task_group() as tg:
                for path in closure:
                    tg.start_soon(verify, path)
    except TimeoutError as e:
        raise VerifyStorePathsError(
            f"Store path verification timeout after {NIX_VERIFY_TIMEOUT}s",
            logs=str(e),
        ) from e
    except huggorm.errors.NixError as e:
        raise VerifyStorePathsError("Failed to verify store paths", logs=str(e)) from e
    if failures:
        raise VerifyStorePathsError(
            f"{len(failures)} store paths failed verification",
            logs="\n".join(sorted(failures)),
        )
