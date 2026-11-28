# SPDX-License-Identifier: Apache-2.0

"""The `reason` of each Kubernetes Event nixkube writes, as the cluster shows it.

No imports beyond the standard library: the guest tests (`nix/uml`) import
this file too, so a check and the code it checks name an event the same way.
"""

from __future__ import annotations

from enum import StrEnum


class EventReason(StrEnum):
    BUILD_FAILED = "NixBuildFailed"
    BUILD_SUCCEEDED = "NixBuildSucceeded"
    DEPRECATED_DRIVER_NAME = "NixDeprecatedDriverName"
    FARM_SUB_PATH = "NixFarmSubPath"
    INTERNAL_ERROR = "NixInternalError"
    KERNEL_INCOMPATIBLE = "NixKernelIncompatible"
    KERNEL_LIMITED = "NixKernelLimited"
    MISSING_NIX_MOUNT = "NixMissingNixMount"
    MISSING_SUB_PATH = "NixMissingSubPath"
    OLD_STORE_RETIRED = "NixOldStoreRetired"
    VOLUME_CLEANUP_FAILED = "NixVolumeCleanupFailed"
    VOLUME_MOUNT = "NixVolumeMount"
    """A volume mounted. Not a failure: that is `VOLUME_MOUNT_FAILED`."""

    # One for each `CSIError` subclass.
    STORE_PATH_CLOSURE = "NixStorePathClosure"
    VERIFY_STORE_PATHS = "NixVerifyStorePaths"
    HARDLINK_CLOSURE = "NixHardlinkClosure"
    INIT_DATABASE = "NixInitDatabase"
    GC_ROOT_INSTALLATION = "NixGCRootInstallation"
    RESULT_LINK_INSTALLATION = "NixResultLinkInstallation"
    VOLUME_MOUNT_FAILED = "NixVolumeMountFailed"
    VOLUME_UNMOUNT = "NixVolumeUnmount"
    CLEANUP_STALE_ENTRIES = "NixCleanupStaleEntries"
    FAILED_VOLUME_CLEANUP = "NixFailedVolumeCleanup"
    BUILD = "NixBuild"
