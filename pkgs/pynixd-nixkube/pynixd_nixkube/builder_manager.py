# SPDX-License-Identifier: Apache-2.0

import contextlib
import hashlib
import socket
import time
import uuid
from collections.abc import AsyncIterator, Callable, Coroutine, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, cast

import anyio
import anyio.abc
import kr8s.asyncio as k8s
import structlog
from kr8s.asyncio.objects import Job, PodTemplate, new_class
from pynixd.build_queue import QueuedBuild
from pynixd.config import SSHSubprocessStoreSpec
from pynixd.serde import StoreId
from pynixd.store import SSHSubprocessStore

if TYPE_CHECKING:
    from pynixd.instance import Server

log = structlog.get_logger(__name__)

BUILDER_LABEL = "app.kubernetes.io/component"
BUILDER_LABEL_VALUE = "builder"
BUILDER_PODTEMPLATE_NAME = "nixkube-builder"
SYSTEM_LABEL = "nixkube/system"
PROBED_LABEL = "nixkube/probed"
PROBE_LABEL = "nixkube/probe"
FEATURES_ANNOTATION = "nixkube/features"

# The CSI driver a builder's `nix-store` volume names. A node whose CSINode
# does not list it cannot mount that volume, so a builder cannot run there.
CSI_DRIVER_NAME = "nixkube"

KUBE_ARCH_TO_NIX_SYSTEM = {
    "amd64": "x86_64-linux",
    "arm64": "aarch64-linux",
}
NIX_SYSTEM_TO_KUBE_ARCH = {v: k for k, v in KUBE_ARCH_TO_NIX_SYSTEM.items()}

_DEFAULT_FEATURES = {"nixos-test", "big-parallel", "benchmark"}

_COOLDOWN_SECONDS = 60.0
_RECONNECT_DELAY = 10.0

# First delay after one failed builder. Each further consecutive failure
# doubles it, up to `backoff_cap`.
_BACKOFF_BASE_SECONDS = 30.0

# How often the manager reconciles without a Job event.
#
# `_ensure_min_builders` used to run only from the Job watch. A system in
# backoff produces no Job events while it waits, so nothing woke the manager
# when the delay expired and the system stayed at zero builders until an
# unrelated Job changed. The same tick drives the startup watchdog, because a
# Pod that hangs in Pending produces no Job events either.
_RECONCILE_INTERVAL = 30.0

# Events reported against a builder Job, using the same API the node driver
# uses for Pods.
ModernEvent = new_class(kind="Event", version="events.k8s.io/v1", namespaced=True)
EVENT_REASON_STARTUP_TIMEOUT = "NixBuilderStartupTimeout"

# The host keys a builder may present.
#
# `secret.nix` generates one ed25519 pair for the namespace and writes the
# public half into the `ssh-dynauth` ConfigMap as `* <key>`. `pynixd.nix`
# mounts that ConfigMap here.
BUILDER_KNOWN_HOSTS = "/etc/ssh-dynauth/ssh_known_hosts"


@dataclass
class PodState:
    """What a builder Job's Pod says about itself."""

    ip: str | None = None
    ready: bool = False
    phase: str | None = None
    node: str | None = None


def _pod_is_ready(pod_status: dict) -> bool:
    """True when the Pod carries a Ready condition set to True."""
    for condition in pod_status.get("conditions", []) or []:
        if condition.get("type") == "Ready":
            return condition.get("status") == "True"
    return False


def _job_age_seconds(job_raw: dict, now: datetime) -> float | None:
    """Seconds since the Job started, or None when the API gives no timestamp.

    `status.startTime` is when the Job controller began, and
    `metadata.creationTimestamp` is the fallback for the moment before the
    controller has written a status. Read from the object, not from a local
    dict, so the age survives a pynixd restart.
    """
    raw = job_raw.get("status", {}).get("startTime") or job_raw.get("metadata", {}).get("creationTimestamp")
    if not raw:
        return None
    try:
        started = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if started.tzinfo is None:
        started = started.replace(tzinfo=UTC)
    return (now - started).total_seconds()


@dataclass
class RecycleView:
    """What the recycle decision needs to know about one registered builder."""

    store_id: str
    system: str
    aged: bool
    ready: bool
    draining: bool
    busy: bool


def plan_recycle(builders: list[RecycleView], min_builders: int) -> tuple[list[str], list[str]]:
    """Which aged builders to drain, and which drained ones to retire.

    An aged builder drains only once a fresh builder of its system is Ready,
    so the system never drops below its minimum while the replacement starts.
    With a minimum of zero nothing replaces it, so it drains at once and the
    queue watch starts a builder when a build needs one.

    A draining builder is retired only when it is not busy. Deleting the Job
    of a busy one makes `remove_store` cancel its builds after 300 seconds.
    """
    fresh = {b.system for b in builders if b.ready and not b.aged and not b.draining}
    drain = [b.store_id for b in builders if b.aged and not b.draining and (min_builders == 0 or b.system in fresh)]
    retire = [b.store_id for b in builders if b.draining and not b.busy]
    return drain, retire


def pending_systems(builds: Iterable[QueuedBuild], available: set[str]) -> set[str]:
    """The systems that pending builds need and a builder could serve.

    The system is the derivation's own `platform`. `QueuedBuild.platform`
    no longer exists, and reading it raised on every pass, so no builder
    scaled up and a queued build was never dispatched (#78).
    """
    return {
        platform
        for build in builds
        if build.is_pending and (platform := build.request.derivation.platform) in available
    }


def deep_merge(base: dict, overrides: dict) -> dict:
    """Deep-merge overrides into base (Nix lib.recursiveUpdate style).

    Non-dict values in overrides replace base values entirely.
    Lists are replaced, not merged.
    """
    result = base.copy()
    for key, value in overrides.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = value
    return result


