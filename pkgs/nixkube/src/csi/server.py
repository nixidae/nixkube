# SPDX-License-Identifier: Apache-2.0

import socket
import time
from collections import defaultdict
from collections.abc import Awaitable, Callable, Coroutine
from functools import wraps
from pathlib import Path
from typing import Any, ClassVar

import anyio
import anyio.abc
import structlog
from grpclib import GRPCError
from grpclib.const import Status
from grpclib.server import Server, Stream
from kr8s import NotFoundError
from kr8s.asyncio.objects import Pod

from csi import csi_grpc, csi_pb2

from .. import presentation
from ..cache import schedule_copy_to_cache
from ..constants import (
    CSI_COMPOSEFS,
    CSI_GCROOTS,
    CSI_SOCKET_PATH,
    CSI_VOLUMES,
    HOST_STORE,
    KUBE_NODE_NAME,
    KUBE_POD_NAME,
    NAMESPACE,
)
from ..errors import CSIError, MountError
from ..events import report_event
from ..kube_docs import pod_doc
from ..metrics import (
    VOLUME_MOUNTS,
    VOLUME_PREPARE_DURATION,
    VOLUME_UNMOUNTS,
    VOLUMES_PUBLISHED,
)
from ..nix import (
    build_pod_packages,
    build_primary_package,
    get_build_args,
    get_current_system,
)
from ..nri.annotations import parse_pod_exclude
from ..presentation import Presentation
from ..volume import (
    cleanup_failed_volume,
    is_mount,
    mount_volume,
    prepare_volume,
    unmount,
)
from . import composefs
from .cleanup import TARGET_FILE, cleanup_stale_entries, collect_active_volume_handles
from .identity import IdentityServicer

_NIXKUBE_DRIVERS = {"nixkube", "nix.csi.store"}


async def check_csi_volume_mounts(pod: Pod) -> None:
    """Emit warning events for nixkube CSI volumes with common mount misconfigurations.

    Checks two conditions for each nixkube CSI volume in the pod spec:
    - MissingSubPath: a volumeMount exists without subPath='nix', which causes
      the volume root (not the nix/ subtree) to be mounted, making /nix/store
      paths inaccessible at the expected location.
    - MissingNixMount: no container mounts the volume at /nix, so /nix/store
      paths will never be reachable regardless of subPath.
    """
    spec = pod_doc(pod).get("spec", {})
    all_containers = spec.get("containers", []) + spec.get("initContainers", [])

    for volume in spec.get("volumes", []):
        if volume.get("csi", {}).get("driver") not in _NIXKUBE_DRIVERS:
            continue
        vol_name = volume["name"]

        vol_mounts = [
            (container, vm)
            for container in all_containers
            for vm in container.get("volumeMounts", [])
            if vm["name"] == vol_name
        ]

        for container, vm in vol_mounts:
            if not vm.get("subPath"):
                await report_event(
                    pod,
                    reason="MissingSubPath",
                    note=(
                        f"Volume '{vol_name}' in container '{container['name']}' "
                        f"mounted at '{vm['mountPath']}' is missing subPath='nix'. "
                        f"Nix store paths will not be accessible at the mount point."
                    ),
                    event_type="Warning",
                )

        if not any(vm["mountPath"] == "/nix" for _, vm in vol_mounts):
            await report_event(
                pod,
                reason="MissingNixMount",
                note=(
                    f"Volume '{vol_name}' has no mount at '/nix' in any container. "
                    f"Nix store paths will not be accessible."
                ),
                event_type="Warning",
            )


