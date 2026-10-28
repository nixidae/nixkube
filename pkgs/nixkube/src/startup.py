# SPDX-License-Identifier: Apache-2.0

"""One-shot container setup, porting environments/modules/setup.nix to Python.

Creates required directories, hardlinks well-known paths (binSh, caCertificates,
usrBinEnv) into the container root, and fixes the Nix gcroot symlink.

When SETUP_BINSH is empty (dev/test environment — makeWrapperArgs not active),
this is a no-op so unit tests and local dev work without a Nix store present.
"""

import os
from pathlib import Path

import structlog

from .constants import SETUP_BINSH, SETUP_CACERTS, SETUP_USRBINENV
from .hardlinks import hardlink_tree
from .subprocessing import try_console

logger = structlog.get_logger("nixkube.startup")


def _write_if_missing(path: Path, content: str) -> None:
    """Write content to path only if it doesn't already exist."""
    if not path.exists():
        path.write_text(content)


async def run_setup() -> None:
    """Run one-shot container setup.

    Creates required directories, copies well-known paths into the container
    root, and fixes the /nix/var/result gcroot symlink.

    No-op when SETUP_BINSH is empty (dev/test environment).
    """
    if not SETUP_BINSH:
        logger.debug("setup_skipped", reason="SETUP_BINSH not set")
        return

    logger.info("setup_starting")

    # Create required directories with correct permissions
    for path, mode in [
        ("/tmp", 0o1777),
        ("/var/tmp", 0o1777),
        ("/var/log", 0o755),
        ("/nix/var/nix-csi", 0o755),
    ]:
        os.makedirs(path, exist_ok=True)
        os.chmod(path, mode)

    # Hardlink well-known paths into the container root filesystem
    root = Path("/")
    for src_str in [SETUP_BINSH, SETUP_CACERTS, SETUP_USRBINENV]:
        if src_str:
            src = Path(src_str)
            for entry in os.scandir(src):
                await hardlink_tree(Path(entry.path), root / entry.name)

    # Write /etc/passwd, /etc/group, /etc/nsswitch.conf.
    # Mirrors users.nix: root, nix (ssh to cache), nixbld1-32 (nix sandboxed builds).
    etc = Path("/etc")
    etc.mkdir(exist_ok=True)
    nixbld_passwd = "\n".join(
        f"nixbld{i}:x:{30000 + i}:30000:Nix build user {i}:/var/empty:/sbin/nologin" for i in range(1, 33)
    )
    nixbld_group_members = ",".join(f"nixbld{i}" for i in range(1, 33))
    _write_if_missing(
        etc / "passwd",
        "root:x:0:0:root:/nix/var/nix-csi/root:/bin/sh\n"
        "nix:x:1000:1000:Nix worker user:/:/sbin/nologin\n"
        "nobody:x:65534:65534:Nobody:/:/sbin/nologin\n"
        f"{nixbld_passwd}\n",
    )
    _write_if_missing(
        etc / "group",
        f"root:x:0:\nnix:x:1000:\nnobody:x:65534:\nnixbld:x:30000:{nixbld_group_members}\n",
    )
    _write_if_missing(
        etc / "nsswitch.conf",
        "hosts: files dns\npasswd: files\ngroup: files\nshadow: files\n",
    )

    # Fix gcroot symlinks: nix build re-registers /nix/var/result so the
    # gcroot chain points to /nix/var/result (not the stale /nix-volume path
    # left by the initContainer).
    #
    # Only the registration is stale. The symlink target is already right: a
    # chroot-store build writes the logical `/nix/store/...` path, not the
    # `--store` prefix. Measured, because reading this as "the target names
    # /nix-volume" makes it look like the cause of a failure to exec out of
    # /nix/var/result/bin, and it is not. See issue #16.
    await try_console(
        "nix",
        "build",
        "--store",
        "local",
        "--out-link",
        "/nix/var/result",
        "/nix/var/result",
    )

    logger.info("setup_complete")
