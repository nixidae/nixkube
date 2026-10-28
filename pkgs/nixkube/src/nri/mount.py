# SPDX-License-Identifier: Apache-2.0

"""Mount /nix and FHS store paths into a running container's mount namespace.

## Approach

/nix is mounted by one of two mechanisms, chosen by whether this container
gets a bind farm (`NRI_BIND_FARM`, issue #65) and whether it asked for
read-write access (nixkube/pod-rw or nixkube/{container}-rw):

  Farm: the worker unshares its own mount namespace and binds one read-only
    mount per closure path into volume_root/nix/store. `fs.mount-max` is
    100,000 per namespace, so this cannot happen in the daemon -- 2443 mounts
    per closure would cap it at about 40 containers. open_tree(2) with
    AT_RECURSIVE clones the whole tree onto a detached fd, move_mount(2)
    attaches it at /nix, and the namespace dies with the worker.

    Read-write is the same farm. Nix never mutates an existing store path, so
    the writable part is the gaps between the mounts, on the volume's own
    disk. Read-only adds mount_setattr(MOUNT_ATTR_RDONLY, AT_RECURSIVE) over
    the tree, because MS_REMOUNT|MS_RDONLY covers the top mount alone.

    Never an overlay lower layer: overlayfs does not follow submounts in one,
    so a farm used that way presents empty directories.

  Hardlink tree (NRI_BIND_FARM=false): the tree is already filled on disk.
    RO clones it with open_tree and remounts read-only; RW builds a detached
    overlayfs fd with fsopen/fsconfig/fsmount over
    lowerdir=volume_root/nix, upperdir=volume_root/upper,
    workdir=volume_root/work, pre-created by prepare_volume.

For FHS store mounts we use the traditional mount(2) MS_BIND approach (RW),
executed inside the container namespace after /nix is attached so that
/nix/store/... paths are reachable as bind-mount sources.

Note on syscall families: open_tree/move_mount and fsopen/fsconfig/fsmount serve
distinct purposes and always coexist — open_tree clones an existing mount tree
(the new-API equivalent of MS_BIND), while fsopen creates a new filesystem instance
(overlay, tmpfs, etc.). There is no fsopen("bind"). The meaningful future migration
for store mounts is therefore open_tree+move_mount, not fsopen: grabbing the mount
fds before setns would remove the ordering constraint where /nix must be mounted
first so /nix/store/... sources are visible.

## Execution context

When our createRuntime hook fires the container init process is alive with its
mount namespace established. pivot_root has NOT happened yet — the container's
root is still the host root. We use the fchdir+chroot trick to enter the
container's rootfs: open an O_PATH fd to the rootfs before setns (while the
host path is accessible), then fchdir+chroot after setns. The rootfs is the
bundle's `config.json` `root.path`, not `{bundle}/rootfs`: containerd puts it
there, CRI-O keeps it in containers-storage. See `rootfs_of`.

## Why multiprocessing.spawn

setns(2) affects the calling thread's namespace, which would contaminate the
asyncio event-loop thread. fork is unsafe inside asyncio. spawn starts a fresh
Python interpreter with no inherited async state and all state passed explicitly
via picklable arguments.

## Worker sequence

  0. farm           — unshare(CLONE_NEWNS), make / private, bind the closure
                      read-only into volume_root/nix/store. Farm only.
  1. /nix mount fd  — while the source paths are still visible:
       farm or RO: open_tree clone (AT_RECURSIVE carries the farm)
       RW hardlink: fsopen("overlay") → fsconfig(lowerdir/upper/work) →
                    CMD_CREATE → fsmount
  2. rootfs fd      — O_PATH to the rootfs while in daemonset namespace
  3. setns          — enter container mount namespace
  4. fchdir+chroot  — pivot into container rootfs
  5. move_mount     — attach /nix fd. RO then goes read-only:
                      mount_setattr(AT_RECURSIVE) for a farm, so the bound
                      paths under it are covered too; MS_REMOUNT otherwise.
  6. MS_BIND        — bind each FHS store path (read-write) inside container;
                      sources resolved after step 5 so /nix/store/... is visible.
                      (Future: open_tree+move_mount before setns removes this dependency)
"""

