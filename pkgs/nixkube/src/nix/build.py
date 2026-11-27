# SPDX-License-Identifier: Apache-2.0

import tempfile
import time
from pathlib import Path

import structlog
from cachetools import TTLCache
from kr8s.asyncio.objects import Pod

from ..cache import check_cache_connectivity, get_substituter_args
from ..constants import NIX_BUILD_TIMEOUT
from ..errors import BuildError, CommandTimeoutError, SubprocessError
from ..metrics import NIX_BUILD_DURATION, NIX_BUILDS
from ..store import extract_store_name, extract_store_paths
from ..subprocessing import try_console

logger = structlog.get_logger("nixkube.nix")

# Cache build args for 30s to avoid redundant cache pings during burst volume
# creation. Cache connectivity is stable during a pod's lifetime.
_build_args_cache = TTLCache[str, list[str]](maxsize=1, ttl=30)


async def get_build_args() -> list[str]:
    """Extra build arguments: the cache as a substituter, while it answers.

    Result is cached for 30 seconds to avoid redundant cache connectivity
    checks during burst volume creation.
    """
    # Check cache first
    cache_key = "build_args"
    if cache_key in _build_args_cache:
        return _build_args_cache[cache_key]

    extra_args = []

    # Add cache as substituter if available
    if await check_cache_connectivity():
        extra_args.extend(get_substituter_args())

    _build_args_cache[cache_key] = extra_args
    return extra_args


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
    extra_args: list[str],
    timeout: float = NIX_BUILD_TIMEOUT,
) -> Path:
    """Build/fetch a store path and create a gc root."""
    name = extract_store_name(store_path)
    return await _run_nix_build(
        [*extra_args, "--print-out-paths", "--out-link", gc_root / name, store_path],
        timeout=timeout,
        timeout_msg=f"Build timeout for {store_path} after {timeout}s",
        error_msg=f"Failed to build store path {store_path}",
        kind="store_path",
    )


async def build_flake_ref(
    flake_ref: str,
    gc_root: Path,
    extra_args: list[str],
    timeout: float = NIX_BUILD_TIMEOUT,
) -> Path:
    """Build a flake reference and create a gc root."""
    return await _run_nix_build(
        [*extra_args, "--print-out-paths", "--out-link", gc_root / "flake", flake_ref],
        timeout=timeout,
        timeout_msg=f"Build timeout for flake {flake_ref} after {timeout}s",
        error_msg=f"Failed to build flake {flake_ref}",
        kind="flake_ref",
    )


async def build_nix_expr(
    nix_expr: str,
    gc_root: Path,
    extra_args: list[str],
    timeout: float = NIX_BUILD_TIMEOUT,
) -> Path:
    """Build a Nix expression and create a gc root."""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".nix") as tmp:
        tmp.write(nix_expr)
        tmp.flush()
        return await _run_nix_build(
            [
                *extra_args,
                "--print-out-paths",
                "--out-link",
                gc_root / "expr",
                "--file",
                tmp.name,
            ],
            timeout=timeout,
            timeout_msg=f"Build timeout for Nix expression after {timeout}s",
            error_msg="Failed to build Nix expression",
            kind="nix_expr",
        )


async def fetch_packages(
    package_paths: set[Path],
    gc_root: Path,
    extra_args: list[str] | None = None,
) -> None:
    """Realize store paths with a single nix build call and pin them via gc root."""
    if not package_paths:
        return

    gc_root.mkdir(parents=True, exist_ok=True)

    args: list[str | Path] = ["nix", "build"]
    args.extend(extra_args or [])
    args.extend(["--out-link", gc_root / "build"])
    args.extend(package_paths)

    started = time.monotonic()
    try:
        await try_console(*args, timeout=NIX_BUILD_TIMEOUT)
    except SubprocessError as e:
        NIX_BUILDS.labels(kind="packages", result="error").inc()
        logger.error(
            "fetch_packages_failed",
            returncode=e.returncode,
            command=e.command,
            logs=e.combined,
        )
        raise
    except Exception:
        NIX_BUILDS.labels(kind="packages", result="error").inc()
        logger.exception("fetch_packages_failed")
        raise
    else:
        NIX_BUILDS.labels(kind="packages", result="ok").inc()
    finally:
        NIX_BUILD_DURATION.labels(kind="packages").observe(time.monotonic() - started)

    logger.debug("fetched_packages", count=len(package_paths))


async def build_pod_packages(
    pod: Pod,
    gc_root: Path,
    extra_args: list[str],
) -> set[Path]:
    """Extract and fetch packages referenced in the pod spec."""
    pod_store_paths = extract_store_paths(pod.raw)

    if not pod_store_paths:
        return set()

    await fetch_packages(pod_store_paths, gc_root, extra_args)
    return pod_store_paths


async def build_primary_package(
    store_path: str | None,
    flake_ref: str | None,
    nix_expr: str | None,
    gc_root: Path,
    extra_args: list[str],
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
        return await build_store_path(
            store_path,
            gc_root,
            extra_args,
            timeout=NIX_BUILD_TIMEOUT,
        )

    if flake_ref is not None:
        logger.debug("building_flake_ref", flake_ref=flake_ref)
        return await build_flake_ref(
            flake_ref,
            gc_root,
            extra_args,
            timeout=NIX_BUILD_TIMEOUT,
        )

    if nix_expr is not None:
        logger.debug("building_nix_expr", nix_expr=nix_expr)
        return await build_nix_expr(
            nix_expr,
            gc_root,
            extra_args,
            timeout=NIX_BUILD_TIMEOUT,
        )

    return None
