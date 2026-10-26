# SPDX-License-Identifier: Apache-2.0
import json
import shutil
import time
from functools import wraps
from pathlib import Path
from typing import Any

import anyio
import anyio.abc
import structlog
from grpclib_nri import NriPlugin as NriPluginBase
from grpclib_nri import NriServer
from kr8s.asyncio.objects import Pod

from nri import nri_pb2

from .. import presentation
from ..cache import schedule_copy_to_cache
from ..constants import (
    HOST_PROC_PATH,
    HOST_ROOT,
    HOST_STORE,
    NODE_ROOT,
    NRI_BIND_FARM,
    NRI_COMPOSEFS,
    NRI_CONTAINERS,
    NRI_PLUGIN_IDX,
    NRI_PLUGIN_NAME,
    NRI_RUNTIME_SOCKET,
    NRI_SWEEP_INTERVAL_SECONDS,
    NRI_VM_BUSYBOX,
    NRI_VM_RUNTIME_HANDLERS,
)
from ..cri import get_cri_socket, list_container_ids
from ..csi import composefs
from ..events import report_event
from ..metrics import (
    NRI_BUILD_DURATION,
    NRI_BUILDS,
    NRI_BUILDS_IN_FLIGHT,
    NRI_CONTAINERS_SEEN,
    NRI_STATE_CHANGES,
)
from ..nix import fetch_packages, get_build_args, get_current_system
from ..presentation import Presentation
from ..supervision import detach
from ..volume import prepare_volume
from .annotations import (
    extract_container_store_paths,
    parse_nix_exclude,
    parse_nix_rw,
    parse_store_mounts,
)
from .builds import BuildRegistry
from .cleanup import garbage_collect_stale_volumes, schedule_garbage_collection
from .mount import kernel_supports_ro, kernel_supports_rw, mount_in_container, rootfs_of
from .vm import (
    ALIVE,
    FAILED,
    READY,
    host_source,
    is_vm_runtime,
    mark,
    prepare_waiter,
    refusal,
    waiter_args,
)
from .zmq import ZeroMQServer