import ctypes
import errno
import multiprocessing
import os
import queue
import traceback
from functools import cache
from pathlib import Path

import anyio.to_thread
import structlog

from ..constants import (
    CSI_ROOT,
    HOST_PROC_PATH,
    MNT_DETACH,
    MS_BIND,
    MS_RDONLY,
    MS_REMOUNT,
)
from ..csi.composefs import BASEDIR, compose
from ..mountattr import set_readonly
from .farm import build_farm, detach_namespace

logger = structlog.get_logger("nixkube.nri.mount")

# setns(2) namespace flag
CLONE_NEWNS = 0x00020000

# openat/open_tree dirfd sentinel
AT_FDCWD = -100

# open_tree(2) flags (Linux 5.2, include/uapi/linux/mount.h)
OPEN_TREE_CLONE = 1  # clone the subtree into a detached mount namespace
OPEN_TREE_CLOEXEC = os.O_CLOEXEC
AT_RECURSIVE = 0x8000  # recurse into sub-mounts within the subtree

# move_mount(2) flags (Linux 5.2, include/uapi/linux/mount.h)
MOVE_MOUNT_F_EMPTY_PATH = 0x4  # from_dirfd is the mount fd; from_pathname ignored

# fsopen(2) flags (Linux 5.2, include/uapi/linux/mount.h)
FSOPEN_CLOEXEC = 0x1

# fsconfig(2) commands (Linux 5.2, include/uapi/linux/mount.h)
FSCONFIG_SET_STRING = 1  # set a string-valued parameter
FSCONFIG_CMD_CREATE = 6  # create the superblock (no key/value)

# fsmount(2) flags (Linux 5.2, include/uapi/linux/mount.h)
FSMOUNT_CLOEXEC = 0x1

# Linux syscall numbers for the new mount API.
# All introduced in Linux 5.2 via the common syscall table
# (include/uapi/asm-generic/unistd.h), so x86_64 and aarch64 share them.
_NR_OPEN_TREE = 428
_NR_MOVE_MOUNT = 429
_NR_FSOPEN = 430
_NR_FSCONFIG = 431
_NR_FSMOUNT = 432


@cache
def kernel_supports_ro() -> bool:
    """Test if kernel supports RO mounts (open_tree/move_mount). Result is cached."""
    libc = ctypes.CDLL(None, use_errno=True)
    test_path = Path("/tmp") if Path("/tmp").exists() else Path("/nix")
    try:
        fd = _open_tree(libc, test_path)
        os.close(fd)
        return True
    except OSError as e:
        if e.errno == errno.ENOSYS:
            logger.error("kernel_no_open_tree", requires="linux_5.2")
        else:
            logger.error("open_tree_test_failed", exc_info=e)
        return False


@cache
def kernel_supports_rw() -> bool:
    """Test if kernel supports new mount API for overlayfs (fsopen/fsconfig/fsmount). Result is cached.

    Probed on the filesystem of the real upper directories, not in /tmp,
    and not in NRI_CONTAINERS, whose sweep would take the probe for a
    stale volume. The container's
    /tmp is on its own rootfs, an overlay, and the kernel refuses an
    overlay upper on one: under CRI-O this said "unsupported" with
    fsconfig('upperdir') EINVAL, on a kernel whose volumes mount read-write
    fine. Measured on 2026-09-29.
    """
    import tempfile

    libc = ctypes.CDLL(None, use_errno=True)
    try:
        CSI_ROOT.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=CSI_ROOT) as tmpdir:
            tmppath = Path(tmpdir)
            lowerdir = tmppath / "lower"
            upperdir = tmppath / "upper"
            workdir = tmppath / "work"

            lowerdir.mkdir()
            upperdir.mkdir()
            workdir.mkdir()

            try:
                overlay_fd = _make_overlay_fd(libc, lowerdir, upperdir, workdir)
                os.close(overlay_fd)
                return True
            except OSError as e:
                if e.errno == errno.ENOSYS:
                    logger.debug("kernel_no_overlay_mount_api", requires="linux_6.5")
                else:
                    logger.debug("overlay_mount_api_test_failed", exc_info=e)
                return False
    except Exception:
        logger.debug("overlay_mount_api_test_unexpected", exc_info=True)
        return False


