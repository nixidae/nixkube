# SPDX-License-Identifier: Apache-2.0

"""GC roots in a volume's chroot store, through huggorm.

Both calls run after `init_database`, so every path is already valid in the
volume. They only link and register: Nix's `addPermRoot`, which `nix build
--out-link` called, so the link and its indirect root under `gcroots/auto`
are what they were. A path that is not valid fails the call rather than
being rooted, where `nix build` would have tried to substitute it into the
volume.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

import anyio
import huggorm

from ..constants import NIX_BUILD_TIMEOUT
from ..errors import InstallGCRootError, InstallResultLinkError
from ..tracing import annotate, step

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path


class _NotValidError(Exception):
    """A path the volume's store does not hold."""


@asynccontextmanager
async def _volume_store(volume_root: Path) -> AsyncIterator[huggorm.AsyncStore]:
    store = huggorm.AsyncStore(str(volume_root))
    try:
        yield store
    finally:
        await store.aclose()


async def _root(store: huggorm.AsyncStore, path: Path, link: Path) -> None:
    store_path = await store.parse_store_path(str(path))
    if not await store.is_valid_path(store_path):
        raise _NotValidError(f"path '{path}' is not valid in the volume's store")
    await store.add_perm_root(store_path, str(link))


async def install_gcroots(
    package_paths: set[Path],
    out_link: Path,
    volume_root: Path,
    timeout: float = NIX_BUILD_TIMEOUT,
) -> None:
    """Root each package in the volume's store: `out_link` for the first,
    `out_link-1`, `out_link-2` and on for the rest, as `nix build` names
    them. Sorted, so a volume's links do not depend on set order.

    The deadline has a default rather than being optional. A caller that
    forgot one got an unbounded call on a request path, which is the shape
    issue #38 measured in the GC loop.
    """
    if not package_paths:
        return
    try:
        with step("store gc roots"), anyio.fail_after(timeout):
            annotate("nixkube.gcroots.paths", len(package_paths))
            async with _volume_store(volume_root) as store:
                for index, path in enumerate(sorted(package_paths)):
                    link = out_link if index == 0 else out_link.with_name(f"{out_link.name}-{index}")
                    await _root(store, path, link)
    except TimeoutError as e:
        raise InstallGCRootError(f"GC root installation timeout after {timeout}s", logs=str(e)) from e
    except (huggorm.errors.NixError, _NotValidError) as e:
        raise InstallGCRootError("Failed to install garbage collection root", logs=str(e)) from e


async def install_result_link(volume_root: Path, package_path: Path) -> None:
    """Link `/nix/var/result` in the volume to the primary package, as a root
    of the volume's store."""
    try:
        with step("store result link"), anyio.fail_after(NIX_BUILD_TIMEOUT):
            async with _volume_store(volume_root) as store:
                await _root(store, package_path, volume_root / "nix/var/result")
    except TimeoutError as e:
        raise InstallResultLinkError(f"Result link installation timeout after {NIX_BUILD_TIMEOUT}s", logs=str(e)) from e
    except (huggorm.errors.NixError, _NotValidError) as e:
        raise InstallResultLinkError("Failed to install /nix/var/result symlink", logs=str(e)) from e
