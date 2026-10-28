# SPDX-License-Identifier: Apache-2.0

import ctypes
import ctypes.util
import errno
import os
import shutil
import time
from dataclasses import dataclass
from pathlib import Path

import structlog

from . import presentation
from .constants import (
    MNT_DETACH,
    MS_BIND,
    MS_RDONLY,
    MS_REC,
    MS_REMOUNT,
    NIX_BUILD_TIMEOUT,
    VERIFY_STORE_PATHS,
)
from .errors import FailedVolumeCleanupError, MountError, UnmountError
from .hardlinks import deref_hardlink_tree, hardlink_closure
from .mountattr import MountSetattrUnsupported, set_readonly
from .mountbudget import affordable
from .nix import (
    get_closure_paths,
    init_database,
    install_gcroots,
    install_result_link,
    verify_store_paths,
)
from .presentation import Presentation

logger = structlog.get_logger("nixkube.volume")

# Load libc for mount/umount syscalls
_libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)

# Get references to syscall functions
_libc.mount.argtypes = [
    ctypes.c_char_p,
    ctypes.c_char_p,
    ctypes.c_char_p,
    ctypes.c_ulong,
    ctypes.c_char_p,
]
_libc.mount.restype = ctypes.c_int
_libc.umount2.argtypes = [ctypes.c_char_p, ctypes.c_int]
_libc.umount2.restype = ctypes.c_int


def _set_readonly_recursive(target_path: Path) -> None:
    """Make `target_path` read-only, and every mount under it too.

    A kernel without mount_setattr answers ENOSYS, and then the remount below
    is correct: such a volume carries no submounts to miss.
    """
    try:
        set_readonly(target_path, recursive=True)
        return
    except MountSetattrUnsupported:
        pass
    except OSError as error:
        raise MountError(
            f"Failed to make bind volume read-only: {error}",
            logs="",
        ) from error

    logger.debug("mount_setattr_unavailable", path=str(target_path))
    ret = _libc.mount(
        None,
        ctypes.c_char_p(os.fsencode(target_path)),
        None,
        MS_BIND | MS_REMOUNT | MS_RDONLY,
        None,
    )
    if ret != 0:
        err = ctypes.get_errno()
        raise MountError(
            f"Failed to remount bind volume read-only: {os.strerror(err)} (errno {err})",
            logs="",
        )


@dataclass(frozen=True)
class PreparedVolume:
    """A volume that is ready, and how its store will be presented."""

    paths: list[Path]
    """The closure, in the order a farm should bind it."""

    bind_farm: bool
    """True when nothing was copied and the caller must build the farm."""


async def prepare_volume(
    volume_root: Path,
    package_paths: set[Path],
    primary_package: Path | None,
    bind_farm: bool = False,
    allow_hardlinks: bool = True,
) -> PreparedVolume:
    """Prepare a volume root, and answer the closure and the layout it got.

    Both layouts come through here, because they differ in one step out of
    five and a second copy of the other four is a second thing to get wrong.

      hardlinks  every closure path is linked into the volume, here and now.
      bind farm  nothing is copied. The caller binds the returned closure.

    `bind_farm` asks for a farm; the answer may still be no. Past
    `MOUNT_BUDGET_FALLBACK_RATIO` of `fs.mount-max` this takes a hardlink tree
    instead and says so at error level. That costs this volume time and disk.
    Spending the last of the namespace's mounts would cost the *next*
    container its start, with an ENOSPC that names no limit.

    An NRI farm effectively never reaches that point: it is built in the mount
    worker, whose namespace dies with it, so containers do not accumulate
    mounts on the node. A CSI farm would, which is what the fallback is for.
    """
    # Capitalized to emphasise they're Nix environment variables
    NIX_STATE_DIR = volume_root / "nix/var/nix"
    NIX_STATE_DIR.mkdir(parents=True, exist_ok=True)

    # Verify all packages and their closures before processing
    if VERIFY_STORE_PATHS:
        await verify_store_paths(package_paths)

    # Get storepaths from all packages
    store_paths = await get_closure_paths(package_paths)

    # Asked before anything is prepared, because the fallback changes what
    # preparing means.
    fallback = presentation.farm_fallback(fits=affordable(len(store_paths)), host_store=not allow_hardlinks)
    if bind_farm and fallback is not Presentation.FARM:
        if fallback is Presentation.REFUSED:
            # A host-store node: a hardlink out of the host's store fails
            # with EXDEV, so there is no tree to fall back to. Issue #25.
            raise MountError(
                f"{len(store_paths)} bind mounts would not leave headroom under"
                " fs.mount-max, and this node shares its host's store, so it has"
                " no hardlink tree to fall back to",
                logs="",
            )
        logger.error(
            "farm_fell_back_to_hardlinks",
            volume_root=str(volume_root),
            count=len(store_paths),
        )
        bind_farm = False

    if not bind_farm:
        # Pre-create overlayfs upper/work dirs so they're ready if the
        # container requests a RW /nix mount. A farm needs neither: it is
        # writable in the gaps between its mounts.
        (volume_root / "upper").mkdir(parents=True, exist_ok=True)
        (volume_root / "work").mkdir(parents=True, exist_ok=True)

    if not bind_farm:
        # This block is essentially nix copy into a chroot store with
        # extra steps. (Hardlinking instead of dumbcopying)
        hardlink_start = time.perf_counter()
        await hardlink_closure(store_paths, volume_root / "nix/store")
        logger.debug(
            "hardlinked_paths",
            volume_root=volume_root,
            count=len(store_paths),
            elapsed=round(time.perf_counter() - hardlink_start, 3),
        )

    # Create Nix database
    await init_database(NIX_STATE_DIR, store_paths)

    if not bind_farm:
        # Install gcroots in container using chroot store. This is
        # required because the auto roots created for /nix/var/result
        # will point to Narnia while this one points into store.
        #
        # A farm gets none. Its store lives only as long as the container, so
        # a root inside it protects nothing, and `nix build --store <volume>`
        # over paths that are not there yet would try to realise them into
        # it -- copying the closure the farm exists to avoid. The root that
        # matters is the one `fetch_packages` leaves in the node's store.
        await install_gcroots(
            package_paths,
            NIX_STATE_DIR / "gcroots" / "csi",
            store=volume_root,
            timeout=NIX_BUILD_TIMEOUT,
        )

        # Install /nix/var/result in container using chroot store
        if primary_package is not None:
            await install_result_link(volume_root, primary_package)
            # Create hardlink farm of primary package to volume_root
            deref_start = time.perf_counter()
            await deref_hardlink_tree(primary_package, volume_root)
            logger.debug(
                "deref_hardlink_tree_done",
                elapsed=round(time.perf_counter() - deref_start, 3),
            )

    return PreparedVolume(paths=sorted(store_paths), bind_farm=bind_farm)