def csi_error_handler[S, Req, Resp, T](
    func: Callable[[S, Stream[Req, Resp]], Awaitable[T]],
) -> Callable[[S, Stream[Req, Resp]], Coroutine[Any, Any, T]]:
    """Decorator for CSI handlers: logs exceptions, emits events, and re-raises as gRPC errors.

    Wraps CSI handler methods to catch exceptions, emit Kubernetes events for debugging,
    and convert them to appropriate gRPC error codes. CSIError exceptions have pod info
    and descriptive messages; unexpected exceptions emit generic warning events.
    """

    @wraps(func)
    async def wrapper(self: S, stream: Stream[Req, Resp]) -> T:
        handler_logger = structlog.get_logger(f"nixkube.csi.{func.__name__.lower()}")
        try:
            return await func(self, stream)
        except Exception as e:
            handler_logger.exception("handler_failed")

            # Emit events for all exceptions
            if isinstance(e, CSIError):
                # CSIError should already have pod set from the operation
                if e.pod:
                    await report_event(
                        e.pod,
                        reason=e.reason,
                        note=e.message,
                        logs=e.logs,
                        event_type="Warning",
                    )
            else:
                # Unexpected exception: emit event (pod will be fetched if needed)
                await report_event(
                    None,
                    reason="InternalError",
                    note=f"{func.__name__} failed: {type(e).__name__}",
                    logs=str(e),
                    event_type="Warning",
                )

            # Re-raise as GRPCError
            if isinstance(e, GRPCError):
                raise
            raise GRPCError(Status.INTERNAL, f"{type(e).__name__}: {e}") from e

    return wrapper


