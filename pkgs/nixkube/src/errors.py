# SPDX-License-Identifier: Apache-2.0
"""CSI driver exception hierarchy with Kubernetes event mapping."""

from grpclib import GRPCError
from grpclib.const import Status
from kr8s.asyncio.objects import Pod


class SubprocessError(Exception):
    """Exception raised when a subprocess command fails.

    Contains structured information about the failure including output
    and return code, which can be extracted when re-throwing as specific
    operation errors.
    """

    def __init__(
        self,
        returncode: int,
        stdout: str,
        stderr: str,
        combined: str,
        command: list[object],
    ) -> None:
        """Initialize subprocess error.

        Args:
            returncode: Command exit code
            stdout: Standard output from command
            stderr: Standard error from command
            combined: Combined stdout and stderr
            command: Command that was executed (as list)
        """
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr
        self.combined = combined
        self.command = command
        super().__init__(f"Subprocess failed with return code {returncode}")

    def __str__(self) -> str:
        """Return detailed error message including command and return code."""
        cmd_str = " ".join(str(arg) for arg in self.command)
        # Truncate command if too long to avoid massive log lines
        if len(cmd_str) > 200:
            cmd_str = cmd_str[:197] + "..."
        return f"Subprocess failed (rc={self.returncode}): {cmd_str}"

    def __repr__(self) -> str:
        """Return repr including command for debugging."""
        cmd_str = repr(self.command)
        # Truncate command if too long
        if len(cmd_str) > 150:
            cmd_str = cmd_str[:147] + "...]"
        return f"{type(self).__name__}(rc={self.returncode}, cmd={cmd_str})"


class CSIError(GRPCError):
    """Base CSI error with Kubernetes event mapping capability.

    Inherits from GRPCError so unhandled exceptions are properly
    reported through gRPC. Each subclass has a 'reason' field that
    maps to Kubernetes event reason codes.
    """

    reason: str = "InternalError"
    status: Status = Status.INTERNAL

    def __init__(
        self,
        message: str,
        logs: str | None = None,
        status: Status = Status.INTERNAL,
    ) -> None:
        """Initialize CSI error.

        Args:
            message: Human-readable error message for events
            logs: Combined stdout/stderr output for inclusion in events
            status: gRPC status code (defaults to Status.INTERNAL)
        """
        self.message = message
        self.logs = logs
        self.pod: Pod | None = None  # Can be set by handler before re-raising
        super().__init__(status, message)


# Store path closure errors
class StorePathClosureError(CSIError):
    """Error retrieving store path closure with 'nix path-info --recursive'."""

    reason = "StorePathClosure"


class VerifyStorePathsError(CSIError):
    """Error verifying store path integrity with 'nix store verify --recursive'."""

    reason = "VerifyStorePaths"


class HardlinkClosureError(CSIError):
    """Error hardlinking store paths to volume root."""

    reason = "HardlinkClosure"


class InitDatabaseError(CSIError):
    """Error initializing Nix database in volume."""

    reason = "InitDatabase"


class InstallGCRootError(CSIError):
    """Error installing garbage collection root."""

    reason = "GCRootInstallation"


class InstallResultLinkError(CSIError):
    """Error installing /nix/var/result symlink in volume."""

    reason = "ResultLinkInstallation"


class MountError(CSIError):
    """Error mounting volume to target path."""

    reason = "VolumeMount"


class UnmountError(CSIError):
    """Error unmounting volume from target path."""

    reason = "VolumeUnmount"


class CleanupStaleEntriesError(CSIError):
    """Error cleaning up stale volume entries."""

    reason = "CleanupStaleEntries"


class FailedVolumeCleanupError(CSIError):
    """Error cleaning up resources after a failed volume operation."""

    reason = "FailedVolumeCleanup"


# Build operation errors
class BuildError(CSIError):
    """Error building a package (store path, flake, or expression)."""

    reason = "Build"


class SystemDetectionError(CSIError):
    """Error detecting system type."""

    reason = "SystemDetection"


class CommandTimeoutError(SubprocessError):
    """Error when a subprocess command times out.

    Inherits from SubprocessError to preserve command details and output
    for logging and event emission. The CSI error handler will catch this
    and emit appropriate Kubernetes events.
    """
