# SPDX-License-Identifier: Apache-2.0

"""The seccomp profile of pynixd's build pods, on this node. nixidae/nixidae#1560.

A build pod's sandbox container names a `Localhost` profile, which the kubelet
reads from its own `seccomp` directory when the container starts. nixkube owns
one subdirectory there, `nixkube/`, and makes it hold exactly the profiles the
deployment wants: the build-pod profile while the mode is on, and no
directory at all while it is off.
"""

import shutil
from collections.abc import Mapping
from pathlib import Path

import structlog

from .constants import BUILD_POD_SECCOMP, BUILD_PODS_ENABLED, KUBELET_SECCOMP_DIR

logger = structlog.get_logger("nixkube.build_pods")

SECCOMP_SUBDIR = "nixkube"
PROFILE_NAME = "pynixd-builder.json"


def reconcile_profiles(directory: Path, wanted: Mapping[str, bytes]) -> None:
    """Make *directory* hold exactly *wanted*, by name and content; remove it when *wanted* is empty."""
    if not wanted and not directory.exists():
        return
    directory.mkdir(mode=0o755, parents=True, exist_ok=True)
    for name, content in wanted.items():
        target = directory / name
        if target.is_file() and not target.is_symlink() and target.read_bytes() == content:
            continue
        # A rename, so a container that starts meanwhile reads the old file or the new one, never half of one.
        temporary = directory / f".{name}.tmp"
        temporary.write_bytes(content)
        temporary.chmod(0o644)
        temporary.replace(target)
        logger.info("seccomp_profile_installed", path=str(target))
    for entry in directory.iterdir():
        if entry.name in wanted:
            continue
        if entry.is_dir() and not entry.is_symlink():
            shutil.rmtree(entry)
        else:
            entry.unlink()
        logger.info("seccomp_profile_removed", path=str(entry))
    if not wanted:
        directory.rmdir()
        logger.info("seccomp_directory_removed", path=str(directory))


def wanted_profiles(*, enabled: bool, profile: str) -> dict[str, bytes]:
    """The profiles this node holds: the build-pod profile when the mode is on, none when it is off."""
    if not enabled:
        return {}
    if not profile:
        raise RuntimeError("build pods are on, but this nixkube carries no seccomp profile (NIXKUBE_BUILD_POD_SECCOMP)")
    return {PROFILE_NAME: Path(profile).read_bytes()}


def install_profiles() -> None:
    """Bring this node's profiles in line with `BUILD_PODS_ENABLED`. Blocking: run it in a thread."""
    reconcile_profiles(
        KUBELET_SECCOMP_DIR / SECCOMP_SUBDIR,
        wanted_profiles(enabled=BUILD_PODS_ENABLED, profile=BUILD_POD_SECCOMP),
    )
