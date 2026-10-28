# SPDX-License-Identifier: Apache-2.0

import json
import shutil
from pathlib import Path

import structlog

from ..constants import CSI_GCROOTS, CSI_VOLUMES, KUBELET_PODS_PATH
from ..volume import is_mount, is_mount_source

# In a composefs volume's root: the target it is mounted at. Its mount's
# source is the image, never the volume root, so `is_mount_source` cannot
# see that the volume is live; whether this target is mounted can.
TARGET_FILE = "target"


def target_is_mounted(volume: Path) -> bool:
    try:
        return is_mount(Path((volume / TARGET_FILE).read_text().strip()))
    except FileNotFoundError:
        return False


logger = structlog.get_logger("nixkube.csi")


def collect_active_volume_handles(exclude_vol_data_path: Path | None) -> set[str]:
    """
    Collect volumeHandle values from all active CSI volumes on this node.

    Scans kubelet's pod volume directories for vol_data.json files and extracts
    the volumeHandle from each. This provides a snapshot of which volumes are
    currently in use by pods.
    """
    active_handles: set[str] = set()

    for vol_data_path in KUBELET_PODS_PATH.glob("*/volumes/kubernetes.io~csi/*/vol_data.json"):
        if exclude_vol_data_path and vol_data_path == exclude_vol_data_path:
            continue

        try:
            data = json.loads(vol_data_path.read_text())
            if handle := data.get("volumeHandle"):
                active_handles.add(handle)
        except (OSError, json.JSONDecodeError):
            logger.warning("vol_data_read_failed", path=str(vol_data_path), exc_info=True)

    return active_handles


def cleanup_stale_entries(active_handles: set[str]) -> None:
    """
    Remove gcroot and volume directories that are no longer in use.

    Iterates through CSI_GCROOTS and CSI_VOLUMES directories and removes any
    entries whose name (volume ID) is not in the set of active volume handles.
    """
    for gcroot in CSI_GCROOTS.iterdir():
        # A composefs volume reads every file from the store path itself, so
        # its root has to outlive any short list while the mount is there; a
        # hardlink tree held its inodes and did not need this.
        if target_is_mounted(CSI_VOLUMES / gcroot.name):
            logger.info("stale_gcroot_still_mounted", path=str(gcroot))
            continue
        if gcroot.name not in active_handles:
            try:
                shutil.rmtree(gcroot, ignore_errors=True)
                logger.debug("removed_stale_gcroot", path=str(gcroot))
            except Exception:
                logger.warning("remove_gcroot_failed", path=str(gcroot), exc_info=True)

    for volume in CSI_VOLUMES.iterdir():
        if volume.name not in active_handles:
            # A volume kubelet no longer lists, but the kernel still mounts,
            # belongs to a pod that is still running. `active_handles` comes
            # from reading vol_data.json files, and a kubelet that is
            # restarting can leave that answer short. The mount table cannot
            # be short: it is what the pod is actually using.
            if is_mount_source(volume) or target_is_mounted(volume):
                logger.info("stale_volume_still_mounted", path=str(volume))
                continue
            try:
                shutil.rmtree(volume)
                logger.debug("removed_stale_volume", path=str(volume))
            except Exception:
                logger.warning("remove_volume_failed", path=str(volume), exc_info=True)