# ============================================================================
# NRI POD CREATION LIFECYCLE
# ============================================================================
#
# The NRI plugin injects Nix stores into containers via a multi-phase process
# coordinated through OCI hooks and ZeroMQ sockets. The overall flow:
#
# PHASE 1: CreateContainer Hook (NRI synchronous)
# ────────────────────────────────────────────────
# 1. Parse pod annotations (nixkube/pod, nixkube/{container}, with system variants)
#    - Extract store paths from annotations, container args, and env vars
#    - Parse store mount paths (e.g., nixkube/pod-path: /container/path → /nix/store/...)
#    - Parse RW flag (e.g., nixkube/pod-rw: "true" for overlayfs instead of RO bind)
#
# 2. Skip if /nix already mounted
#    - If CSI driver already mounted /nix, NRI skips (CSI takes precedence)
#    - Prevents collision between two Nix injection methods
#
# 3. Inject createRuntime OCI hook
#    - Creates a hook that will fire during container init, before exec
#    - Executes: chroot ${NODE_ROOT} wait (nri-wait binary from nixkube dependencies)
#    - Hook will report container PID+bundle via ZeroMQ REQ/REP sockets
#    - Uses OCI hooks (not NRI request handlers) because they lack forced low timeouts
#    - Uses pkgsStatic.chroot to create a comfortable isolated execution environment
#
# 4. Spawn background build task (starts immediately, runs async)
#    - Task ID = container_id, added to pending_builds set
#    - Begins realizing all store paths via nix build immediately
#    - Registers the closure in a chroot-store database under
#      /nix/var/nixkube/containers/{container_id}/nix. The closure itself is
#      bound in the mount worker (bind farm) or hardlinked here
#      (NRI_BIND_FARM=false).
#    - This build starts NOW and runs concurrently with OCI hook execution
#
#
# PHASE 2: OCI Hook Execution (during container init)
# ─────────────────────────────────────────────────────
# The createRuntime hook fires while the container init process is alive with its
# mount namespace established (but before pivot_root, so host paths are accessible).
#
# 1. nri-wait binary starts (executed via chrooted coreutils-static.chroot)
#    - Connects to ZeroMQ REQ socket at /nix/var/nixkube/wait-req.sock
#    - Sends RegisterContainerRequest with PID (getpid) and bundle path (from OCI state)
#
# 2. ZeroMQ REQ/REP coordination
#    - Build task waits for pid_event[container_id] via zmq_server.wait_for_pid()
#    - Once nri-wait sends PID+bundle, zmq_server stores them and signals the event
#    - nri-wait's response is "container ready" (REP socket reply)
#    - nri-wait then waits for a follow-up signal (via PUB socket) before exiting
#
# 3. Heartbeat via PUB socket
#    - Build task spawns a _pump_build_progress task that publishes every 10 seconds
#    - nri-wait subscribes and resets its 30-second timeout on each message
#    - Ensures slow builds don't timeout during the mount operation
#
#
# PHASE 3: Build Task Waits & Mounts
# ────────────────────────────────────
# After spawning the OCI hook, build task:
#
# 1. Realizes store paths
#    - Calls nix build with extra args (builders, cache endpoints)
#    - Outputs at /nix/var/nixkube/containers/{container_id}/nix
#
# 2. Prepare the volume — prepare_volume(bind_farm=NRI_BIND_FARM)
#    - Farm: registers the closure in the chroot store's database and copies
#      nothing. The paths are bound in the mount worker. The build task asks
#      the mount budget here and refuses before the first mount if the
#      closure will not fit.
#    - Hardlinks: links every closure path into the volume, and creates
#      upper/ and work/ for the RW overlay.
#
# 3. Wait for PID+bundle
#    - Blocks on zmq_server.wait_for_pid(container_id, timeout=30)
#    - When nri-wait reports, returns (pid, bundle_path)
#
# 4. Spawn mount subprocess
#    - Calls mount_in_container(pid, rootfs, nix_tree_path, store_mounts, nix_rw)
#    - Uses multiprocessing.spawn to avoid contaminating asyncio event loop with setns(2)
#    - Passes precomputed mount FDs created in the original namespace
#
#
# PHASE 4: Mount Subprocess (FD-based namespace operations)
# ───────────────────────────────────────────────────────────
# The subprocess is spawned with file descriptors for isolated namespace ops:
#
# 0. Build the farm (farm only)
#    - unshare(CLONE_NEWNS) first: fs.mount-max is 100,000 per namespace, so
#      2443 mounts per closure cannot go in the daemon's. The namespace dies
#      with the worker, so nothing is left to unmount or collect.
#
# 1. Create detached mount FDs (while the sources are still visible)
#    - open_tree with AT_RECURSIVE clones the tree, farm submounts included
#      - Survives setns(2) so the source path is never visible inside container
#      - Attached with move_mount(2), then made read-only if RO
#    - Hardlink tree + RW: fsopen/fsconfig build a detached overlayfs fd
#      - lowerdir=hardlink tree, upperdir=volume/upper, workdir=volume/work
#
# 2. Switch to container namespace
#    - setns(2) to enter the container's mount namespace via PID
#    - fchdir+chroot to enter the rootfs (using fd opened before setns)
#    - Now inside the container's mount namespace and rootfs
#
# 3. Attach /nix mount
#    - move_mount(2) attaches the detached /nix fd at /nix
#    - If RO: mount_setattr(MOUNT_ATTR_RDONLY, AT_RECURSIVE) for a farm, since
#      MS_REMOUNT|MS_RDONLY covers the top mount alone and would leave every
#      bound store path writable into /nix/store on the node
#
# 4. Attach store mounts
#    - For each store_mount (container_path → /nix/store/...) requested in annotations:
#      - Use traditional mount(2) MS_BIND from /nix/store/... to container_path
#      - Done after setns so /nix/store/... paths are reachable as bind-mount sources
#
# 5. Return to nri-wait
#    - After all mounts attached, subprocess exits
#    - nri-wait (waiting on PUB socket) receives "container ready" signal
#    - nri-wait exits the hook, allowing container init to continue to exec
#
#
# PHASE 5: Container Execution & Cleanup
# ─────────────────────────────────────────
# 1. Container runs with /nix and store mounts injected
#
# 2. On container removal (StateChange REMOVE_CONTAINER event)
#    - schedule_garbage_collection(cri_socket): acknowledges the event at once
#      and sweeps in the background. Queries CRI for active containers, removes
#      any stale volumes (including the one being removed) that are orphaned by
#      crashed containers or abrupt shutdowns. In the background because
#      containerd deadlines every NRI request, and a sweep is slower than an
#      acknowledgement may be.
#
# 3. On every (re)connection (Synchronize), the same sweep: a container that
#    went away while the runtime was down sends no REMOVE_CONTAINER.
#
# VM RUNTIMES (NRI_VM_RUNTIME_HANDLERS, e.g. Kata)
# ─────────────────────────────────────────────────
# No hook and no namespace mount: the hook's pid is the hypervisor. Phase 1
# instead binds the container's tree at /nix, which the runtime shares into
# the VM over virtio-fs, and puts a static busybox waiter in front of the
# container's args. Phase 3 fills the tree with hardlinks and writes a ready
# or failed marker. Read-only only. See `vm.py`.
#
# ============================================================================


