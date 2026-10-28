# SPDX-License-Identifier: Apache-2.0
"""Prometheus metrics for the nixkube daemon.

Served on `METRICS_ADDR:METRICS_PORT`, which `cli.py` starts once at the top
of `async_main`. A DaemonSet pod answers for its own node, so every series
here is about this node and not about the cluster.

The server is the threaded one that `prometheus_client` ships. The daemon is
async and a thread is off-pattern, but the alternative is an ASGI server and
a second HTTP stack in the image for one read-only route. The values that
thread reads are lock-protected by `prometheus_client`, so the loop writing
them while it serves is safe.

**Every label here is bounded.** A store path, a container id or a pod name
would give one series per value, and a node sees thousands of each. `result`,
`kind`, `service` and `event` are written out in this file, and `command`
comes from an allow-list.

`prometheus_client` registers `ProcessCollector`, `PlatformCollector` and
`GCCollector` on the default registry by itself, so `process_cpu_seconds_total`,
`process_resident_memory_bytes`, `process_open_fds` and `python_gc_*` are
already served. Do not add a gauge for any of them.
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Iterator
from importlib.metadata import PackageNotFoundError, version

import anyio
import structlog
from prometheus_client import REGISTRY, Counter, Gauge, Histogram, start_http_server
from prometheus_client.core import GaugeMetricFamily, Metric

from .constants import NIX_ROOT, PYNIXD_ENABLED

logger = structlog.get_logger("nixkube.metrics")

# --- Garbage collection ---

GC_CYCLES = Counter(
    "nixkube_gc_cycles_total",
    "Garbage collection cycles this daemon has finished",
    ["result"],  # ok, error
)

GC_PATHS_DELETED = Counter(
    "nixkube_gc_paths_deleted_total",
    "Store paths deleted by garbage collection",
)

GC_CYCLE_DURATION = Histogram(
    "nixkube_gc_cycle_duration_seconds",
    "Time one garbage collection cycle took",
    buckets=(1, 5, 15, 30, 60, 120, 300, 600),
)

# Zero until the first cycle finishes, so an alert reads it as
# `== 0 or time() - it > N`. That is the shape `node_boot_time_seconds` and
# the textfile collector use for the same thing.
#
# **This is the series that would have surfaced #38 on day one.** A timestamp
# that stops advancing says the collector is stuck, which no size gauge says
# on its own: the store grew for 33 hours while every other number looked
# ordinary.
GC_LAST_SUCCESS = Gauge(
    "nixkube_gc_last_success_timestamp_seconds",
    "When a garbage collection cycle last finished, in unix seconds",
)

# --- The store itself ---
#
# Free: `_run_gc_cycle` already reads `nix path-info --all --json` for
# `registrationTime`, and `narSize` rides along in the same answer. So these
# cost nothing beyond the call the GC cycle already makes, and they move on
# the GC cadence rather than the scrape one.

# **`nar_bytes`, and not `store_bytes`.** A NAR size is what the path weighs on
# the wire. It ignores the hard links and the block overhead of the store on
# disk, so it is not the number `du` gives. Measured on a workstation: summing
# `narSize` over `ValidPaths` answered 952 GB for a file system of 268 GB. The
# name says `nar` so that nobody subtracts it from
# `nixkube_store_filesystem_size_bytes` beside it. It is the right series for
# watching growth, and the wrong one for how full the disk is.
STORE_NAR_BYTES = Gauge(
    "nixkube_store_nar_bytes",
    "Sum of the NAR size of every path in this node's store, which over-counts disk use",
)

STORE_PATHS = Gauge(
    "nixkube_store_paths",
    "Paths in this node's store",
)

# --- Volumes ---
#
# A CSI volume is published when a pod starts and unpublished when it stops,
# so these two counters and the gauge derived from them say how much of this
# node's work nixkube is carrying.

VOLUME_MOUNTS = Counter(
    "nixkube_volume_mounts_total",
    "Volumes this daemon has mounted",
    ["result"],  # ok, error
)

VOLUME_UNMOUNTS = Counter(
    "nixkube_volume_unmounts_total",
    "Volumes this daemon has unmounted",
    ["result"],  # ok, error
)

VOLUME_PREPARE_DURATION = Histogram(
    "nixkube_volume_prepare_duration_seconds",
    "Time spent realising the closure of one volume before it is mounted",
    buckets=(0.5, 1, 5, 15, 30, 60, 120, 300),
)

# Mounts minus unmounts, kept here rather than derived in a query: a counter
# difference across a restart is wrong, and this is the number that says
# whether the node is leaking volumes.
VOLUMES_PUBLISHED = Gauge(
    "nixkube_volumes_published",
    "Volumes this daemon has mounted and not yet unmounted",
)

# --- Mount budget ---
#
# `fs.mount-max` counts per mount namespace, and a bind farm spends one per
# closure path. Nothing outside a node can see its mount table fill up, and
# the first symptom otherwise is a container that will not start with an
# ENOSPC that names no limit. These two are what an alert reads:
# `used / limit` past about 0.8 means the next large closure may not fit.

MOUNT_NAMESPACE_USED = Gauge(
    "nixkube_mount_namespace_mounts",
    "Mounts held by this daemon's mount namespace",
)

MOUNT_NAMESPACE_LIMIT = Gauge(
    "nixkube_mount_namespace_limit",
    "fs.mount-max, the most this namespace may hold",
)

MOUNT_BUDGET_FALLBACKS = Counter(
    "nixkube_mount_budget_fallbacks_total",
    "Volumes given a hardlink tree because a bind farm would not leave headroom",
)

# --- Hardlinking ---
#
# Irreducible work: linking a closure into a container's farm is a metadata
# syscall per file and there is no cheaper way to do it. It also runs on the
# loop that answers the NRI heartbeat, and `nri-wait` gives up after 30 s of
# silence, so how long this takes decides whether a container starts.

HARDLINK_CLOSURES = Counter(
    "nixkube_hardlink_closures_total",
    "Closures linked into a container's volume",
    ["result"],  # ok, error
)

HARDLINK_CLOSURE_DURATION = Histogram(
    "nixkube_hardlink_closure_duration_seconds",
    "Time spent linking one closure into a volume",
    buckets=(0.1, 0.5, 1, 5, 15, 30, 60, 120, 300),
)

HARDLINK_PATHS = Counter(
    "nixkube_hardlink_paths_total",
    "Store paths considered for linking, whether or not the volume already had them",
)

# --- CSI presentation ---

COMPOSEFS_AVAILABLE = Gauge(
    "nixkube_composefs_available",
    "1 when this node's kernel passed the composefs probe, 0 when it failed"
    " and volumes fall back to hardlink trees and bind farms",
)

# --- Host store ---

OLD_STORE_BYTES = Gauge(
    "nixkube_old_store_bytes",
    "Bytes on disk of a host-store node's former separate store, until it is retired (issue #25); 0 when there is none",
)

# --- Nix builds ---
#
# `kind` is which of the three volume attributes asked for this build, and
# never the path or the expression itself.

NIX_BUILDS = Counter(
    "nixkube_nix_builds_total",
    "Nix builds this daemon has run",
    [
        "kind",
        "result",
    ],  # kind: store_path, flake_ref, nix_expr, packages, composefs_image
)

NIX_BUILD_DURATION = Histogram(
    "nixkube_nix_build_duration_seconds",
    "Time one nix build took",
    ["kind"],
    buckets=(1, 5, 15, 30, 60, 120, 300, 600, 1800),
)

# --- Subprocesses ---
#
# Everything nixkube does to the store is a subprocess, and `run_console` is
# the one funnel they all pass through.

SUBPROCESS_CALLS = Counter(
    "nixkube_subprocess_calls_total",
    "Subprocesses this daemon has run",
    ["command", "result"],  # result: ok, error, timeout
)

SUBPROCESS_DURATION = Histogram(
    "nixkube_subprocess_duration_seconds",
    "Time one subprocess took",
    ["command"],
    buckets=(0.05, 0.25, 1, 5, 15, 60, 300, 900),
)

# --- NRI ---

# `NRI_CONTAINERS_SEEN`, because `constants.NRI_CONTAINERS` is the farm
# directory and `nri/server.py` imports both.
NRI_CONTAINERS_SEEN = Counter(
    "nixkube_nri_containers_total",
    "Containers this plugin has seen create",
    ["result"],  # injected, no_paths, already_mounted, excluded, error
)

NRI_STATE_CHANGES = Counter(
    "nixkube_nri_state_changes_total",
    "State change events the runtime has sent",
    ["event"],  # the NRI event name, which is an enum
)

NRI_BUILDS = Counter(
    "nixkube_nri_builds_total",
    "Build tasks this plugin has finished",
    ["result"],  # ok, error
)

NRI_BUILDS_IN_FLIGHT = Gauge(
    "nixkube_nri_builds_in_flight",
    "Build tasks running now",
)

NRI_BUILD_DURATION = Histogram(
    "nixkube_nri_build_duration_seconds",
    "Time one build task took, from spawn to mounted",
    buckets=(1, 5, 15, 30, 60, 120, 300, 600, 1800),
)

# --- The cache ---
#
# This is the node's own view of pynixd. A node whose builds are slow and
# whose cache is unreachable has one fault and not two.

CACHE_REACHABLE = Gauge(
    "nixkube_cache_reachable",
    "Whether the last connectivity check reached pynixd (1 = yes)",
)

# **Read this before alerting on `cache_reachable`.** An unset Gauge reads 0,
# which is the same number a failed check writes. `cache_probe_loop` checks
# at startup, then every `CACHE_PROBE_INTERVAL_SECONDS`, and sooner after a
# failure; a mount and a successful copy also write it. So the gap is short,
# but it exists: between the start of the process and the first answer.
#
# This timestamp is what separates the two. It is written by every
# completed check, whatever the answer, and stays 0 while none has run.
# A consumer that wants a real failure asks for both.
CACHE_LAST_CHECK = Gauge(
    "nixkube_cache_last_check_timestamp_seconds",
    "When the last connectivity check finished, 0 if none has run",
)

# Without this, `cache_reachable == 0` fires on every cluster that runs no
# pynixd, where it is the correct state and not a fault. The alert is
# `configured == 1 and reachable == 0 and last_check_timestamp_seconds > 0`,
# and the third clause is not optional -- see `CACHE_LAST_CHECK`.
#
# `rate(cache_copy_attempts_total{result="error"}[15m])` needs none of this
# and is the better first alert: it is an event that happened rather than a
# state somebody has to have refreshed.
CACHE_CONFIGURED = Gauge(
    "nixkube_cache_configured",
    "Whether this node is configured to use pynixd as a cache at all (1 = yes)",
)
CACHE_CONFIGURED.set(1 if PYNIXD_ENABLED else 0)

CACHE_COPIES = Counter(
    "nixkube_cache_copies_total",
    "Copies of a closure to the cache",
    ["result"],  # ok, exhausted, timeout
)

CACHE_COPY_ATTEMPTS = Counter(
    "nixkube_cache_copy_attempts_total",
    "Individual `nix copy` attempts, retries included",
    ["result"],  # ok, error, timeout
)

CACHE_COPY_DURATION = Histogram(
    "nixkube_cache_copy_duration_seconds",
    "Time one copy to the cache took, retries included",
    buckets=(1, 5, 15, 60, 300, 900, 1800),
)

# --- Supervision ---
#
# A restart count was the diagnosis twice over on the nixlab2 cluster, and
# both times it came from `kubectl` rather than from a series anybody could
# alert on.

SERVICE_RESTARTS = Counter(
    "nixkube_service_restarts_total",
    "Times a supervised service crashed and was restarted",
    ["service"],
)

SERVICE_CRASH_LOOPS = Counter(
    "nixkube_service_crash_loops_total",
    "Times a supervised service exceeded its restart threshold and took the process down",
    ["service"],
)

# --- The event loop ---
#
# nixkube blocks its own loop: linking a closure is synchronous work on the
# loop that answers the NRI heartbeat, and `nri-wait` gives up after 30 s.
# A stalled loop looks exactly like a slow build from outside, and this is the
# series that tells them apart.

EVENT_LOOP_LAG = Gauge(
    "nixkube_event_loop_lag_seconds",
    "Seconds the event loop went without running a ready callback, over the last window",
)

# Not alertable, and the name does not say so. This never decays, so a
# threshold it crosses stays crossed until the process restarts. It answers
# "did this ever stall"; `nixkube_event_loop_lag_seconds` above answers "is
# it stalling now", and that is the one to alert on.
EVENT_LOOP_LAG_MAX = Gauge(
    "nixkube_event_loop_lag_max_seconds",
    "Largest event loop stall seen since the process started; never decays",
)

# --- Build info ---

BUILD_INFO = Gauge(
    "nixkube_build_info",
    "Always 1. The version rides on the label, which is how a dashboard joins on it",
    ["version"],
)

try:
    BUILD_INFO.labels(version=version("nixkube")).set(1)
except PackageNotFoundError:
    # A source tree with no installed distribution. The series goes absent,
    # which is truthful, and is not a reason to fail an import.
    logger.debug("build_info_version_unavailable")


_NIX_SUBCOMMANDS = frozenset(
    {
        "build",
        "copy",
        "derivation",
        "eval",
        "log",
        "path-info",
        "realisation",
        "store",
        "why-depends",
    }
)
_NIX_STORE_SUBCOMMANDS = frozenset(
    {
        "copy-sigs",
        "delete",
        "gc",
        "info",
        "optimise",
        "ping",
        "repair-path",
        "sign",
        "verify",
    }
)


def command_label(args: tuple[object, ...]) -> str:
    """A bounded name for a command line.

    **An allow-list, and not "the leading tokens that are not flags".** A
    caller passes a store path, a flake reference or a temporary file name
    straight after the subcommand, and any of those as a label gives one
    series per value. `nix build nixpkgs#hello` is the case that looks safe
    and is not.
    """
    if not args:
        return "unknown"
    binary = os.path.basename(str(args[0]))
    if binary != "nix" or len(args) < 2:
        return binary
    sub = str(args[1])
    if sub not in _NIX_SUBCOMMANDS:
        return binary
    if sub == "store" and len(args) >= 3 and str(args[2]) in _NIX_STORE_SUBCOMMANDS:
        return f"nix store {args[2]}"
    return f"nix {sub}"


class LoopLagMonitor:
    """Sample how long the loop goes without running a ready callback.

    The measurement is what a `sleep` overshoots by: the loop was asked to
    wake this task after `interval` and did not, so the difference is time it
    spent not scheduling. That is what the NRI heartbeat experiences.

    `window_max` decays, because a stall a minute ago says nothing about now.
    `lifetime_max` does not, because the worst stall a process ever had is
    what an operator wants after the fact.
    """

    def __init__(self, interval: float = 0.25, window: float = 30.0) -> None:
        self._interval = interval
        self._window = window
        self._samples: list[tuple[float, float]] = []
        self.lifetime_max = 0.0

    @property
    def window_max(self) -> float:
        cutoff = time.monotonic() - self._window
        self._samples = [(t, lag) for t, lag in self._samples if t >= cutoff]
        return max((lag for _, lag in self._samples), default=0.0)

    async def run(self) -> None:
        """Sample until cancelled. Intended to run as a background task."""
        while True:
            t0 = time.monotonic()
            await anyio.sleep(self._interval)
            lag = time.monotonic() - t0 - self._interval
            if lag < 0:
                # A sleep that returned early is not a stall, and recording it
                # would lower the maximum.
                continue
            self._samples.append((time.monotonic(), lag))
            self.lifetime_max = max(self.lifetime_max, lag)
            EVENT_LOOP_LAG.set(self.window_max)
            EVENT_LOOP_LAG_MAX.set(self.lifetime_max)


class StoreSpaceCollector:
    """How much room this node's store has, read when a scrape asks.

    A collector, and not a gauge that something keeps up to date: the answer
    costs one `statvfs`, and nothing else in nixkube needs it, so reading it
    on the scrape is cheaper and fresher than a periodic write.

    **This is the file system that holds the store, and not the sum of what is
    in it.** Those are different numbers, and the difference is large: on a
    development machine `sum(narSize)` over Nix's `ValidPaths` answered 952 GB
    for a file system of 268 GB, because NAR sizes add up without the
    deduplication and the hard links the store on disk has.

    That sum is also too slow to serve. It took 584 ms over 141,749 paths,
    against 0.010 ms for this `statvfs`. A node scraped every 15 s cannot pay
    half a second, and the number it bought would answer the wrong question.

    `NIX_ROOT` is `/` and these are pod paths, so this reads the store the
    DaemonSet shares with its node.
    """

    def collect(self) -> Iterator[Metric]:
        """Yield the two numbers, or nothing when the store is unreachable."""
        path = NIX_ROOT / "nix/store"
        try:
            stat = os.statvfs(path)
        except OSError:
            # An unreadable store is not a reason to fail the scrape: every
            # other series is still an answer. These two go absent, which a
            # dashboard shows as absent.
            logger.warning("store_space_unreadable", path=str(path), exc_info=True)
            return

        yield GaugeMetricFamily(
            "nixkube_store_filesystem_size_bytes",
            "Total size of the file system that holds /nix/store on this node",
            value=stat.f_blocks * stat.f_frsize,
        )
        # `f_bavail`, and not `f_bfree`. The reserved blocks are not available
        # to nixkube, and a dashboard reading `f_bfree` reports room on a node
        # where the next build fails.
        yield GaugeMetricFamily(
            "nixkube_store_filesystem_available_bytes",
            "Space on that file system available to this daemon",
            value=stat.f_bavail * stat.f_frsize,
        )


REGISTRY.register(StoreSpaceCollector())


# --- appstarter ---

APPSTARTER_STATE = NIX_ROOT / "nix/var/appstarter/state.json"
"""What `appstarter init` left behind, and the only record of which
environment this pod actually got. See `AppstarterCollector`."""


class AppstarterCollector:
    """Whether this pod runs what its deployment asks for, or the image's copy.

    `appstarter init` fetches the environment the deployment names, and when
    that fetch fails it seeds the store from the copy baked into the image
    instead. That is deliberate -- a node that starts behind beats a node that
    does not start -- and it is invisible from outside: the pod is Running and
    every probe passes either way. The two store paths in `state.json` are the
    only thing that says which happened.

    **Read from the file and on the scrape, not from the environment at
    import.** `appstarter run` puts both paths in the environment of the
    container it execs, and only some of these containers are started that way
    -- the pynixd builder runs its program directly. The store they share
    answers for all of them.

    **Absent rather than zero when the file cannot be read.** A process that
    cannot tell must not report "not degraded", which is the one answer that
    would hide exactly what this exists to show. Alert on `== 1`, and on
    `absent()` separately if the silence itself matters.

    **1 is a state, not an event: it cannot clear while the pod lives.**
    `appstarter init` is an `initContainer`, and a container restart does not
    re-run one -- a pod killed by its liveness probe comes back onto the same
    store and reports the same bit. Only a new pod, or a sandbox the kubelet
    recreates after a node restart, decides it again. So an alert on `== 1`
    needs no `for:` beyond a scrape or two, will not flap, and stays firing
    until somebody replaces the pod.

    The paths are deliberately not labels: this module's rule against store
    paths holds, the bit is what an alert needs, and the paths are in the
    pod's log and in `APPSTARTER_RUNNING_STORE_PATH`.
    """

    def collect(self) -> Iterator[Metric]:
        """Yield the one bit, or nothing when no state was recorded."""
        try:
            recorded = json.loads(APPSTARTER_STATE.read_text())
            wanted, running = recorded["wanted"], recorded["running"]
        except (OSError, ValueError, KeyError, TypeError):
            # Deliberately silent. `REGISTRY.register` calls `collect()` once
            # to check for a duplicate metric name, so anything written here
            # lands on stdout during import. A scrape every 15 seconds would
            # then repeat it for ever. The absent series is the signal.
            return
        yield GaugeMetricFamily(
            "nixkube_appstarter_degraded",
            "1 when this pod runs the environment baked into its image instead of the one the deployment asks for",
            value=float(wanted != running),
        )


REGISTRY.register(AppstarterCollector())


IPV6_ANY = "::"
IPV4_ANY = "0.0.0.0"


def bind_candidates(addr: str) -> tuple[str, ...]:
    """`addr`, and IPv4 after it when `addr` is the every-interface v6 form.

    A `::` socket also serves IPv4 while the kernel keeps its default
    `net.ipv6.bindv6only=0`, so one bind covers both families. A kernel with
    IPv6 switched off refuses it outright, and that node is scrapable over
    IPv4 or not at all.
    """
    return (addr, IPV4_ANY) if addr == IPV6_ANY else (addr,)


def serve(port: int, addr: str) -> None:
    """Start the metrics endpoint, and let the daemon run without it on failure.

    A node that cannot bind the metrics port still has a CSI driver to run,
    and refusing to start one because the other is unavailable would turn an
    observability gap into an outage.
    """
    for candidate in bind_candidates(addr):
        try:
            start_http_server(port, addr=candidate)
        except OSError:
            logger.warning("metrics_server_failed", port=port, addr=candidate, exc_info=True)
            continue
        logger.info("metrics_serving", port=port, addr=candidate)
        return