async def mount_volume(
    volume_root: Path,
    target_path: Path,
    readonly: bool,
) -> None:
    """Mount the volume root to the target path using syscalls."""
    # Check source and target paths before attempting mount
    if not volume_root.exists():
        raise MountError(
            f"Source path does not exist: {volume_root}",
            logs="",
        )

    target_path.mkdir(parents=True, exist_ok=True)
    if not target_path.exists():
        raise MountError(
            f"Target path could not be created: {target_path}",
            logs="",
        )

    if readonly:
        # For readonly we use a bind mount, the benefit is that different
        # container stores using bindmounts will get the same inodes and
        # share page cache with others, reducing memory usage.
        logger.debug("mounting_bind_readonly", src=str(volume_root), dst=str(target_path))
        #
        # MS_REC because a volume root may hold mounts of its own: a store
        # presented as one bind mount per path (issue #65). Without it the
        # pod sees the mountpoints as empty directories -- measured 0 of 3
        # paths readable. It is a no-op against a volume that holds none.
        ret = _libc.mount(
            ctypes.c_char_p(os.fsencode(volume_root)),
            ctypes.c_char_p(os.fsencode(target_path)),
            None,
            MS_BIND | MS_REC | MS_RDONLY,
            None,
        )
        if ret != 0:
            err = ctypes.get_errno()
            if err == errno.EEXIST:
                pass  # Already mounted is fine
            else:
                logger.error(
                    "volume_mount_failed",
                    error=os.strerror(err),
                    error_code=err,
                    source=volume_root,
                    target=target_path,
                    source_exists=volume_root.exists(),
                    target_exists=target_path.exists(),
                )
                raise MountError(
                    f"Failed to mount bind volume: {os.strerror(err)} (errno {err}). "
                    f"source_exists={volume_root.exists()}, target_exists={target_path.exists()}, "
                    f"target_parent_exists={target_path.parent.exists()}",
                    logs="",
                )
        # The MS_RDONLY above is advisory on older kernels, and it never
        # covers submounts. Enforce it over the whole subtree.
        try:
            _set_readonly_recursive(target_path)
        except MountError:
            # Do not leave a writable store exposed. MNT_DETACH because the
            # bind is recursive: a plain umount2 of a mount that carries
            # submounts fails with EBUSY.
            _libc.umount2(ctypes.c_char_p(os.fsencode(target_path)), ctypes.c_int(MNT_DETACH))
            raise
    else:
        # For readwrite we use an overlayfs mount, the benefit here is that
        # it works as CoW even if the underlying filesystem doesn't support
        # it, reducing host storage usage.
        workdir = volume_root / "work"
        upperdir = volume_root / "upper"
        workdir.mkdir(parents=True, exist_ok=True)
        upperdir.mkdir(parents=True, exist_ok=True)

        if not workdir.exists() or not upperdir.exists():
            raise MountError(
                f"Failed to create overlay directories: workdir={workdir.exists()}, upperdir={upperdir.exists()}",
                logs="",
            )

        options = f"lowerdir={volume_root},upperdir={upperdir},workdir={workdir}".encode()
        logger.debug("mounting_overlay", src=str(volume_root), dst=str(target_path))
        ret = _libc.mount(
            ctypes.c_char_p(b"overlay"),
            ctypes.c_char_p(os.fsencode(target_path)),
            ctypes.c_char_p(b"overlay"),
            0,
            ctypes.c_char_p(options),
        )
        if ret != 0:
            err = ctypes.get_errno()
            if err == errno.EEXIST:
                pass  # Already mounted is fine
            else:
                raise MountError(
                    f"Failed to mount overlay volume: {os.strerror(err)} (errno {err}). "
                    f"lowerdir_exists={volume_root.exists()}, target_exists={target_path.exists()}, "
                    f"target_parent_exists={target_path.parent.exists()}",
                    logs="",
                )