class NodeServicer(csi_grpc.NodeBase):
    # `NodePublishVolume` takes the volume lock and then, inside it, a lock
    # keyed on the build and one keyed on the pod uid. Those three key spaces
    # do not overlap. If one ever did, `anyio.Lock` raises where a semaphore of
    # one hangs, and a handler that raises is one kubelet retries.
    volume_locks: ClassVar[defaultdict[str, anyio.Lock]] = defaultdict(anyio.Lock)

    def __init__(self, system: str, plugin_name: str, tasks: anyio.abc.TaskGroup):
        self.system = system
        self.plugin_name = plugin_name
        # Required, and not defaulted to None: a servicer with nowhere to put
        # a background copy would drop every copy silently.
        self.tasks = tasks

    @csi_error_handler
    async def NodePublishVolume(
        self,
        stream: Stream[csi_pb2.NodePublishVolumeRequest, csi_pb2.NodePublishVolumeResponse],
    ) -> None:
        logger = structlog.get_logger("nixkube.csi.nodepublishvolume")
        start_time = time.perf_counter()
        request = await stream.recv_message()
        if request is None:
            raise GRPCError(Status.INVALID_ARGUMENT, "NodePublishVolumeRequest is None")

        if not request.volume_context.get("csi.storage.k8s.io/ephemeral"):
            raise GRPCError(
                Status.INVALID_ARGUMENT,
                "This CSI driver only supports ephemeral volumes",
            )

        log = logger.bind(volume_id=request.volume_id)

        async with self.volume_locks[request.volume_id]:
            gc_root = CSI_GCROOTS / request.volume_id
            volume_root = CSI_VOLUMES / request.volume_id
            extra_args = await get_build_args()

            # Fetch pod for event reporting and package extraction
            pod_name = request.volume_context["csi.storage.k8s.io/pod.name"]
            pod_namespace = request.volume_context["csi.storage.k8s.io/pod.namespace"]
            pod_uid = request.volume_context["csi.storage.k8s.io/pod.uid"]

            log = log.bind(
                pod=f"{pod_namespace}/{pod_name}",
                target_path=request.target_path,
            )
            log.info("publishing_volume")

            # Already published. Say so and touch nothing.
            #
            # The CSI spec requires this: "If the volume corresponding to the
            # volume_id has already been published at the specified
            # target_path [...] the Plugin MUST reply 0 OK". kubelet asks
            # again after every restart, because its reconstructed volume
            # state is uncertain until the driver confirms it.
            #
            # Without the guard the second call rebuilds `volume_root` --
            # which is the source of the *live* bind mount a running pod is
            # using. Measured: a kubelet restart left the resident pod's
            # mount reading
            #
            #     /nixkube/nix/var/nix-csi/volumes/csi-ff7e...//deleted
            #
            # and the re-mount refused with ENOENT, because the kernel marks
            # an unlinked directory DCACHE_CANT_MOUNT and will not mount onto
            # a mount whose root carries it. The pod kept running with an
            # empty store, which for a container whose command is a store
            # path is the worst of both worlds.
            if is_mount(Path(request.target_path)):
                log.info("already_published")
                await stream.send_message(csi_pb2.NodePublishVolumeResponse())
                return

            try:
                pod = await Pod.get(pod_name, namespace=pod_namespace)
            except NotFoundError:
                # Pod was deleted before we could mount — stale request from kubelet.
                # Return success so kubelet stops retrying; it will call
                # NodeUnpublishVolume next, which will also return success.
                log.warning("pod_not_found_skip_mount")
                await stream.send_message(csi_pb2.NodePublishVolumeResponse())
                return

            # Validate that fetched pod matches the UID from volume context
            if pod.metadata.uid != pod_uid:
                raise RuntimeError(f"Pod UID mismatch: {pod.metadata.uid} != {pod_uid}")

            # If the pod is terminating, mounting makes no sense — it's going away.
            # Return success to break kubelet's retry loop; it will call
            # NodeUnpublishVolume to clean up.
            if pod_doc(pod).get("metadata", {}).get("deletionTimestamp"):
                log.info("pod_terminating_skip_mount")
                await stream.send_message(csi_pb2.NodePublishVolumeResponse())
                return

            try:
                await check_csi_volume_mounts(pod)
            except Exception:
                log.warning("csi_mount_check_failed", exc_info=True)

            # Emit deprecation warning if using compatibility driver
            if self.plugin_name == "nix.csi.store":
                try:
                    await report_event(
                        pod,
                        reason="DeprecatedDriverName",
                        note="Using deprecated nix.csi.store driver. Please migrate to nixkube driver.",
                        event_type="Warning",
                    )
                except Exception:
                    log.warning("deprecation_warning_failed", exc_info=True)

            # Build primary package from volume attributes
            try:
                store_path = request.volume_context.get(self.system)
                flake_ref = request.volume_context.get("flakeRef")
                nix_expr = request.volume_context.get("nixExpr")

                # Use first non-None value as lock key (same priority as build_primary_package)
                lock_key = store_path or flake_ref or nix_expr or "null"
                async with self.volume_locks[lock_key]:
                    primary_package = await build_primary_package(
                        store_path,
                        flake_ref,
                        nix_expr,
                        gc_root,
                        extra_args,
                    )
            except CSIError as e:
                e.pod = pod
                raise

            # Build packages from pod spec, unless the pod opted out: then
            # the volume holds its own package alone (`parse_pod_exclude`).
            annotations = pod_doc(pod).get("metadata", {}).get("annotations") or {}
            if parse_pod_exclude(annotations, self.system):
                log.info("pod_packages_excluded")
                package_paths: set[Path] = set()
            else:
                try:
                    async with self.volume_locks[pod_uid]:
                        package_paths = await build_pod_packages(
                            pod,
                            gc_root,
                            extra_args,
                        )
                except CSIError as e:
                    e.pod = pod
                    raise

            if primary_package is not None:
                package_paths.add(primary_package)
                log.debug("primary_package", path=str(primary_package))

            if not package_paths:
                log.error("no_packages_to_mount")
                raise GRPCError(
                    Status.INVALID_ARGUMENT,
                    "No packages to mount",
                )

            try:
                shown = presentation.csi(
                    enabled=CSI_COMPOSEFS,
                    kernel=CSI_COMPOSEFS and await composefs.available(),
                    user_namespace=pod_doc(pod).get("spec", {}).get("hostUsers") is False,
                    host_store=HOST_STORE,
                )
                log.debug("volume_presentation", presentation=str(shown))
                if shown is Presentation.COMPOSEFS:
                    image = await composefs.build_image(
                        package_paths,
                        primary_package,
                        gc_root / "composefs",
                        extra_args,
                    )
                    volume_root.mkdir(parents=True, exist_ok=True)
                    # The sweep cannot ask whether the volume root is a mount
                    # source: a composefs mount's source is the image. It asks
                    # whether this target is still mounted instead.
                    (volume_root / TARGET_FILE).write_text(request.target_path)
                    await composefs.mount(image, Path(request.target_path), request.readonly, volume_root)
                elif shown is Presentation.REFUSED:
                    raise MountError(
                        "this node shares its host's store, where a volume can only be"
                        " a composefs mount, and this pod cannot have one: either it"
                        " sets hostUsers: false, and the kernel cannot idmap an overlay,"
                        " or this kernel failed the composefs probe (see"
                        " nixkube_composefs_available). Issues #25 and #68.",
                        logs="",
                    )
                else:
                    await prepare_volume(
                        volume_root,
                        package_paths,
                        primary_package,
                    )
                    await mount_volume(
                        volume_root,
                        Path(request.target_path),
                        request.readonly,
                    )
                # Report successful mount with closure size and elapsed time
                elapsed = time.perf_counter() - start_time
                VOLUME_MOUNTS.labels(result="ok").inc()
                VOLUMES_PUBLISHED.inc()
                # The whole publish, and not `mount_volume` alone: realising
                # the closure is where the time goes, and a pod waits for all
                # of it. `start_time` is the top of this handler.
                VOLUME_PREPARE_DURATION.observe(elapsed)
                await report_event(
                    pod,
                    reason="VolumeMount",
                    note=f"Mounted Nix volume with {len(package_paths)} store paths in {elapsed:.2f}s",
                    event_type="Normal",
                )
            except CSIError as e:
                VOLUME_MOUNTS.labels(result="error").inc()
                cleanup_failed_volume(gc_root, volume_root)
                # Attach pod to exception for decorator to emit pod-specific event
                e.pod = pod
                raise
            except Exception:
                VOLUME_MOUNTS.labels(result="error").inc()
                cleanup_failed_volume(gc_root, volume_root)
                raise

            await stream.send_message(csi_pb2.NodePublishVolumeResponse())

            # Copy all packages to cache in background
            schedule_copy_to_cache(self.tasks, package_paths)

    @csi_error_handler
    async def NodeUnpublishVolume(
        self,
        stream: Stream[csi_pb2.NodeUnpublishVolumeRequest, csi_pb2.NodeUnpublishVolumeResponse],
    ) -> None:
        logger = structlog.get_logger("nixkube.csi.nodeunpublishvolume")
        request = await stream.recv_message()
        if request is None:
            raise GRPCError(Status.INVALID_ARGUMENT, "NodeUnpublishVolumeRequest is None")

        log = logger.bind(
            volume_id=request.volume_id,
            target_path=request.target_path,
        )
        log.info("unpublishing_volume")

        async with self.volume_locks[request.volume_id]:
            target_path = Path(request.target_path)

            # CRITICAL: This handler MUST NOT return success unless the mount is
            # fully gone. If we return OK while the mount persists, kubelet considers
            # the volume cleaned up and won't retry — leaving pods stuck in
            # Terminating indefinitely, requiring manual node-level intervention.
            #
            # CSI driver is responsible for unmounting only, not for removing the
            # kubelet-managed mount directory (that's kubelet's job).
            if is_mount(target_path):
                try:
                    await unmount(target_path)
                except Exception:
                    # Counted before it propagates. A node that cannot unmount
                    # leaves pods in Terminating, and this is the series that
                    # says so before somebody opens a shell on the node.
                    VOLUME_UNMOUNTS.labels(result="error").inc()
                    raise
                VOLUME_UNMOUNTS.labels(result="ok").inc()
                VOLUMES_PUBLISHED.dec()
                log.debug("unmounted")
            else:
                # Not counted either way. Kubelet retries an unpublish, so a
                # path already gone is a repeat of work that was done, and
                # counting it would inflate the total over the mounts.
                log.debug("not_mounted")

            # Clean up stale gcroots and volume directories based on active volumes.
            # This catches orphaned resources from volumes that failed to unpublish cleanly.
            # Note: we do this cleanup even if above steps fail, to ensure CSI driver
            # resources in /nix/var/nix-csi are always cleaned up. Kubelet will retry
            # NodeUnpublishVolume if needed, but we should not block cleanup of our own resources.
            try:
                current_vol_data = target_path.parent / "vol_data.json"
                active_handles = collect_active_volume_handles(exclude_vol_data_path=current_vol_data)
                cleanup_stale_entries(active_handles)
            except Exception:
                log.exception("stale_cleanup_failed")
                # Report error event on CSI driver controller since we don't have pod info here
                try:
                    controller_pod = Pod(
                        {
                            "metadata": {
                                "name": KUBE_POD_NAME,
                                "namespace": NAMESPACE,
                            },
                        },
                        namespace=NAMESPACE,
                    )
                    await report_event(
                        controller_pod,
                        reason="VolumeCleanupFailed",
                        note=f"Failed to cleanup stale entries for volume {request.volume_id}",
                        event_type="Warning",
                    )
                except Exception:
                    log.warning("cleanup_event_failed", exc_info=True)
                # Don't raise - CSI driver should still return success if unmount succeeded

            await stream.send_message(csi_pb2.NodeUnpublishVolumeResponse())

    @csi_error_handler
    async def NodeGetCapabilities(
        self,
        stream: Stream[csi_pb2.NodeGetCapabilitiesRequest, csi_pb2.NodeGetCapabilitiesResponse],
    ) -> None:
        request = await stream.recv_message()
        if request is None:
            raise GRPCError(Status.INVALID_ARGUMENT, "NodeGetCapabilitiesRequest is None")
        await stream.send_message(csi_pb2.NodeGetCapabilitiesResponse(capabilities=[]))

    @csi_error_handler
    async def NodeGetInfo(self, stream: Stream[csi_pb2.NodeGetInfoRequest, csi_pb2.NodeGetInfoResponse]) -> None:
        request = await stream.recv_message()
        if request is None:
            raise GRPCError(Status.INVALID_ARGUMENT, "NodeGetInfoRequest is None")
        await stream.send_message(csi_pb2.NodeGetInfoResponse(node_id=KUBE_NODE_NAME))

    async def NodeGetVolumeStats(
        self, stream: Stream[csi_pb2.NodeGetVolumeStatsRequest, csi_pb2.NodeGetVolumeStatsResponse]
    ) -> None:
        raise GRPCError(Status.UNIMPLEMENTED, "NodeGetVolumeStats not implemented")

    async def NodeExpandVolume(
        self, stream: Stream[csi_pb2.NodeExpandVolumeRequest, csi_pb2.NodeExpandVolumeResponse]
    ) -> None:
        raise GRPCError(Status.UNIMPLEMENTED, "NodeExpandVolume not implemented")

    async def NodeStageVolume(
        self, stream: Stream[csi_pb2.NodeStageVolumeRequest, csi_pb2.NodeStageVolumeResponse]
    ) -> None:
        raise GRPCError(Status.UNIMPLEMENTED, "NodeStageVolume not implemented")

    async def NodeUnstageVolume(
        self, stream: Stream[csi_pb2.NodeUnstageVolumeRequest, csi_pb2.NodeUnstageVolumeResponse]
    ) -> None:
        raise GRPCError(Status.UNIMPLEMENTED, "NodeUnstageVolume not implemented")


async def csi_serve(plugin_name: str | None = None, socket_path: Path | None = None) -> None:
    logger = structlog.get_logger("nixkube.csi.serve")
    if socket_path is None:
        socket_path = Path(CSI_SOCKET_PATH)
    if plugin_name is None:
        plugin_name = "nixkube"
    socket_path.parent.mkdir(parents=True, exist_ok=True)
    socket_path.unlink(missing_ok=True)

    identity_servicer = IdentityServicer(plugin_name)

    # The group the handlers put their background copies in. Cancelled on the
    # way out, so a copy dies with the server that started it instead of
    # outliving a restart.
    async with anyio.create_task_group() as tasks:
        # Pod will be fetched and cached on first use via get_nixkube_pod()
        node_servicer = NodeServicer(get_current_system(), plugin_name, tasks)

        server = Server(
            [
                identity_servicer,
                node_servicer,
            ]
        )

        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            sock.bind(str(socket_path))
            sock.listen(128)

            await server.start(sock=sock)
            logger.info("csi_listening", socket=str(socket_path))
            await server.wait_closed()
        except Exception:
            sock.close()
            socket_path.unlink(missing_ok=True)
            raise
        finally:
            tasks.cancel_scope.cancel()