def _open_tree(libc: ctypes.CDLL, path: Path) -> int:
    """Clone the mount subtree at path; return an fd to the detached clone."""
    flags = OPEN_TREE_CLONE | AT_RECURSIVE | OPEN_TREE_CLOEXEC
    fd = libc.syscall(
        ctypes.c_long(_NR_OPEN_TREE),
        ctypes.c_int(AT_FDCWD),
        ctypes.c_char_p(os.fsencode(path)),
        ctypes.c_uint(flags),
    )
    if fd < 0:
        errno = ctypes.get_errno()
        raise OSError(errno, f"open_tree({path!r}): {os.strerror(errno)}")
    return int(fd)


def _move_mount(libc: ctypes.CDLL, from_fd: int, to_path: Path) -> None:
    """Attach the detached mount fd at to_path (resolved in current root)."""
    ret = libc.syscall(
        ctypes.c_long(_NR_MOVE_MOUNT),
        ctypes.c_int(from_fd),
        ctypes.c_char_p(b""),
        ctypes.c_int(AT_FDCWD),
        ctypes.c_char_p(os.fsencode(to_path)),
        ctypes.c_uint(MOVE_MOUNT_F_EMPTY_PATH),
    )
    if ret < 0:
        errno = ctypes.get_errno()
        raise OSError(errno, f"move_mount(fd={from_fd} → {to_path!r}): {os.strerror(errno)}")


def _make_overlay_fd(libc: ctypes.CDLL, lowerdir: Path, upperdir: Path, workdir: Path) -> int:
    """Build a detached RW overlayfs mount fd via fsopen/fsconfig/fsmount.

    Paths are resolved in the caller's namespace (daemonset), so this must be
    called before setns. The returned fd can be passed to move_mount after setns.
    The internal fsopen context fd is always closed before returning.
    """
    ctx_fd = int(
        libc.syscall(
            ctypes.c_long(_NR_FSOPEN),
            ctypes.c_char_p(b"overlay"),
            ctypes.c_uint(FSOPEN_CLOEXEC),
        )
    )
    if ctx_fd < 0:
        errno = ctypes.get_errno()
        raise OSError(errno, f"fsopen(overlay): {os.strerror(errno)}")

    try:
        for key, path in (
            ("lowerdir", lowerdir),
            ("upperdir", upperdir),
            ("workdir", workdir),
        ):
            ret = libc.syscall(
                ctypes.c_long(_NR_FSCONFIG),
                ctypes.c_int(ctx_fd),
                ctypes.c_uint(FSCONFIG_SET_STRING),
                ctypes.c_char_p(key.encode()),
                ctypes.c_char_p(os.fsencode(path)),
                ctypes.c_int(0),
            )
            if ret < 0:
                errno = ctypes.get_errno()
                raise OSError(errno, f"fsconfig({key!r}={path!r}): {os.strerror(errno)}")

        ret = libc.syscall(
            ctypes.c_long(_NR_FSCONFIG),
            ctypes.c_int(ctx_fd),
            ctypes.c_uint(FSCONFIG_CMD_CREATE),
            ctypes.c_char_p(None),
            ctypes.c_char_p(None),
            ctypes.c_int(0),
        )
        if ret < 0:
            errno = ctypes.get_errno()
            raise OSError(errno, f"fsconfig(CMD_CREATE): {os.strerror(errno)}")

        mnt_fd = int(
            libc.syscall(
                ctypes.c_long(_NR_FSMOUNT),
                ctypes.c_int(ctx_fd),
                ctypes.c_uint(FSMOUNT_CLOEXEC),
                ctypes.c_uint(0),  # attr_flags: 0 = read-write
            )
        )
        if mnt_fd < 0:
            errno = ctypes.get_errno()
            raise OSError(errno, f"fsmount: {os.strerror(errno)}")

        return mnt_fd
    finally:
        os.close(ctx_fd)