def is_mount_source(path: Path, mountinfo_file: Path | None = None) -> bool:
    """Is `path` the source of a mount that is live right now?

    Field 4 of a /proc/self/mountinfo line is the mounted subtree's path
    inside its filesystem, which for a bind mount is the source directory.
    The kernel appends "//deleted" to it once that directory is unlinked, so
    a match here also catches a source somebody has already destroyed.

    /proc/self/mounts cannot answer this: it prints the *device* for a bind
    mount, not the directory. That is why `is_mount` cannot be reused.
    """
    if mountinfo_file is None:
        mountinfo_file = Path("/proc/self/mountinfo")

    try:
        wanted = str(path.resolve())
        for line in mountinfo_file.read_text().splitlines():
            fields = line.split()
            if len(fields) >= 4 and fields[3].removesuffix("//deleted") == wanted:
                return True
        return False
    except (FileNotFoundError, OSError):
        # Unreadable means unknown, and unknown must not authorise a delete.
        logger.warning("mountinfo_check_failed", mountinfo_file=str(mountinfo_file), exc_info=True)
        return True


def cleanup_failed_volume(gc_root: Path, volume_root: Path) -> None:
    """Clean up resources after a failed volume operation.

    A volume root that something is still mounted from is not cleaned up. It
    belongs to a running pod, and deleting it does not undo the mount -- the
    kernel keeps serving the unlinked directory and marks it "//deleted", so
    the pod reads an empty store and no later publish can repair the mount,
    because the kernel refuses to mount onto a deleted mount root.

    That is how one transient build error used to destroy a pod that had
    been running happily for an hour.
    """
    failed_paths = []
    for path in [gc_root, volume_root]:
        if path == volume_root and is_mount_source(path):
            logger.info("cleanup_skipped_live_mount", path=str(path))
            continue
        if path.exists():
            try:
                shutil.rmtree(path)
            except OSError as e:
                failed_paths.append(f"{path}: {e}")

    if failed_paths:
        raise FailedVolumeCleanupError(
            "Failed to clean up volume resources",
            logs="\n".join(failed_paths),
        )


def is_mount(path: Path, mounts_file: Path | None = None) -> bool:
    """Check if a path is a mount point by reading the mounts file.

    Format: filesystem mountpoint fstype options dump pass
    We just need to check if path matches index 1 (mountpoint).

    /proc/self/mounts is a virtual file that never blocks, so synchronous read is safe.

    Args:
        path: Path to check if it's a mount point
        mounts_file: Path to mounts file (default: /proc/self/mounts)
    """
    if mounts_file is None:
        mounts_file = Path("/proc/self/mounts")

    try:
        path_resolved = path.resolve()
        path_str = str(path_resolved)

        content = mounts_file.read_text()
        for line in content.splitlines():
            parts = line.split()
            if len(parts) >= 2 and parts[1] == path_str:
                return True
        return False
    except (FileNotFoundError, OSError):
        logger.warning("mounts_check_failed", mounts_file=str(mounts_file), exc_info=True)
        return False


async def unmount(path: Path, mounts_file: Path | None = None) -> None:
    """Unmount a path using syscall. Raises UnmountError if the mount persists.

    Args:
        path: Path to unmount
        mounts_file: Path to mounts file for checking (default: /proc/self/mounts)
    """
    logger.debug("unmounting", path=str(path))
    ret = _libc.umount2(
        ctypes.c_char_p(os.fsencode(path)),
        ctypes.c_int(0),
    )
    err = ctypes.get_errno() if ret != 0 else 0

    if ret != 0:
        logger.warning(
            "umount2_error",
            path=str(path),
            error=os.strerror(err),
            errno=err,
        )

    # Always verify the mount is actually gone — umount2 can return 0
    # but leave the mount (e.g. propagation), or return non-zero for a
    # non-fatal reason (e.g. EINVAL when already unmounted).
    if is_mount(path, mounts_file=mounts_file):
        raise UnmountError(
            f"Mount still present after unmount: {os.strerror(err)} (errno {err})"
            if err
            else f"umount2 succeeded but mount still present at {path}",
            logs="",
        )