def nri_error_handler(
    func: Any,
) -> Any:  # decorator wraps arbitrary async handler methods
    """Decorator for NRI handlers: logs exceptions and reports events."""

    @wraps(func)
    async def wrapper(self, stream):
        handler_logger = structlog.get_logger(f"nixkube.nri.{func.__name__.lower()}")
        try:
            return await func(self, stream)
        except Exception as e:
            handler_logger.exception("handler_failed")
            await report_event(
                None,
                reason="InternalError",
                note=f"{func.__name__} failed: {type(e).__name__}",
                logs=str(e),
                event_type="Warning",
            )
            raise

    return wrapper


class NriPlugin(NriPluginBase):
    """NRI plugin with ZeroMQ build coordination."""

    def __init__(
        self,
        zmq_server: ZeroMQServer,
        cri_socket: Path,
        tasks: anyio.abc.TaskGroup,
    ):
        """Initialize the NRI plugin with ZeroMQ and CRI socket coordination.

        Args:
            zmq_server: ZeroMQ server for build task coordination and PID/bundle reporting
            cri_socket: Path to the CRI socket for container introspection
            tasks: the group the builds and sweeps run in, owned by `nri_serve`
        """
        logger = structlog.get_logger("nixkube.nri.init")
        # Call NirPluginBase init that registers plugin and which events we listen to
        super().__init__(
            [
                nri_pb2.Event.CREATE_CONTAINER,  # Inject stores into containers
                nri_pb2.Event.REMOVE_CONTAINER,  # Cleanup hardlink farm volumes
            ]
        )
        self.zmq_server = zmq_server
        self.cri_socket = cri_socket
        self.tasks = tasks
        # Find nri-wait binary on PATH (available as nix-csi dependency)
        self.nri_wait_bin = shutil.which("wait")
        logger.debug("nri_wait_resolved", binary=self.nri_wait_bin)

    @nri_error_handler
    async def CreateContainer(self, stream) -> None:
        structlog.contextvars.clear_contextvars()
        logger = structlog.get_logger("nixkube.nri.createcontainer")
        req: nri_pb2.CreateContainerRequest | None = await stream.recv_message()
        assert req is not None
        structlog.contextvars.bind_contextvars(
            pod={"namespace": req.pod.namespace, "name": req.pod.name},
            container={"name": req.container.name, "id": req.container.id},
        )
        logger.debug("create_container", runtime_handler=req.pod.runtime_handler)

        # Check if /nix is already mounted (e.g., by nix-csi) to avoid collision
        if any(m.destination == "/nix" for m in req.container.mounts):
            logger.debug("nix_already_mounted")
            NRI_CONTAINERS_SEEN.labels(result="already_mounted").inc()
            resp = nri_pb2.CreateContainerResponse(adjust=nri_pb2.ContainerAdjustment())
            await stream.send_message(resp)
            return

        system = get_current_system()

        # Before anything reads the container's environment. A container that
        # asked to be left alone must not have its store paths extracted, let
        # alone realised on the node. See `parse_nix_exclude`.
        if parse_nix_exclude(req.pod.annotations, req.container.name, system):
            logger.info("injection_excluded")
            NRI_CONTAINERS_SEEN.labels(result="excluded").inc()
            resp = nri_pb2.CreateContainerResponse(adjust=nri_pb2.ContainerAdjustment())
            await stream.send_message(resp)
            return

        store_paths = extract_container_store_paths(req, system)
        if store_paths:
            logger.info(
                "extracted_store_paths",
                count=len(store_paths),
                store_paths=sorted(str(p) for p in store_paths),
            )

        # Parse store mount annotations (nixkube/[container-name/]path), filtered by system
        store_mounts = parse_store_mounts(
            req.pod.annotations, req.container.name, system
        )
        if store_mounts:
            logger.info(
                "parsed_store_mounts",
                count=len(store_mounts),
                store_mounts={str(k): str(v) for k, v in store_mounts.items()},
            )

        # Parse RW flag (nixkube/pod-rw or nixkube/{container-name}-rw), filtered by system
        nix_rw = parse_nix_rw(req.pod.annotations, req.container.name, system)
        if nix_rw:
            logger.info("nix_rw_requested")
            # Verify kernel supports new mount API for overlayfs (6.5+)
            if not kernel_supports_rw():
                logger.error("nix_rw_kernel_unsupported")
                raise RuntimeError(
                    "RW overlay mounts not supported on this kernel (requires Linux 6.5+)"
                )

        adjust = nri_pb2.ContainerAdjustment()

        # Enable NRI build if we have storepaths to inject
        if store_paths:
            container_id = req.container.id

            logger.info("store_injection_enabled", count=len(store_paths))

            try:
                # Create Pod object for event reporting
                pod = Pod(
                    {
                        "metadata": {
                            "name": req.pod.name,
                            "namespace": req.pod.namespace,
                            "uid": req.pod.uid,
                        },
                    },
                    namespace=req.pod.namespace,
                )

                vm = is_vm_runtime(req.pod.runtime_handler, NRI_VM_RUNTIME_HANDLERS)
                if vm:
                    if not NRI_VM_BUSYBOX:
                        raise RuntimeError(
                            "NRI_VM_BUSYBOX is not set, so a VM container cannot wait for its /nix"
                        )
                    tree = NRI_CONTAINERS / container_id / "nix"
                    await prepare_waiter(tree, NRI_VM_BUSYBOX)
                    source = host_source(tree, NODE_ROOT)
                    adjust.mounts.append(
                        nri_pb2.Mount(
                            destination="/nix",
                            type="bind",
                            source=str(source),
                            options=["rbind", "ro"],
                        )
                    )
                    adjust.args.extend(waiter_args(list(req.container.args)))
                    logger.info(
                        "vm_waiter_injected",
                        runtime_handler=req.pod.runtime_handler,
                        source=str(source),
                    )
                else:
                    # Inject OCI hook to wait for build completion and report PID+bundle
                    assert self.nri_wait_bin is not None, (
                        "nri-wait binary not found on PATH, wait hook won't be able to execute"
                    )
                    coreutils_container = shutil.which("coreutils")
                    assert coreutils_container is not None, (
                        "coreutils not found on PATH"
                    )
                    coreutils_host = NODE_ROOT / Path(coreutils_container).relative_to(
                        "/"
                    )
                    hook = nri_pb2.Hook(
                        path=str(coreutils_host),
                        args=[
                            "chroot",  # somehow this works in OCI hooks but not --coreutils-prog=chroot....
                            str(NODE_ROOT),
                            self.nri_wait_bin,
                        ],
                        env=[
                            "NRI_QUERY_SOCKET=/nix/var/nixkube/wait-req.sock",
                            "NRI_PUB_SOCKET=/nix/var/nixkube/wait-pub.sock",
                            "NRI_TIMEOUT=30",
                        ],
                    )
                    adjust.hooks.create_runtime.append(hook)
                    logger.info(
                        "hook_injected",
                        nri_wait_bin=self.nri_wait_bin,
                        coreutils_host=coreutils_host,
                    )

                # Spawn build task to build store paths and namespace-mount them into the container
                if container_id not in self.zmq_server.pending_builds:
                    self.zmq_server.pending_builds.add(container_id)
                    logger.info("build_task_spawning", count=len(store_paths))
                    try:
                        detach(
                            self.tasks,
                            self._spawn_build_task,
                            container_id,
                            req.container.name,
                            pod,
                            store_paths,
                            store_mounts,
                            nix_rw,
                            vm,
                            name="nri_build",
                        )
                    except Exception:
                        # `start_soon` refuses once the group is closing, with
                        # `RuntimeError: This task group is not active`. The id
                        # would then name a build that never starts: the sweep
                        # skips that volume, and every later removal pays the
                        # full cancel timeout waiting for nothing.
                        self.zmq_server.pending_builds.discard(container_id)
                        raise
                else:
                    logger.warning("build_already_pending")

                NRI_CONTAINERS_SEEN.labels(result="injected").inc()

            except Exception:
                NRI_CONTAINERS_SEEN.labels(result="error").inc()
                logger.exception("volume_setup_failed")
        else:
            NRI_CONTAINERS_SEEN.labels(result="no_paths").inc()

        resp = nri_pb2.CreateContainerResponse(adjust=adjust)
        await stream.send_message(resp)

    @nri_error_handler
    async def StateChange(self, stream) -> None:
        structlog.contextvars.clear_contextvars()
        logger = structlog.get_logger("nixkube.nri.statechange")
        event: nri_pb2.StateChangeEvent | None = await stream.recv_message()
        assert event is not None

        event_name = nri_pb2.Event.Name(event.event)
        # The label is the enum's name, so the series count is the enum's size.
        NRI_STATE_CHANGES.labels(event=event_name).inc()
        structlog.contextvars.bind_contextvars(
            nri_event=event_name,
            pod={"namespace": event.pod.namespace, "name": event.pod.name},
        )

        # Only include container info if this is a container event (container exists and has a name)
        if event.container and event.container.name:
            container_info: dict = {
                "name": event.container.name,
                "state": nri_pb2.ContainerState.Name(event.container.state),
            }
            if event.container.exit_code:
                container_info["exit_code"] = event.container.exit_code
            structlog.contextvars.bind_contextvars(container=container_info)

        logger.info("state_change")

        # Cleanup stale hardlink farm volumes when container is removed
        if event.event == nri_pb2.Event.REMOVE_CONTAINER:
            schedule_garbage_collection(
                self.tasks,
                self.cri_socket,
                event.container.id or None,
                self.zmq_server.pending_builds,
            )

        await stream.send_message(nri_pb2.Empty())

    @nri_error_handler
    async def Synchronize(self, stream) -> None:
        """Sweep after every (re)connection to the runtime.

        A container that went away while the runtime was down sends no
        REMOVE_CONTAINER, so its volume stayed until the next removal of any
        container. Measured after a containerd restart: 59 s, the period of
        kubelet's container GC.
        """
        logger = structlog.get_logger("nixkube.nri.synchronize")
        req: nri_pb2.SynchronizeRequest | None = await stream.recv_message()
        logger.info("synchronize", containers=len(req.containers) if req else 0)
        await stream.send_message(nri_pb2.SynchronizeResponse())
        # A long list comes in parts; `more` marks every part but the last.
        if req is not None and req.more:
            return
        schedule_garbage_collection(
            self.tasks, self.cri_socket, building=self.zmq_server.pending_builds
        )

    async def _pump_build_progress(self, container_id: str, vm: bool) -> None:
        """Periodically publish build progress heartbeats to reset nri-wait timeout.

        Runs until the group around it is cancelled, which is how the build
        stops it. The cancellation is not caught: a pump that returned
        normally would leave that group waiting on nothing.
        """
        while True:
            await anyio.sleep(10)
            await self.zmq_server.publish_build_progress(container_id)
            if vm:
                await mark(NRI_CONTAINERS / container_id / "nix", ALIVE)

    async def _spawn_build_task(
        self,
        container_id: str,
        container_name: str,
        pod: Pod,
        store_paths: set[Path],
        store_mounts: dict[Path, Path] | None = None,
        nix_rw: bool = False,
        vm: bool = False,
    ) -> None:
        """Run the build inside a scope the volume sweep can cancel.

        The scope is attached to the registry rather than created there,
        because the container id is registered by the NRI handler and this
        task starts later. A removal that arrives in that window is honoured
        when the scope arrives (issue #64).
        """
        scope = anyio.CancelScope()
        self.zmq_server.pending_builds.attach(container_id, scope)
        with scope:
            await self._run_build_task(
                container_id, container_name, pod, store_paths, store_mounts, nix_rw, vm
            )

    async def _run_build_task(
        self,
        container_id: str,
        container_name: str,
        pod: Pod,
        store_paths: set[Path],
        store_mounts: dict[Path, Path] | None = None,
        nix_rw: bool = False,
        vm: bool = False,
    ) -> None:
        """Realize store paths, link into the volume, then namespace-mount store mounts.

        Periodically pumps progress updates to reset nri-wait timeout.

        Reports its own failure and does not re-raise. It runs in the group
        that holds the NRI server, and one container's build failing is not a
        reason to take the plugin down.
        """
        log = structlog.get_logger("nixkube.nri.buildtask").bind(
            container_id=container_id
        )
        log.info("build_task_started", count=len(store_paths), vm=vm)
        tree = NRI_CONTAINERS / container_id / "nix"
        started = time.monotonic()
        NRI_BUILDS_IN_FLIGHT.inc()
        try:
            # If no store paths to build, just mark as done
            if not store_paths:
                log.info("no_store_paths")
                self.zmq_server.build_status[container_id] = {"status": "done"}
                await self.zmq_server.publish_build_complete(container_id)
                self.zmq_server.pending_builds.discard(container_id)
                NRI_BUILDS.labels(result="ok").inc()
                return

            # The pump keeps nri-wait's timeout reset while the build runs. It
            # gets a group of its own, cancelled on the way out, so it cannot
            # outlive the build it reports on however that build ends.
            async with anyio.create_task_group() as pump:
                pump.start_soon(self._pump_build_progress, container_id, vm)
                log.debug("progress_pump_started")
                try:
                    if vm:
                        await self._build_for_vm(
                            log, container_id, store_paths, store_mounts, nix_rw
                        )
                    else:
                        await self._build_and_mount(
                            log, container_id, store_paths, store_mounts, nix_rw
                        )
                finally:
                    pump.cancel_scope.cancel()

            if vm:
                await mark(tree, READY)
            log.info("build_task_completed")
            self.zmq_server.build_status[container_id] = {"status": "done"}
            log.debug("build_status_updated")
            await self.zmq_server.publish_build_complete(container_id)
            self.zmq_server.pending_builds.discard(container_id)
            log.info("removed_from_pending")

            # Copy all packages to cache in background
            schedule_copy_to_cache(self.tasks, store_paths)

            # Report successful build
            await report_event(
                pod,
                reason="BuildSucceeded",
                note=f"Successfully built {len(store_paths)} store path(s)",
                event_type="Normal",
            )
            NRI_BUILDS.labels(result="ok").inc()
        except Exception as e:
            NRI_BUILDS.labels(result="error").inc()
            log.exception("build_task_failed")
            self.zmq_server.pending_builds.discard(container_id)
            reason = failure_reason(e)
            # So the hook fails now, with the reason, instead of after its
            # timeout with none.
            self.zmq_server.build_status[container_id] = {
                "status": "failed",
                "reason": reason,
            }
            await self.zmq_server.publish_build_failed(container_id, reason)
            if vm:
                await mark(tree, FAILED, f"nixkube: {reason}\n")

            # Report failed build
            await report_event(
                pod,
                reason="BuildFailed",
                note=f"Failed to build store paths for container {container_name}",
                logs=reason,
                event_type="Warning",
            )
        finally:
            # Here and not only in the two paths above. `pending_builds` is
            # what the volume sweep reads to decide a build is still filling
            # a directory (issue #64), and a cancelled task leaves neither
            # of those paths: `CancelledError` is a BaseException, so
            # `except Exception` does not see it. An id left behind would
            # pin that container's farm against both collection rules for
            # the life of the process.
            self.zmq_server.pending_builds.discard(container_id)
            NRI_BUILDS_IN_FLIGHT.dec()
            NRI_BUILD_DURATION.observe(time.monotonic() - started)

    async def _build_and_mount(
        self,
        log: Any,
        container_id: str,
        store_paths: set[Path],
        store_mounts: dict[Path, Path] | None,
        nix_rw: bool,
    ) -> None:
        """Realize the closure, link it in, then mount it into the namespace.

        Split from `_spawn_build_task` so the progress pump's task group wraps
        exactly this and nothing else.
        """
        # Get extra build args for builders and cache
        extra_args = await get_build_args()

        # Realize storepaths
        volume_path = NRI_CONTAINERS / container_id
        log.debug("fetch_packages_starting", count=len(store_paths))
        await fetch_packages(store_paths, volume_path, extra_args)
        log.debug("fetch_packages_done")

        shown = presentation.nri(
            vm=False,
            enabled=NRI_COMPOSEFS,
            kernel=NRI_COMPOSEFS and await composefs.available(),
            bind_farm=NRI_BIND_FARM,
            host_store=HOST_STORE,
        )
        log.debug("nix_presentation", presentation=str(shown))
        farm_paths = None
        image = None
        if shown is Presentation.COMPOSEFS:
            # The image references its closure, so this out-link roots both.
            built = await composefs.build_image(
                store_paths, None, volume_path / "composefs", extra_args
            )
            image = built / composefs.IMAGE
        else:
            # A farm binds the closure in the mount worker and leaves nothing
            # on disk; the hardlink path fills the volume here instead. Issue
            # #65. A host-store node has only the farm: a hardlink out of the
            # host's store fails with EXDEV. Issue #25.
            prepared = await prepare_volume(
                volume_path,
                store_paths,
                None,
                bind_farm=shown is Presentation.FARM,
                allow_hardlinks=not HOST_STORE,
            )
            # `prepared.bind_farm`, not `shown`: the volume may have fallen
            # back to a hardlink tree, and then there is nothing to bind.
            farm_paths = prepared.paths if prepared.bind_farm else None

        nix_tree_path = volume_path / "nix"

        # Wait for nri-wait to report PID+bundle (arrives when the createRuntime hook fires).
        # We need the PID to enter the container's mount namespace and mount /nix + store mounts.
        log.debug("pid_bundle_waiting")
        container_info = await self.zmq_server.wait_for_pid(container_id)
        pid_info = container_info[0] if container_info else None
        log.debug("pid_bundle_received", pid=pid_info)
        if container_info is None:
            raise RuntimeError(
                f"No PID/bundle received for container={container_id!r}, cannot mount /nix"
            )
        pid, bundle = container_info

        rootfs = await reachable_rootfs(pid, bundle)
        if rootfs is None:
            raise RuntimeError(
                f"the container's rootfs is not reachable through pid {pid}, so"
                " NRI cannot mount /nix into it. A sandboxed runtime such as"
                " gVisor or Kata keeps it out of the host's reach; mount the"
                " store with a CSI volume instead."
            )

        mounts = []
        if store_mounts:
            for container_path, store_path in store_mounts.items():
                resolved = store_path.resolve()
                if not resolved.exists():
                    raise ValueError(
                        f"Invalid store path in annotation: {store_path!r} → {container_path!r} "
                        f"(resolved: {resolved!r} does not exist)"
                    )
                mounts.append((resolved, container_path))

        log.info(
            "namespace_mounting",
            pid=pid,
            bundle=bundle,
            mounts=len(mounts),
            farm=len(farm_paths) if farm_paths else 0,
            composefs=image is not None,
        )
        await mount_in_container(
            pid, rootfs, nix_tree_path, mounts, nix_rw, farm_paths, image
        )

    async def _build_for_vm(
        self,
        log: Any,
        container_id: str,
        store_paths: set[Path],
        store_mounts: dict[Path, Path] | None,
        nix_rw: bool,
    ) -> None:
        """Fill the tree a VM already shares at /nix. See `vm.py`.

        A hardlink tree and never a farm: the VM sees the tree through
        virtio-fs, which serves files and would have to serve each farm bind
        as a submount of its own.
        """
        reason = refusal(nix_rw, store_mounts)
        if reason:
            raise RuntimeError(reason)
        shown = presentation.nri(
            vm=True,
            enabled=NRI_COMPOSEFS,
            kernel=False,
            bind_farm=NRI_BIND_FARM,
            host_store=HOST_STORE,
        )
        if shown is Presentation.REFUSED:
            raise RuntimeError(
                "a VM runtime reads /nix as a hardlink tree over virtio-fs, and this"
                " node shares its host's store, where a hardlink out of it fails"
                " with EXDEV. Issue #25."
            )
        extra_args = await get_build_args()
        volume_path = NRI_CONTAINERS / container_id
        await fetch_packages(store_paths, volume_path, extra_args)
        await prepare_volume(volume_path, store_paths, None, bind_farm=False)
        log.info("vm_tree_filled")