def rootfs_of(bundle: str, config: dict) -> str:
    """The container's rootfs, from its bundle's OCI `config.json`.

    The spec lets `root.path` be absolute or relative to the bundle.
    containerd writes "rootfs"; CRI-O writes an absolute path into
    containers-storage, and its bundle has no rootfs directory at all.
    """
    path = config["root"]["path"]
    return path if path.startswith("/") else f"{bundle}/{path}"


def _mount_worker(
    container_pid: int,
    rootfs: str,
    nix_tree_path: Path,
    store_mounts: list[tuple[Path, Path]],
    nix_rw: bool,
    result_queue: "multiprocessing.Queue[Exception | None]",
    host_proc_path: str,
    farm_paths: list[Path] | None = None,
    image: Path | None = None,
) -> None:
    """Worker that runs in a spawned process to mount /nix and store paths.

    Sends None on success or the Exception (with traceback in __notes__) on failure.
    All arguments are passed explicitly (spawn context, no inherited state).
    """
    try:
        libc = ctypes.CDLL("libc.so.6", use_errno=True)
        libc.syscall.restype = ctypes.c_long

        # Step 0: Build the farm, if this container gets one.
        #
        # Here and not in the daemon, because `fs.mount-max` is 100,000 per
        # mount namespace: at 2443 mounts per closure the daemon's own
        # namespace would hold about 40 containers. This process unshares
        # first, so the mounts belong to something that is about to exit.
        if farm_paths:
            detach_namespace()
            build_farm(nix_tree_path / "store", farm_paths)

        # Step 1: Build the /nix mount fd while source paths are still visible.
        # Both open_tree and fsmount return fds that survive setns.
        #
        # A farm is never an overlay lower layer: overlayfs does not follow
        # submounts in one, so the container would see empty directories where
        # the store paths are. `open_tree(CLONE | AT_RECURSIVE)` carries them,
        # and the writable side of a read-write farm is the gaps between the
        # mounts, on the volume's own disk.
        #
        # composefs: the overlay over the image, in this process's own
        # namespace like the farm, and its `nix` directory cloned out. The
        # overlay holds the image's EROFS mount, as it does for CSI, so
        # both outlive this namespace through the clone.
        if image is not None:
            detach_namespace()
            volume = nix_tree_path.parent
            merged = volume / "merged"
            compose(image, merged, BASEDIR, volume, volume if nix_rw else None)
            nix_fd = _open_tree(libc, merged / "nix")
        elif nix_rw and not farm_paths:
            nix_fd = _make_overlay_fd(
                libc,
                lowerdir=nix_tree_path,
                upperdir=nix_tree_path.parent / "upper",
                workdir=nix_tree_path.parent / "work",
            )
        else:
            nix_fd = _open_tree(libc, nix_tree_path)

        # Step 2: Open the container rootfs while still in the daemonset namespace.
        # /host/proc/{pid}/root reaches the container init's root; pre-pivot_root
        # that equals the host root, so appending the rootfs gives the OCI rootfs.
        rootfs_path = f"{host_proc_path}/{container_pid}/root{rootfs}"
        rootfs_fd = os.open(rootfs_path, os.O_PATH | os.O_DIRECTORY)

        # Step 3: Enter the container's mount namespace.
        with open(f"{host_proc_path}/{container_pid}/ns/mnt", "rb") as f:
            ret = libc.setns(f.fileno(), CLONE_NEWNS)
        if ret != 0:
            errno = ctypes.get_errno()
            os.close(rootfs_fd)
            raise OSError(errno, f"setns({container_pid}): {os.strerror(errno)}")

        # Step 4: Enter the container's rootfs using the fd opened before setns.
        os.fchdir(rootfs_fd)
        os.close(rootfs_fd)
        os.chroot(".")

        # Step 5: Attach /nix.
        nix_dest = Path("/nix")
        nix_dest.mkdir(exist_ok=True)
        _move_mount(libc, nix_fd, nix_dest)
        os.close(nix_fd)

        if not nix_rw:
            # Bind clone is RW by default; flip it to RO.
            #
            # `mount_setattr(AT_RECURSIVE)` for a farm, because
            # `MS_BIND | MS_REMOUNT | MS_RDONLY` changes the top mount alone
            # and every bound store path under it would stay writable --
            # straight into /nix/store on the node, through the shared inode.
            # The farm's binds are already read-only one by one; this is what
            # covers the tree they were cloned into.
            try:
                if farm_paths:
                    set_readonly("/nix", recursive=True)
                else:
                    ret = libc.mount(None, b"/nix", None, MS_BIND | MS_REMOUNT | MS_RDONLY, None)
                    if ret != 0:
                        errno = ctypes.get_errno()
                        raise OSError(errno, f"remount /nix RO: {os.strerror(errno)}")
            except OSError:
                # Do not leave a writable /nix exposed. MNT_DETACH because a
                # farm carries submounts, and a plain umount2 over those
                # answers EBUSY.
                libc.umount2(b"/nix", MNT_DETACH)
                raise

        # Step 6: Bind-mount each FHS store path (read-write) into the container.
        # /nix is mounted now, so /nix/store/... source paths are reachable.
        # Resolve symlinks after chroot so they follow the container's view.
        for src, dst in store_mounts:
            src = src.resolve()
            if src.is_dir():
                dst.mkdir(parents=True, exist_ok=True)
            else:
                dst.parent.mkdir(parents=True, exist_ok=True)
                dst.touch()
            ret = libc.mount(
                os.fsencode(src),
                os.fsencode(dst),
                None,
                MS_BIND,
                None,
            )
            if ret != 0:
                errno = ctypes.get_errno()
                raise OSError(
                    errno,
                    f"mount(bind {src!r} → {dst!r}): {os.strerror(errno)}",
                )

        result_queue.put(None)
    except Exception as e:  # noqa: BLE001 -- forwarded to the parent process through the queue, traceback attached
        e.__notes__ = [traceback.format_exc()]
        result_queue.put(e)