class BuilderManager:
    def __init__(
        self,
        server: "Server",
        namespace: str,
        max_builders: int = 3,
        min_builders: int = 1,
        idle_timeout: int = 300,
        systems: list[str] | None = None,
        cooldown_seconds: float = _COOLDOWN_SECONDS,
        backoff_cap: float = 600.0,
        startup_timeout: float = 600.0,
        max_age: float = 21600.0,
    ) -> None:
        self.server = server
        self.namespace = namespace
        self.max_builders = max_builders
        self.min_builders = min_builders
        self.idle_timeout = idle_timeout
        # A default, and deliberately not the local store's probe result.
        #
        # Deriving this from the local probe reads as the obvious tidy-up and
        # is a trap. pynixd mounts its own store read-only over the CSI
        # driver, so its local probe fails -- `creating directory
        # "/nix/var/nix/userpool": Read-only file system` -- and records
        # `systems: []` as a successful probe of an empty store. Node probing
        # reads this attribute, so deriving it would turn that into "no node
        # is ever probed", with no error, no probe and nothing in the log.
        #
        # The two defects are independent by accident rather than by design.
        # Keep it that way. See solid-kubernetes git-bug 66ddf78.
        self.systems = systems or ["x86_64-linux"]
        self.cooldown_seconds = cooldown_seconds
        self.backoff_cap = backoff_cap
        self.startup_timeout = startup_timeout
        # Seconds after which a builder is replaced, or 0 for never. A Job that
        # never completes raises kube-prometheus' KubeJobNotCompleted at 12h.
        self.max_age = max_age
        self._last_create_time: dict[str, float] = {}
        self._registered: dict[str, str] = {}
        self._idle_since: dict[str, float] = {}
        self._job_names: dict[str, str] = {}
        self._available_systems: set[str] = set()
        self._pending_probes: set[str] = set()
        self._tasks: anyio.abc.TaskGroup | None = None
        # Consecutive failed builders per system, and when that system may
        # create another. A builder that reaches Ready clears both.
        self._failures: dict[str, int] = {}
        self._backoff_until: dict[str, float] = {}
        # Job names already counted as a failure. A Job stays failed for its
        # `ttlSecondsAfterFinished`, and every reconcile sees it again.
        self._counted_failures: set[str] = set()
        # Job names whose Pod has been Ready at least once. The startup
        # watchdog only ends a builder that has never answered. In memory, so
        # it is empty after a restart -- the Pod phase is the guard that does
        # not forget.
        self._ever_ready: set[str] = set()
        # Job names past `max_age`, and the system of each registered builder.
        # Both come from the Job object, so a restart rebuilds them.
        self._aged: set[str] = set()
        self._store_systems: dict[str, str] = {}
        # One reconcile at a time.
        #
        # `_reconcile_job` awaits three times, and the periodic tick reconciles
        # the same Jobs as the watch. Two calls on one Job would both find it
        # unregistered, both fetch the Pod and both add the same store. A Job
        # MODIFIED event fires exactly when a new builder's Pod gets an IP,
        # which is when the tick is most likely to be looking at it.
        self._reconcile_lock = anyio.Lock()

    @contextlib.asynccontextmanager
    async def running(self) -> AsyncIterator["BuilderManager"]:
        """Run the five loops for as long as the block lasts.

        A block, and not `start()` / `stop()`: a task group's lifetime *is* a
        block, and holding one open across two calls needs a task whose only
        job is to keep the block from closing.

        Each loop catches its own `Exception` and retries, so none of them
        reaches the group. One that did would cancel the other four and end
        the process, which is the right answer for a bug nobody anticipated.
        """
        await self._reap_orphaned_builder_pods()
        self._available_systems = set(self.systems)
        await self._sync_dynamic_features()

        async with anyio.create_task_group() as tasks:
            self._tasks = tasks
            for loop in (
                self._watch_jobs,
                self._watch_queue,
                self._reap_idle,
                self._watch_nodes,
                self._periodic_reconcile,
            ):
                tasks.start_soon(loop, name=loop.__name__)
            log.info(
                "builder_manager_started",
                namespace=self.namespace,
                max_builders=self.max_builders,
                min_builders=self.min_builders,
                systems=list(self._available_systems),
            )
            try:
                yield self
            finally:
                tasks.cancel_scope.cancel()
                self._tasks = None
                log.info("builder_manager_stopped")

    def _detach(
        self,
        func: Callable[..., Coroutine[Any, Any, None]],
        *args: Any,
        name: str,
    ) -> None:
        """Start one-off work in the manager's group, and keep its failure in.

        A task group cancels every sibling when a child raises, and this group
        holds the five loops. A probe failing on one node is not a reason to
        stop watching the cluster.

        It is also the first time such a failure is visible: these ran as bare
        `create_task` with no callback, so the traceback went wherever the
        garbage collector put it.
        """
        if self._tasks is None:
            log.warning("builder_task_not_started", task=name)
            return

        async def guarded() -> None:
            try:
                await func(*args)
            except anyio.get_cancelled_exc_class():
                raise
            except Exception:
                log.exception("builder_task_failed", task=name)

        self._tasks.start_soon(guarded, name=name)

    async def _periodic_reconcile(self) -> None:
        while True:
            await anyio.sleep(_RECONCILE_INTERVAL)
            try:
                async with self._reconcile_lock:
                    await self._resync_jobs()
                    await self._ensure_min_builders()
                    await self._recycle_aged()
            except Exception:
                log.warning("builder_periodic_reconcile_error", exc_info=True)

    # ---- Failure backoff ----

    def _backoff_delay(self, failures: int) -> float:
        """Delay after `failures` consecutive failed builders of one system."""
        if failures <= 0:
            return 0.0
        return min(_BACKOFF_BASE_SECONDS * (2 ** (failures - 1)), self.backoff_cap)

    def _backoff_remaining(self, system: str) -> float:
        until = self._backoff_until.get(system)
        if until is None:
            return 0.0
        return max(0.0, until - time.monotonic())

    def _record_failure(self, system: str, job_name: str, reason: str, node: str | None = None) -> None:
        """Count one failed builder and hold that system back.

        `_ensure_min_builders` counts a Job as active only while it is neither
        succeeded nor failed. So a builder that fails at once drops the count
        at once and the next reconcile creates another, at the reconcile rate,
        for as long as the cause lasts. Nothing recorded that the last one
        failed. This is that record.

        The count is per system, and the node is logged but not counted. One
        broken node makes every builder for its system back off, so the log
        line is what separates "the cluster is slow" from "this node is
        broken". Per-node state is not wanted -- the scheduler places
        builders, and the bound is right either way.
        """
        if job_name in self._counted_failures:
            return
        self._counted_failures.add(job_name)
        failures = self._failures.get(system, 0) + 1
        self._failures[system] = failures
        delay = self._backoff_delay(failures)
        self._backoff_until[system] = time.monotonic() + delay
        log.warning(
            "builder_failure_recorded",
            system=system,
            job=job_name,
            node=node,
            reason=reason,
            consecutive_failures=failures,
            backoff_seconds=delay,
        )

    def _record_ready(self, system: str, job_name: str) -> None:
        """Clear the backoff, because a builder of this system works.

        Ready, not registered. `_reconcile_job` registers a builder as soon as
        its Pod has an IP, which a Pod has before its container runs. A builder
        that crashes at startup would register, clear the count and fail, over
        and over, and the delay would never grow past the first step. The Pod's
        Ready condition follows the readinessProbe on the ssh port, so it means
        the builder answers.
        """
        if not self._failures.get(system) and system not in self._backoff_until:
            return
        log.info(
            "builder_backoff_reset",
            system=system,
            job=job_name,
            cleared_failures=self._failures.get(system, 0),
        )
        self._failures.pop(system, None)
        self._backoff_until.pop(system, None)

    def _forget_job(self, job_name: str) -> None:
        self._counted_failures.discard(job_name)
        self._ever_ready.discard(job_name)
        self._aged.discard(job_name)

    # ---- Startup watchdog ----

    def _startup_expired(self, job_raw: dict, job_name: str, phase: str | None, now: datetime) -> bool:
        """True when a builder that never started has had long enough.

        Not `activeDeadlineSeconds`. That field measures the whole life of a
        Job, from `status.startTime`, and a builder Job runs for as long as it
        serves builds -- one measured at three and a half hours on a live
        cluster. Any deadline short enough to bound a Pod that hangs before it
        starts would become the usual way a healthy builder dies, mid-build.

        The phase is the guard, not `_ever_ready`. `_ever_ready` is in memory,
        so a restart empties it, and a three-hour builder whose ssh probe was
        failing at that moment would look like a builder that never started.
        `Pending` is the case the issue describes: the kubelet fails the CSI
        mount before it creates a sandbox, so the Pod has no IP and no
        container. A `Running` Pod that is not Ready is left alone, and a
        container that crashes at startup is covered by `backoffLimit: 0`,
        which fails the Job.
        """
        if phase is not None and phase != "Pending":
            return False
        if job_name in self._ever_ready:
            return False
        if job_name in self._counted_failures:
            return False
        age = _job_age_seconds(job_raw, now)
        return age is not None and age > self.startup_timeout

    async def _expire_slow_starter(self, job: Any, system: str, pod: PodState) -> bool:
        """End a builder whose Pod never started. True when it did."""
        job_name = job.metadata.name
        now = datetime.now(UTC)
        if not self._startup_expired(job.raw, job_name, pod.phase, now):
            return False

        age = _job_age_seconds(job.raw, now)
        # Name the node. The backoff counts per system, and a startup failure
        # is almost always per node -- one broken nix-node makes every system
        # on the cluster look slow. The node is what turns "builds are slow"
        # into "this node" in one kubectl get events.
        where = f"on node {pod.node}" if pod.node else "with no node assigned"
        note = (
            f"Builder for {system} never started {where}. Its Pod was still "
            f"{pod.phase or 'absent'} {round(age or 0.0)}s after the Job began, "
            f"over the {round(self.startup_timeout)}s startup timeout. Deleting "
            "it. A Pod that stays Pending usually cannot mount its nixkube CSI "
            "volume; check that Pod's events and nix-node on that node. An "
            "unassigned Pod is unschedulable instead."
        )
        log.warning(
            "builder_startup_timeout",
            job=job_name,
            system=system,
            node=pod.node,
            phase=pod.phase,
            age_seconds=round(age or 0.0, 1),
            startup_timeout=self.startup_timeout,
        )
        await self._report_job_event(job, EVENT_REASON_STARTUP_TIMEOUT, note, event_type="Warning")
        self._record_failure(system, job_name, reason="StartupTimeout", node=pod.node)
        store_id = f"builder-{job_name}"
        if store_id in self._registered:
            await self._unregister_builder(store_id)
        await self._delete_job(job)
        return True

    async def _report_job_event(self, job: Any, reason: str, note: str, event_type: str = "Normal") -> None:
        """Report a Kubernetes event against a builder Job.

        An operator looks at events first. The symptom this watchdog acts on
        is a Pod stuck in Pending with a FailedMount, and nothing there says
        the controller has given up on it. Best effort -- a failure to report
        must not stop the deletion.
        """
        try:
            uid = getattr(job.metadata, "uid", "") or ""
            digest = hashlib.md5(f"{uid}{reason}".encode()).hexdigest()[:8]
            event = await ModernEvent(
                {
                    "metadata": {
                        "name": f"{job.metadata.name}.{digest}",
                        "namespace": self.namespace,
                    },
                    "type": event_type,
                    "reason": reason,
                    "action": "NixBuilderReap",
                    "regarding": {
                        "apiVersion": "batch/v1",
                        "kind": "Job",
                        "name": job.metadata.name,
                        "namespace": self.namespace,
                        "uid": uid,
                    },
                    "reportingController": "nixkube-builder-manager",
                    "reportingInstance": socket.gethostname(),
                    "note": note[:1000],
                    "eventTime": datetime.now(UTC).isoformat(),
                },
                namespace=self.namespace,
            )
            await event.create()
        except Exception:
            log.warning("builder_event_report_failed", job=job.metadata.name, exc_info=True)

    # ---- Node probing ----

    async def _watch_nodes(self) -> None:
        """Probe nodes where the nixkube CSI driver is registered.

        Watches CSINode, not Node, and both halves of that matter.

        As a GATE, it is the fact rather than a proxy for it. A builder gets
        /nix through a `nix-store` volume served by this CSI driver, and
        kubelet writes `CSINode.spec.drivers` on plugin registration. So this
        asks the same question the mount asks. A taint only correlates: on
        nixlab2 cp-0 carries `node-role.kubernetes.io/control-plane:NoSchedule`
        and has no driver, but a node can equally be untainted, running a
        healthy nix-node, and not registered *yet*.

        As a TRIGGER, it is quiet. Measured on nixlab2 at cluster head
        resourceVersion ~149763: cp-0's CSINode was last written at 14245 --
        static for about 135,000 revisions -- while every Node sat between
        148388 and 149763, because kubelet rewrites node status constantly.
        Watching Node meant every heartbeat re-fired this, which is the fuel
        the retry loop ran on. A CSINode wakes on driver registration only:
        a node joins, nix-node starts, nix-node stops.

        The RESULT still goes on the Node. `nixkube/probed` and the features
        annotation are what pod nodeSelectors and affinities match against,
        and a label on a CSINode is not selectable. So this watches one
        object and writes another on purpose.

        STARTUP DEPENDS ON THE WATCH REPLAYING. A watch with no
        `resourceVersion` gets synthetic ADDED events for everything that
        already exists, then the stream. That is what probes the cluster
        after a restart, and it matters more here than it did for Nodes:
        every CSINode is static, so a watch that only carried changes would
        fire never and probe nothing. Measured on nixlab2 -- five ADDED
        events in twenty seconds with nothing changing.

        THERE IS NO OTHER PATH BACK TO PROBING, which matters when one fails.
        `_probe_node` discards the node from `_pending_probes` in its
        `finally`, so the node is eligible again -- but nothing fires.
        `_periodic_reconcile` does not probe, and the CSINode has not
        changed, because CSINodes do not change. The only thing that can
        probe it again is another replay, when this watch re-establishes.

        Whether that amounts to failure recovery is NOT measured, and is a
        property of the apiserver rather than of this code: it depends on
        whether kube-apiserver closes an idle watch and how long it waits.
        nixlab2 does not set `--min-request-timeout`, so nothing at the
        cluster level has chosen that number either. Treat recovery as
        unknown until someone measures it. Giving probe failure a retry that
        this file owns, rather than one inherited from a watch timeout, is
        solid-kubernetes git-bug d0b5385.

        So do not pass `since=` to make this resumable. It would look like an
        improvement. Startup would break visibly, on the next restart. Any
        recovery that the replay does provide would go with it, and that
        failure is silent: a node whose probe failed once stays unlabelled
        and unusable as a build target, with nothing tying it to the change.

        A KNOWN COMPROMISE, and the reason matters more than the fact.
        Nothing structurally binds a builder to the host's shared store: the
        builder gets /nix through this CSI driver *today*, which is what
        makes CSINode the right question today. Give a builder its own store
        -- an emptyDir, a PVC, a layer in its own image -- and this gate
        becomes wrong while continuing to look correct, because a node could
        then run a builder without the driver. If that changes, this is the
        line to revisit.
        """
        while True:
            try:
                api = await k8s.api()
                async for event in api.watch("csinodes"):
                    event_type, resource = cast(Any, event)
                    if event_type not in {"ADDED", "MODIFIED"}:
                        continue
                    node_name = getattr(resource.metadata, "name", "")
                    if not node_name or node_name in self._pending_probes:
                        continue
                    drivers = {d.get("name") for d in (resource.raw.get("spec", {}).get("drivers") or [])}
                    if CSI_DRIVER_NAME not in drivers:
                        continue
                    node = await self._get_node(node_name)
                    if node is None:
                        continue
                    labels = node.raw.get("metadata", {}).get("labels", {}) or {}
                    if PROBED_LABEL in labels:
                        continue
                    kube_arch = labels.get("kubernetes.io/arch", "")
                    nix_system = KUBE_ARCH_TO_NIX_SYSTEM.get(kube_arch)
                    if not nix_system or nix_system not in set(self.systems):
                        log.warning("probe_node_unsupported", node=node_name, arch=kube_arch)
                        continue
                    self._pending_probes.add(node_name)
                    self._detach(
                        self._probe_node,
                        node_name,
                        nix_system,
                        name=f"probe:{node_name}",
                    )
            except anyio.get_cancelled_exc_class():
                raise
            except Exception:
                log.warning("node_watch_error", exc_info=True)
                await anyio.sleep(10)

    async def _get_node(self, node_name: str) -> Any | None:
        try:
            api = await k8s.api()
            async for node in api.get("nodes", node_name):
                return cast(Any, node)
        except Exception:
            log.warning("probe_node_read_failed", node=node_name, exc_info=True)
        return None

    async def _probe_node(self, node_name: str, system: str) -> None:
        """Probe one node: create builder, wait for probe, label, cleanup."""
        log.info("probe_node_started", node=node_name, system=system)
        job_name = None
        try:
            overrides = {
                "metadata": {
                    "labels": {
                        PROBE_LABEL: "true",
                    },
                },
                # Ask the scheduler for this node. Do not assign it.
                #
                # `nodeName` skips the scheduler entirely: the Pod is placed,
                # not scheduled, so taints, cordons and resources never enter
                # the decision. That is how a probe reached a control plane
                # node and sat in ContainerCreating for three and a half
                # hours.
                #
                # The CSINode gate in `_watch_nodes` already means we only ask
                # about nodes that can mount the volume. This is the other
                # half: everything else that makes a node unusable is the
                # scheduler's to know, and an unplaceable probe is then
                # Pending/Unschedulable -- which names the reason -- instead
                # of Pending/ContainerCreating, which does not.
                #
                # `affinity` merges rather than replaces, so the template's
                # own podAntiAffinity survives. See `deep_merge`.
                "spec": {
                    "template": {
                        "spec": {
                            "affinity": {
                                "nodeAffinity": {
                                    "requiredDuringSchedulingIgnoredDuringExecution": {
                                        "nodeSelectorTerms": [
                                            {
                                                "matchExpressions": [
                                                    {
                                                        "key": "kubernetes.io/hostname",
                                                        "operator": "In",
                                                        "values": [node_name],
                                                    }
                                                ]
                                            }
                                        ]
                                    }
                                }
                            },
                        },
                    },
                },
            }
            job_name = await self._create_builder_job(system=system, overrides=overrides)
            if not job_name:
                return

            # One deadline for both halves of the wait.
            #
            # This was `for _ in range(30)` with a one-second sleep, so a
            # probe gave a builder 30 seconds to register, and that is less
            # than a builder takes to start. Measured on nixlab2: four nodes,
            # `probe_node_started` at 08:36:12 and `probe_store_not_found` at
            # 08:36:43 for every one of them, with the probe Pod still
            # ContainerCreating and 11 seconds old when its Job was deleted.
            # No node was ever labelled, so `_watch_nodes` saw it unlabelled
            # and started again, four seconds later, for ever.
            #
            # A probe is a builder, so it gets the builder's budget:
            # `startup_timeout`, the same knob `_expire_slow_starter` reads.
            #
            # The second wait had no bound at all. A store that registered
            # and never answered held this coroutine open, so the `finally`
            # never ran and the probe Job leaked, pinned to a node and
            # excluded from the watchdog. `_pending_probes` then blocked
            # every later probe of that node.
            store_id = f"builder-{job_name}"
            # `anyio.current_time`, not `time.monotonic`: this deadline is
            # what the `fail_after` below is measured against, and that scope
            # uses the event loop's own clock.
            deadline = anyio.current_time() + self.startup_timeout
            store = None
            while anyio.current_time() < deadline:
                store = self.server.stores.get(store_id)  # type: ignore[arg-type]
                if store is not None:
                    break
                await anyio.sleep(1)

            if store is None:
                log.warning(
                    "probe_store_not_found",
                    job=job_name,
                    node=node_name,
                    waited_seconds=round(self.startup_timeout, 1),
                )
                return

            try:
                with anyio.fail_after(max(deadline - anyio.current_time(), 0.0)):
                    await store._probe_event.wait()
            except TimeoutError:
                log.warning(
                    "probe_no_answer",
                    job=job_name,
                    node=node_name,
                    waited_seconds=round(self.startup_timeout, 1),
                )
                return

            features = store.feature_matrix
            if features:
                await self._label_node(node_name, next(iter(features)), features)
        finally:
            self._pending_probes.discard(node_name)
            if job_name:
                try:
                    job = await Job.get(job_name, namespace=self.namespace)
                    await self._delete_job(job)
                except Exception:  # noqa: BLE001 -- best-effort cleanup of a probe job; reconciliation follows regardless
                    log.warning("probe_job_cleanup_failed", job=job_name)

        await self._reconcile_systems()

    async def _label_node(self, node_name: str, system: str, features: dict[str, set[str]]) -> None:
        """Store probe results on the node as labels and a features annotation."""
        all_feats = sorted({f for feats in features.values() for f in feats})
        try:
            api = await k8s.api()
            async for node in api.get("nodes", node_name):
                node = cast(Any, node)
                await node.patch(
                    {
                        "metadata": {
                            "labels": {
                                PROBED_LABEL: "true",
                            },
                            "annotations": {
                                FEATURES_ANNOTATION: ",".join(all_feats),
                            },
                        },
                    },
                    type="merge",
                )
                break
            log.info(
                "node_labeled_with_probe",
                node=node_name,
                system=system,
                features=all_feats,
            )
        except Exception:
            log.exception("node_labeling_failed", node=node_name)

    # ---- Dynamic features ----

    async def _sync_dynamic_features(self) -> None:
        scheduler = self.server.scheduler
        if scheduler is None:
            return
        features = await self._read_node_features(self._available_systems)
        scheduler.add_dynamic_features(features)
        log.info("dynamic_features_synced", systems=sorted(features))

    async def _read_node_features(self, systems: set[str]) -> dict[str, set[str]]:
        """Read feature matrix from nixkube/features annotation."""
        feature_matrix: dict[str, set[str]] = {}
        try:
            api = await k8s.api()
            async for node in api.get("nodes"):
                node = cast(Any, node)
                labels = getattr(node.metadata, "labels", {}) or {}
                if labels.get(PROBED_LABEL) != "true":
                    continue
                kube_arch = labels.get("kubernetes.io/arch", "")
                sys_label = KUBE_ARCH_TO_NIX_SYSTEM.get(kube_arch)
                if sys_label not in systems or sys_label in feature_matrix:
                    continue
                annotations = getattr(node.metadata, "annotations", {}) or {}
                raw = annotations.get(FEATURES_ANNOTATION, "")
                features = set(filter(None, raw.split(","))) if raw else set()
                feature_matrix[sys_label] = features or set(_DEFAULT_FEATURES)
        except Exception:
            log.exception("read_node_features_error")
        for s in systems:
            feature_matrix.setdefault(s, set(_DEFAULT_FEATURES))
        return feature_matrix

    async def _reconcile_systems(self) -> None:
        """Read node kubernetes.io/arch to determine available systems."""
        enabled = set(self.systems)
        any_probed = False
        available: set[str] = set()

        try:
            api = await k8s.api()
            async for node in api.get("nodes"):
                node = cast(Any, node)
                labels = getattr(node.metadata, "labels", {}) or {}
                if labels.get(PROBED_LABEL) == "true":
                    any_probed = True
                    kube_arch = labels.get("kubernetes.io/arch", "")
                    system = KUBE_ARCH_TO_NIX_SYSTEM.get(kube_arch)
                    if system and system in enabled:
                        available.add(system)
        except Exception:
            log.exception("system_reconcile_error")
            return

        if not any_probed:
            return

        old = self._available_systems
        self._available_systems = available
        await self._sync_dynamic_features()

        if available != old:
            log.info(
                "available_systems_changed",
                available=sorted(available),
                removed=sorted(old - available),
                added=sorted(available - old),
            )

    # ---- Job watching and reconciliation ----

    async def _resync_jobs(self) -> None:
        """List every builder Job and reconcile it.

        Also drops state for Jobs that are gone, so `_counted_failures` does
        not grow for the life of the process.
        """
        api = await k8s.api()

        seen: set[str] = set()
        async for job in api.get(
            "jobs",
            namespace=self.namespace,
            label_selector={BUILDER_LABEL: BUILDER_LABEL_VALUE},
        ):
            job = cast(Any, job)
            seen.add(job.metadata.name)
            await self._reconcile_job(job)

        for store_id in list(self._registered):
            store_id_prefix = "builder-"
            if store_id.startswith(store_id_prefix):
                job_name = store_id[len(store_id_prefix) :]
                if job_name not in seen:
                    await self._unregister_builder(store_id)

        self._counted_failures &= seen
        self._ever_ready &= seen
        self._aged &= seen

    async def _watch_jobs(self) -> None:
        while True:
            try:
                api = await k8s.api()

                async with self._reconcile_lock:
                    await self._resync_jobs()
                    await self._ensure_min_builders()

                async for event in api.watch(
                    "jobs",
                    namespace=self.namespace,
                    label_selector={BUILDER_LABEL: BUILDER_LABEL_VALUE},
                ):
                    event_type, job = cast(Any, event)
                    if job is None:
                        continue

                    async with self._reconcile_lock:
                        if event_type == "DELETED":
                            store_id = f"builder-{job.metadata.name}"
                            if store_id in self._registered:
                                await self._unregister_builder(store_id)
                            self._forget_job(job.metadata.name)
                            await self._ensure_min_builders()
                        elif event_type in ("ADDED", "MODIFIED"):
                            await self._reconcile_job(job)
                            await self._ensure_min_builders()
            except Exception as e:  # noqa: BLE001 -- watch loop: any failure is retried after _RECONNECT_DELAY
                log.warning("builder_watch_error", error=f"{type(e).__name__}: {e}")

            await anyio.sleep(_RECONNECT_DELAY)

    async def _reconcile_job(self, job: Any) -> None:
        job_name = job.metadata.name
        store_id = f"builder-{job_name}"

        if await self._is_stale_builder(job):
            log.info("builder_stale", store_id=store_id, job_name=job_name)
            await self._unregister_builder(store_id)
            await self._delete_job(job)
            return

        labels = getattr(job.metadata, "labels", {}) or {}
        is_probe = labels.get(PROBE_LABEL) == "true"
        system = labels.get(SYSTEM_LABEL, "unknown")

        status = job.raw.get("status", {})
        if status.get("succeeded") or status.get("failed"):
            if (
                status.get("failed")
                and not is_probe
                # The Pod is fetched only for its node name, and only once.
                # A failed Job is reconciled again until its TTL reaps it.
                and job_name not in self._counted_failures
            ):
                failed_on = (await self._get_job_pod_state(job)).node
                self._record_failure(system, job_name, reason="JobFailed", node=failed_on)
            if store_id in self._registered:
                await self._unregister_builder(store_id)
            return

        if not is_probe and self._is_aged(job.raw, datetime.now(UTC)):
            if job_name not in self._aged:
                log.info("builder_max_age_reached", job=job_name, system=system)
            self._aged.add(job_name)

        pod = await self._get_job_pod_state(job)

        if pod.ready:
            self._ever_ready.add(job_name)
            if not is_probe:
                self._record_ready(system, job_name)
        elif not is_probe and await self._expire_slow_starter(job, system, pod):
            return

        if store_id not in self._registered:
            if pod.ip:
                await self._register_builder(store_id, pod.ip, job_name, probe=is_probe)
        else:
            self._job_names[store_id] = job_name
        if not is_probe and store_id in self._registered:
            self._store_systems[store_id] = system

    @staticmethod
    def _builder_store_spec(store_id: str, pod_ip: str, probe: bool = False) -> SSHSubprocessStoreSpec:
        """The store spec for one builder Pod.

        `known_hosts` has no default in pynixd, on purpose: a build store sees
        every build a client pushes and returns the paths the client then
        registers as valid, so the host key is what makes the far side the
        machine the configuration named. This call did not answer the
        question, so every registration raised a ValidationError and no
        builder ever registered.

        The answer is the file the deployment already writes. `secret.nix`
        generates one ed25519 pair for the whole namespace and puts the public
        half in the `ssh-dynauth` ConfigMap as `* <key>`. That ConfigMap is
        mounted at `/etc/ssh-dynauth` in this Pod, and asyncssh matches a
        known_hosts host pattern with `fnmatch`, so the wildcard covers every
        builder IP.

        `None` would have been one character shorter and would mean "accept
        any host key". Builders are addressed by Pod IP, and a cluster reuses
        those, so that is worth avoiding for the price.
        """
        return SSHSubprocessStoreSpec(
            store_id=StoreId(store_id),
            host=pod_ip,
            port=22,
            username="nix",
            known_hosts=BUILDER_KNOWN_HOSTS,
            client_keys=["/etc/ssh-key/id_ed25519"],
            nix_bin="/nix/var/result/bin/nix",
            monitor=False,
            no_schedule=probe,
        )

    async def _register_builder(self, store_id: str, pod_ip: str, job_name: str = "", probe: bool = False) -> None:
        store = SSHSubprocessStore(self._builder_store_spec(store_id, pod_ip, probe))
        try:
            await self.server.add_store(store, dynamic=True)
            self._registered[store_id] = pod_ip
            self._idle_since[store_id] = time.monotonic()
            if job_name:
                self._job_names[store_id] = job_name
            log.info("builder_registered", store_id=store_id, pod_ip=pod_ip)
        except Exception:
            log.exception("builder_register_failed", store_id=store_id, pod_ip=pod_ip)

    async def _unregister_builder(self, store_id: str) -> None:
        try:
            await self.server.remove_store(store_id)  # type: ignore[arg-type]
            self._registered.pop(store_id, None)
            self._idle_since.pop(store_id, None)
            self._job_names.pop(store_id, None)
            self._store_systems.pop(store_id, None)
            log.info("builder_unregistered", store_id=store_id)
        except Exception:
            log.exception("builder_unregister_failed", store_id=store_id)

    async def _is_stale_builder(self, job: Any) -> bool:
        expected = await self._get_expected_store_versions()
        if not expected:
            return False
        job_versions = self._get_job_store_versions(job)
        return not job_versions <= expected

    @staticmethod
    def _get_job_store_versions(job: Any) -> set[str]:
        for vol in getattr(job, "raw", {}).get("spec", {}).get("template", {}).get("spec", {}).get("volumes", []):
            csi = vol.get("csi", {})
            if csi.get("driver") == "nixkube" and csi.get("readOnly") is False:
                found = set()
                for val in csi.get("volumeAttributes", {}).values():
                    if isinstance(val, str) and val.startswith("/nix/store/"):
                        found.add(val)
                return found
        return set()

    async def _get_expected_store_versions(self) -> set[str]:
        try:
            template = await PodTemplate.get(BUILDER_PODTEMPLATE_NAME, namespace=self.namespace)
        except Exception:
            log.exception("builder_podtemplate_get_failed")
            return set()
        pod_spec = template.raw.get("template", {})
        for vol in pod_spec.get("spec", {}).get("volumes", []):
            csi = vol.get("csi", {})
            if csi.get("driver") == "nixkube" and csi.get("readOnly") is False:
                found = set()
                for val in csi.get("volumeAttributes", {}).values():
                    if isinstance(val, str) and val.startswith("/nix/store/"):
                        found.add(val)
                return found
        return set()

    async def _get_job_pod_state(self, job: Any) -> "PodState":
        """Return what the Job's Pod says about itself.

        Four signals, four uses. A Pod has an IP once its sandbox is up,
        before the container runs, and registration uses that. Ready follows
        the readinessProbe on the ssh port, so it means the builder answers,
        and the failure backoff uses that. The phase is what the startup
        watchdog acts on, because it is the one signal the API server holds
        rather than this process. The node is for the operator: the backoff is
        per system, and a failure is usually per node.
        """
        state = PodState()
        try:
            api = await k8s.api()
            async for pod in api.get(
                "pods",
                namespace=self.namespace,
                label_selector={"job-name": job.metadata.name},
            ):
                pod = cast(Any, pod)
                status = pod.raw.get("status", {})
                state.phase = status.get("phase")
                state.node = pod.raw.get("spec", {}).get("nodeName")
                state.ip = status.get("podIP")
                if state.ip:
                    state.ready = _pod_is_ready(status)
                    return state
        except Exception:
            log.exception("job_pod_ip_fetch_failed", job=job.metadata.name)
        return state

    # ---- Builder min/max lifecycle ----

    def _is_aged(self, job_raw: dict, now: datetime) -> bool:
        if self.max_age <= 0:
            return False
        age = _job_age_seconds(job_raw, now)
        return age is not None and age > self.max_age

    async def _list_builder_jobs(self) -> list[Any]:
        api = await k8s.api()
        return [
            cast(Any, job)
            async for job in api.get(
                "jobs",
                namespace=self.namespace,
                label_selector={BUILDER_LABEL: BUILDER_LABEL_VALUE},
            )
        ]

    async def _ensure_min_builders(self) -> None:
        try:
            jobs = await self._list_builder_jobs()
        except Exception:
            log.exception("builder_job_count_error")
            return

        now = datetime.now(UTC)
        active_by_system: dict[str, int] = {}
        for job in jobs:
            status = job.raw.get("status", {})
            if status.get("succeeded") or status.get("failed"):
                continue
            labels = getattr(job.metadata, "labels", {}) or {}
            if labels.get(PROBE_LABEL) == "true":
                continue
            # An aged builder counts toward neither the minimum nor the
            # maximum, so its replacement starts while it still serves. At
            # `max == min` the replacement could not start otherwise.
            if self._is_aged(job.raw, now):
                continue
            sys_label = labels.get(SYSTEM_LABEL, "unknown")
            active_by_system[sys_label] = active_by_system.get(sys_label, 0) + 1

        total_active = sum(active_by_system.values())

        for system in sorted(self._available_systems):
            active = active_by_system.get(system, 0)
            if active >= self.min_builders:
                continue
            if total_active >= self.max_builders:
                break

            remaining = self._backoff_remaining(system)
            if remaining > 0:
                log.info(
                    "builder_backoff_hold",
                    system=system,
                    active=active,
                    consecutive_failures=self._failures.get(system, 0),
                    retry_in_seconds=round(remaining, 1),
                )
                continue

            log.info(
                "ensuring_min_builders",
                system=system,
                active=active,
                min_builders=self.min_builders,
                max_builders=self.max_builders,
            )
            await self._create_builder_job(system=system)
            total_active += 1
            await anyio.sleep(0.5)

    async def _maybe_create_builder(self, system: str) -> None:
        now = time.monotonic()
        last = self._last_create_time.get(system, 0.0)
        if now - last < self.cooldown_seconds:
            return

        remaining = self._backoff_remaining(system)
        if remaining > 0:
            log.info(
                "builder_backoff_hold",
                system=system,
                consecutive_failures=self._failures.get(system, 0),
                retry_in_seconds=round(remaining, 1),
            )
            return

        try:
            jobs = await self._list_builder_jobs()
        except Exception:
            log.exception("builder_job_count_error")
            return

        wall = datetime.now(UTC)
        active = 0
        for job in jobs:
            status = job.raw.get("status", {})
            if status.get("succeeded") or status.get("failed"):
                continue
            labels = getattr(job.metadata, "labels", {}) or {}
            # Aged builders are leaving; see `_ensure_min_builders`.
            if labels.get(PROBE_LABEL) == "true" or self._is_aged(job.raw, wall):
                continue
            active += 1

        if active >= self.max_builders:
            log.debug(
                "builder_max_reached",
                active=active,
                max=self.max_builders,
                system=system,
            )
            return

        log.info("creating_builder_job", active=active, max=self.max_builders, system=system)
        await self._create_builder_job(system=system)
        self._last_create_time[system] = now

    def _is_aged_store(self, store_id: str) -> bool:
        return self._job_names.get(store_id) in self._aged

    def _is_busy(self, store: Any) -> bool:
        """True while the store has work, sent or only assigned.

        `in_flight` counts connections in use. A build the scheduler has
        assigned but not yet sent does not show there.
        """
        if store.in_flight > 0:
            return True
        scheduler = self.server.scheduler
        if scheduler is None:
            return False
        return any(build.assigned_store_id == store.store_id and not build.is_done for build in scheduler.queue.queue)

    async def _recycle_aged(self) -> None:
        """Drain each aged builder once its replacement is Ready, then retire it."""
        views = []
        # SSHSubprocessStore, which carries `draining`; the mapping's type is
        # the Store base class, which does not.
        stores: dict[str, Any] = {}
        for store_id, system in self._store_systems.items():
            store = self.server.stores.get(store_id)  # type: ignore[arg-type]
            job_name = self._job_names.get(store_id)
            if store is None or job_name is None:
                continue
            stores[store_id] = store
            views.append(
                RecycleView(
                    store_id=store_id,
                    system=system,
                    aged=job_name in self._aged,
                    ready=job_name in self._ever_ready and store.is_healthy,
                    draining=stores[store_id].draining,
                    busy=self._is_busy(store),
                )
            )
        drain, retire = plan_recycle(views, self.min_builders)

        for store_id in drain:
            stores[store_id].draining = True
            log.info("builder_max_age_draining", store_id=store_id)
        if drain and self.server.scheduler is not None:
            self.server.scheduler.trigger()

        for store_id in retire:
            log.info("builder_max_age_retired", store_id=store_id)
            await self._delete_builder_job(store_id)
            await self._unregister_builder(store_id)

    async def _reap_idle(self) -> None:
        """Delete builders idle past `idle_timeout`, down to the minimum.

        Aged builders are left to `_recycle_aged`, and do not count toward the
        minimum here either: an idle replacement must not be reaped while the
        builder it replaces drains.
        """
        while True:
            try:
                fresh = [s for s in self._store_systems if s in self._registered and not self._is_aged_store(s)]
                remaining = len(fresh)
                now = time.monotonic()
                for store_id in fresh:
                    if remaining <= self.min_builders:
                        break
                    store = self.server.stores.get(store_id)  # type: ignore[arg-type]
                    if store is None:
                        continue

                    if store.in_flight > 0:
                        self._idle_since[store_id] = now
                        continue

                    idle_seconds = now - self._idle_since.get(store_id, now)
                    if idle_seconds < self.idle_timeout:
                        continue

                    log.info(
                        "builder_idle_timeout",
                        store_id=store_id,
                        pod_ip=self._registered.get(store_id),
                        idle_seconds=idle_seconds,
                    )
                    await self._delete_builder_job(store_id)
                    # The watch unregisters it later, so count it gone now.
                    remaining -= 1
            except Exception:
                log.exception("builder_idle_reap_error")

            await anyio.sleep(60)

    async def _delete_job(self, job: Any) -> None:
        try:
            await job.delete(propagation_policy="Background")
            log.info("builder_job_deleted", job=job.metadata.name)
        except Exception:
            log.exception("builder_job_delete_failed", job=job.metadata.name)

    async def _delete_builder_job(self, store_id: str) -> None:
        job_name = self._job_names.get(store_id)
        if not job_name:
            return

        try:
            job = await Job.get(job_name, namespace=self.namespace)
            await self._delete_job(job)
        except Exception:
            log.exception("builder_job_delete_idle_failed", job=job_name)

    async def _reap_orphaned_builder_pods(self) -> None:
        try:
            api = await k8s.api()
            async for pod in api.get(
                "pods",
                namespace=self.namespace,
                label_selector={BUILDER_LABEL: BUILDER_LABEL_VALUE},
            ):
                pod = cast(Any, pod)
                refs = getattr(pod.metadata, "ownerReferences", None)
                if not refs:
                    log.info("orphaned_builder_pod_found", pod=pod.metadata.name)
                    await pod.delete(propagation_policy="Background")
        except Exception:
            log.exception("orphaned_builder_pod_cleanup_error")

    # ---- Queue watching for reactive builder creation ----

    async def _watch_queue(self) -> None:
        scheduler = self.server.scheduler
        if scheduler is None:
            return

        while True:
            try:
                needed_systems = pending_systems(scheduler.queue.queue, self._available_systems)

                if not needed_systems:
                    await anyio.sleep(5)
                    continue

                active_per_system: dict[str, int] = {}
                for s in self.server.stores.values():
                    if s.store_id.startswith("builder-") and s.is_healthy and not s.draining and not s.no_schedule:
                        fm = s.feature_matrix
                        if fm:
                            for system in fm:
                                active_per_system[system] = active_per_system.get(system, 0) + 1
                        else:
                            for system in self._available_systems:
                                active_per_system[system] = active_per_system.get(system, 0) + 1

                for system in sorted(needed_systems):
                    if active_per_system.get(system, 0) == 0:
                        await self._maybe_create_builder(system)
            except Exception:
                log.exception("builder_queue_watch_error")

            await anyio.sleep(5)

    # ---- Job creation ----

    def _build_job_resource(self, system: str, pod_spec: dict, overrides: dict | None = None) -> dict:
        """The Job a builder is created from.

        `activeDeadlineSeconds` is deliberately absent. It measures a Job's
        whole active time, from `status.startTime`, and cannot tell a Pod that
        hangs in Pending from a builder that is serving builds. A builder Job
        is long-lived by design -- one measured at 3h36m, still Running -- so
        any value short enough to bound the hung case would be the usual way a
        healthy builder dies, mid-build. `_expire_slow_starter` bounds a
        builder that never starts, because it knows whether the builder has
        ever been Ready. `_recycle_aged` bounds a healthy one at `max_age`,
        because it knows whether the builder is busy.

        `backoffLimit: 0` means a failed Job is not retried in place, and
        `ttlSecondsAfterFinished` reaps it. Neither helps a Job that never
        finishes.
        """
        job_id = str(uuid.uuid4())[:8]
        job_resource = {
            "apiVersion": "batch/v1",
            "kind": "Job",
            "metadata": {
                "name": f"nixkube-builder-{job_id}",
                "namespace": self.namespace,
                "labels": {
                    BUILDER_LABEL: BUILDER_LABEL_VALUE,
                    SYSTEM_LABEL: system,
                },
            },
            "spec": {
                "ttlSecondsAfterFinished": 300,
                "backoffLimit": 0,
                "template": pod_spec,
            },
        }
        if overrides:
            job_resource = deep_merge(job_resource, overrides)
        return job_resource

    async def _create_builder_job(self, system: str, overrides: dict | None = None) -> str | None:
        try:
            template = await PodTemplate.get(BUILDER_PODTEMPLATE_NAME, namespace=self.namespace)
        except Exception:  # noqa: BLE001 -- absent, malformed or unreachable all mean the same thing to the caller
            log.error("builder_podtemplate_not_found", name=BUILDER_PODTEMPLATE_NAME)
            return None

        pod_spec = dict(template.raw.get("template", {}))

        kube_arch = NIX_SYSTEM_TO_KUBE_ARCH.get(system)
        if kube_arch:
            spec = pod_spec.setdefault("spec", {})
            node_selector = spec.setdefault("nodeSelector", {})
            node_selector["kubernetes.io/arch"] = kube_arch

        job_resource = self._build_job_resource(system, pod_spec, overrides)
        job_name = job_resource["metadata"]["name"]

        try:
            job = await Job(job_resource)
            await job.create()
            log.info(
                "builder_job_created",
                job=job_name,
                system=system,
                node_name=job_resource["spec"]["template"].get("spec", {}).get("nodeName"),
            )
            return job_name
        except Exception:
            log.exception(
                "builder_job_create_failed",
                job=job_name,
                system=system,
            )
            return None