async def reachable_rootfs(pid: int, bundle: str) -> str | None:
    """The rootfs mount.py will open, or None where it cannot reach one.

    Asked first so a failure can say why. runc and crun report the
    container's own pid, whose root is the host's before pivot_root. gVisor
    reports its sandbox, whose root is its own (measured, issue #74); Kata
    puts the container in a VM.
    """
    root = f"{HOST_PROC_PATH}/{pid}/root"
    try:
        config = json.loads(await anyio.Path(f"{root}{bundle}/config.json").read_text())
        rootfs = rootfs_of(bundle, config)
        if await anyio.Path(f"{root}{rootfs}").is_dir():
            return rootfs
    except (OSError, ValueError, KeyError):
        pass
    return None


def failure_reason(error: BaseException) -> str:
    """The messages of the errors a group wraps, not the group's own.

    The build runs inside the progress pump's task group, so its failure
    arrives as "unhandled errors in a TaskGroup (1 sub-exception)".
    """
    if isinstance(error, BaseExceptionGroup):
        return "; ".join(failure_reason(inner) for inner in error.exceptions)
    return str(error)


async def sweep_loop(cri_socket: Path, building: BuildRegistry) -> None:
    """The backstop for a removal that sends no REMOVE_CONTAINER."""
    while True:
        await anyio.sleep(NRI_SWEEP_INTERVAL_SECONDS)
        await garbage_collect_stale_volumes(cri_socket, building=building)


