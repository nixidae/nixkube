# SPDX-License-Identifier: Apache-2.0

"""Realise a volume's packages in the node's store, and root them.

Store paths go through huggorm to the node's store server: `build_paths`
substitutes them, then `add_perm_root` links each, as `nix build
--out-link` did. A path is an opaque target, so a `.drv` in a pod spec is
fetched as a file, which is also what the volume's closure mounts.

`flakeRef` and `nixExpr` still evaluate in the `nix` CLI
(`docs/node-store.md`, the client table). For both, the server's nix.conf
names the substituters: a huggorm client sends none of its own.
"""

from __future__ import annotations

import tempfile
import time
from pathlib import Path
from typing import TYPE_CHECKING

import anyio
import huggorm
import structlog

from ..constants import NIX_BUILD_TIMEOUT
from ..errors import BuildError, CommandTimeoutError, SubprocessError
from ..metrics import NIX_BUILD_DURATION, NIX_BUILDS
from ..store import extract_store_name, extract_store_paths
from ..subprocessing import try_console
from ..tracing import annotate, step
from .node_store import node_store

if TYPE_CHECKING:
    from kr8s.asyncio.objects import Pod

logger = structlog.get_logger("nixkube.nix")


def out_links(out_link: Path, count: int) -> list[Path]:
    """`out_link`, then `out_link-1`, `out_link-2` and on: `nix build`'s names."""
    return [out_link if index == 0 else out_link.with_name(f"{out_link.name}-{index}") for index in range(count)]


async def realise_store_paths(
    rooted: list[tuple[Path, Path]],
    *,
    kind: str,
    error_msg: str,
    timeout: float = NIX_BUILD_TIMEOUT,
) -> None:
    """Make each path valid in the node's store and root it at its link.

    `kind` is a metric label: one of a fixed few words, never a path.
    """
    store = node_store()
    started = time.monotonic()
    try:
        with step("store build"), anyio.fail_after(timeout):
            annotate("nixkube.build.kind", kind)
            annotate("nixkube.build.paths", len(rooted))
            targets = [await store.parse_derived_path(str(path)) for path, _ in rooted]
            await store.build_paths(targets)
            for path, link in rooted:
                await store.add_perm_root(await store.parse_store_path(str(path)), str(link))
    except TimeoutError as e:
        NIX_BUILDS.labels(kind=kind, result="timeout").inc()
        raise BuildError(f"{error_msg}: timeout after {timeout}s") from e
    except huggorm.errors.NixError as e:
        NIX_BUILDS.labels(kind=kind, result="error").inc()
        # In the message, not `logs`: NRI reports `str()` of the error alone.
        raise BuildError(f"{error_msg}: {e}") from e
    else:
        NIX_BUILDS.labels(kind=kind, result="ok").inc()
    finally:
        NIX_BUILD_DURATION.labels(kind=kind).observe(time.monotonic() - started)


async def _run_nix_build(
    args: list[str | Path],
    timeout: float,
    timeout_msg: str,
    error_msg: str,
    kind: str,
) -> Path:
    """Run `nix build` with given args and translate errors to BuildError.

    `kind` says which volume attribute asked for this build. It is a metric
    label, so it is one of a fixed few words and never the path itself.
    """
    started = time.monotonic()
    try:
        result = await try_console("nix", "build", *args, timeout=timeout)
    except CommandTimeoutError as e:
        NIX_BUILDS.labels(kind=kind, result="timeout").inc()
        raise BuildError(timeout_msg, logs=e.combined) from e
    except SubprocessError as e:
        NIX_BUILDS.labels(kind=kind, result="error").inc()
        raise BuildError(error_msg, logs=e.combined) from e
    else:
        NIX_BUILDS.labels(kind=kind, result="ok").inc()
        return Path(result.stdout.splitlines()[0])
    finally:
        NIX_BUILD_DURATION.labels(kind=kind).observe(time.monotonic() - started)


async def build_store_path(
    store_path: str,
    gc_root: Path,
    timeout: float = NIX_BUILD_TIMEOUT,
) -> Path:
    """Fetch a store path and root it at `gc_root/<name>`."""
    await realise_store_paths(
        [(Path(store_path), gc_root / extract_store_name(store_path))],
        kind="store_path",
        error_msg=f"Failed to build store path {store_path}",
        timeout=timeout,
    )
    return Path(store_path)


async def build_flake_ref(
    flake_ref: str,
    gc_root: Path,
    timeout: float = NIX_BUILD_TIMEOUT,
) -> Path:
    """Build a flake reference and create a gc root."""
    return await _run_nix_build(
        ["--print-out-paths", "--out-link", gc_root / "flake", flake_ref],
        timeout=timeout,
        timeout_msg=f"Build timeout for flake {flake_ref} after {timeout}s",
        error_msg=f"Failed to build flake {flake_ref}",
        kind="flake_ref",
    )


async def build_nix_expr(
    nix_expr: str,
    gc_root: Path,
    timeout: float = NIX_BUILD_TIMEOUT,
) -> Path:
    """Build a Nix expression and create a gc root."""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".nix") as tmp:
        tmp.write(nix_expr)
        tmp.flush()
        return await _run_nix_build(
            ["--print-out-paths", "--out-link", gc_root / "expr", "--file", tmp.name],
            timeout=timeout,
            timeout_msg=f"Build timeout for Nix expression after {timeout}s",
            error_msg="Failed to build Nix expression",
            kind="nix_expr",
        )


async def fetch_packages(package_paths: set[Path], gc_root: Path) -> None:
    """Fetch store paths into the node's store, rooted at `gc_root/build`,
    `build-1` and on. Sorted, so a volume's links do not depend on set order."""
    if not package_paths:
        return
    await anyio.Path(gc_root).mkdir(parents=True, exist_ok=True)
    paths = sorted(package_paths)
    await realise_store_paths(
        list(zip(paths, out_links(gc_root / "build", len(paths)), strict=True)),
        kind="packages",
        error_msg=f"Failed to fetch {len(paths)} packages",
    )
    logger.debug("fetched_packages", count=len(paths))


async def build_pod_packages(pod: Pod, gc_root: Path) -> set[Path]:
    """Extract and fetch packages referenced in the pod spec."""
    pod_store_paths = extract_store_paths(pod.raw)
    await fetch_packages(pod_store_paths, gc_root)
    return pod_store_paths


async def build_primary_package(
    store_path: str | None,
    flake_ref: str | None,
    nix_expr: str | None,
    gc_root: Path,
) -> Path | None:
    """
    Build the primary package from various sources.

    Source selection order (intentional, documented in README):
    1. storePath - if present, use directly
    2. flakeRef - if storePath not present, build flake
    3. nixExpr - if neither above present, evaluate expression

    Users can specify multiple; first non-None in priority order is used.
    """
    if store_path is not None:
        logger.debug("building_store_path", store_path=store_path)
        return await build_store_path(store_path, gc_root)

    if flake_ref is not None:
        logger.debug("building_flake_ref", flake_ref=flake_ref)
        return await build_flake_ref(flake_ref, gc_root)

    if nix_expr is not None:
        logger.debug("building_nix_expr", nix_expr=nix_expr)
        return await build_nix_expr(nix_expr, gc_root)

    return None