async def mount_in_container(
    container_pid: int,
    rootfs: str,
    nix_tree_path: Path,
    store_mounts: list[tuple[Path, Path]],
    nix_rw: bool = False,
    farm_paths: list[Path] | None = None,
    image: Path | None = None,
) -> None:
    """Mount /nix and FHS store paths inside a container's mount namespace.

    nix_tree_path: prepared /nix tree (lowerdir for overlayfs or bind source)
    store_mounts:  additional (src, dst) pairs for bind mounts (read-write)
    nix_rw:        True → writable /nix; False → read-only
    farm_paths:    the closure to bind into `nix_tree_path/store` inside the
                   worker's own namespace. Absent means the tree is already
                   filled -- the hardlink path.
    image:         a composefs image to present instead of `nix_tree_path`.
    Raises the worker's original exception (with traceback) on failure.
    """
    ctx = multiprocessing.get_context("spawn")
    result_queue: multiprocessing.Queue = ctx.Queue()

    proc = ctx.Process(
        target=_mount_worker,
        args=(
            container_pid,
            rootfs,
            nix_tree_path,
            store_mounts,
            nix_rw,
            result_queue,
            HOST_PROC_PATH,
            farm_paths,
            image,
        ),
        daemon=True,
    )
    proc.start()
    # `abandon_on_cancel`: a cancelled mount must not wait for a worker that
    # is itself stuck. The thread is left to finish against a daemon process
    # that dies with the daemon.
    await anyio.to_thread.run_sync(proc.join, abandon_on_cancel=True)

    try:
        result = result_queue.get_nowait()
    except queue.Empty:
        # The worker died without answering: OOM-killed, or a segfault in
        # setns. `exitcode` is negative for a signal. Without this the
        # caller sees a bare `Empty` and nothing about the process.
        raise RuntimeError(f"mount worker for pid {container_pid} left no result (exit code {proc.exitcode})") from None
    if isinstance(result, Exception):
        logger.error("worker_spawn_failed", exc_info=result)
        raise result

    logger.info(
        "mounted_nix",
        mode="rw" if nix_rw else "ro",
        presentation="composefs" if image else "farm" if farm_paths else "hardlinks",
        store_mounts=len(store_mounts),
        pid=container_pid,
    )