async def nri_serve() -> None:
    """Run the NRI plugin server with automatic reconnection and kernel checks."""
    logger = structlog.get_logger("nixkube.nri.serve")

    # Test kernel capabilities at startup
    if not kernel_supports_ro():
        # RO support is required; fail hard
        logger.critical("kernel_ro_unsupported")
        await report_event(
            None,
            reason="KernelIncompatible",
            note="NRI plugin failed: kernel does not support open_tree/move_mount (requires Linux 5.2+)",
            event_type="Warning",
        )
        raise RuntimeError("Kernel does not support required mount API syscalls")

    if not kernel_supports_rw():
        # RW support is optional; warn but continue
        logger.warning(
            "kernel_rw_unsupported",
            note="RW /nix mounts will be unavailable (requires Linux 6.5+ for fsopen/fsconfig/fsmount)",
        )
        await report_event(
            None,
            reason="KernelLimited",
            note="NRI plugin running in RO-only mode: kernel does not support new mount API for overlayfs (fsopen/fsconfig/fsmount, requires Linux 6.5+)",
            event_type="Warning",
        )

    # Initialize ZeroMQ server
    zmq_server = ZeroMQServer()
    await zmq_server.initialize()

    # Discover CRI socket and verify connectivity. Without CRI access, garbage
    # collection of stale volumes won't work and the node will fill up.
    cri_socket = await get_cri_socket()
    containers = await list_container_ids(HOST_ROOT / cri_socket.relative_to("/"))
    logger.info("cri_connected", container_count=len(containers))

    # One group holds the REP handler and every build and sweep a handler
    # starts. The `cancel_scope.cancel()` below is what lets it close at all:
    # the REP handler waits on `recv()` and never returns, so a group that
    # waited for its children would hang on a clean exit.
    async with anyio.create_task_group() as tasks:
        plugin = NriPlugin(zmq_server, cri_socket, tasks)
        server = NriServer(
            plugin,
            socket_path=Path(NRI_RUNTIME_SOCKET),
            plugin_name=NRI_PLUGIN_NAME,
            plugin_idx=NRI_PLUGIN_IDX,
        )

        tasks.start_soon(zmq_server.start_request_handler, name="zmq-rep")
        tasks.start_soon(
            sweep_loop, cri_socket, zmq_server.pending_builds, name="nri-sweep"
        )

        try:
            # Start server (handles reconnection with exponential backoff internally)
            await server.start()
        finally:
            await server.close()
            zmq_server.shutdown()
            tasks.cancel_scope.cancel()
