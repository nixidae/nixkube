# SPDX-License-Identifier: Apache-2.0

"""The NixOSRollout controller (#359).

One level-triggered loop: each pass reads every NixOSRollout, the Nodes and
the Jobs it made, and moves each node one step. All state is in the
rollout's status and in Jobs with deterministic names, so a restarted
controller resumes where the last one stopped.

A node goes Pending, Switching (the switch Job runs), Confirming (wait for a
kubelet Lease renewal and Ready), Disarming (the disarm Job runs), Done. A
switch that fails or is not confirmed in time is Reverted: the node is
cordoned and the rollout halts until its spec changes.

A switch Job that finds a new kernel, initrd or kernel modules changes
nothing and exits with REBOOT_NEEDED. The node is then cordoned and drained
(Draining; DrainFailed halts on the timeout), the boot Job makes the system
the boot default and schedules a reboot (Booting), and the controller waits
for a new boot ID and Ready (Rebooting). The disarm Job then checks the
running system, and the node is uncordoned.

A rollout with a `source` first builds each node's system (Building). No
node switches until every build is done, so a bad source halts the rollout
(BuildFailed) before it touches a host. Builds live in memory, keyed by
generation: a restarted controller builds again, and pynixd answers from
its store.
"""

from __future__ import annotations

import base64
import contextlib
import copy
import hashlib
import json
import os
import shlex
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Final

import anyio
import structlog
from kr8s import ALL, NotFoundError, ServerError
from kr8s.asyncio.objects import Job, Node, Pod, PodTemplate, Secret, new_class

from .kube_docs import (
    job_doc,
    lease_doc,
    node_doc,
    pod_doc,
    pod_template_doc,
    rollout_doc,
    secret_doc,
)

if TYPE_CHECKING:
    from collections.abc import Mapping

    from anyio.abc import TaskGroup
    from kr8s.asyncio.objects import APIObject
    from structlog.typing import FilteringBoundLogger

    from .kube_docs import (
        LabelSelector,
        NodeEntry,
        PodDoc,
        RolloutSource,
        RolloutStatus,
    )

logger: FilteringBoundLogger = structlog.get_logger("nixkube.nixos_rollout")

GROUP: Final = "nixkube.nixidae.org"
VERSION: Final = f"{GROUP}/v1alpha1"
TEMPLATE: Final = "nixkube-nixos-host"

NixOSRollout = new_class("NixOSRollout", version=VERSION, namespaced=False, plural="nixosrollouts")
Lease = new_class("Lease", version="coordination.k8s.io/v1", namespaced=True)
ModernEvent = new_class("Event", version="events.k8s.io/v1", namespaced=True)


class Phase(StrEnum):
    PROGRESSING = "Progressing"
    COMPLETE = "Complete"
    HALTED = "Halted"


class NodePhase(StrEnum):
    BUILDING = "Building"
    BUILD_FAILED = "BuildFailed"
    PENDING = "Pending"
    CONFLICT = "Conflict"
    SWITCHING = "Switching"
    CONFIRMING = "Confirming"
    DRAINING = "Draining"
    DRAIN_FAILED = "DrainFailed"
    BOOTING = "Booting"
    REBOOTING = "Rebooting"
    DISARMING = "Disarming"
    DONE = "Done"
    REVERTED = "Reverted"


class Action(StrEnum):
    SWITCH = "switch"
    BOOT = "boot"
    DISARM = "disarm"
    REVERT = "revert"


IN_FLIGHT: Final = frozenset(
    {
        NodePhase.SWITCHING,
        NodePhase.CONFIRMING,
        NodePhase.DRAINING,
        NodePhase.BOOTING,
        NodePhase.REBOOTING,
        NodePhase.DISARMING,
    }
)
HALTING: Final = frozenset({NodePhase.REVERTED, NodePhase.BUILD_FAILED, NodePhase.DRAIN_FAILED})

# The switch Job's exit code when only a boot picks the change up, as
# `rebootNeeded` in kubenix/nixos-host.nix.
REBOOT_NEEDED: Final = 100
MIRROR: Final = "kubernetes.io/config.mirror"


def _kept(entry: NodeEntry) -> NodeEntry:
    """What a node's entry keeps when it starts again. `drained`: the controller
    cordoned the node, so the controller uncordons it."""
    kept: NodeEntry = {}
    if "current" in entry:
        kept["current"] = entry["current"]
    if "failedSystem" in entry:
        kept["failedSystem"] = entry["failedSystem"]
    if "drained" in entry:
        kept["drained"] = entry["drained"]
    return kept


def selects(selector: LabelSelector, labels: Mapping[str, str]) -> bool:
    """Whether a Kubernetes LabelSelector matches `labels`. An empty one matches all."""
    for key, value in (selector.get("matchLabels") or {}).items():
        if labels.get(key) != value:
            return False
    for expr in selector.get("matchExpressions") or []:
        key, op, values = expr["key"], expr["operator"], expr.get("values") or []
        match op:
            case "In":
                ok = key in labels and labels[key] in values
            case "NotIn":
                ok = key not in labels or labels[key] not in values
            case "Exists":
                ok = key in labels
            case "DoesNotExist":
                ok = key not in labels
            case _:
                ok = False
        if not ok:
            return False
    return True


def job_name(rollout: str, node: str, action: Action, target: str, generation: int) -> str:
    """One name per step, so a restarted controller finds the Job it made."""
    digest = hashlib.sha256(f"{rollout}\0{node}\0{target}\0{generation}".encode()).hexdigest()[:16]
    return f"nixos-{action}-{digest}"


def plan(
    entries: Mapping[str, NodeEntry],
    systems: Mapping[str, str],
    selected: list[str],
    conflicts: set[str],
    generation: int,
) -> dict[str, NodeEntry]:
    """The node entries after a spec change, before any Job is looked at.

    A node in flight keeps its entry. A node whose target moved, or that was
    reverted at an older generation, starts again.
    """
    out: dict[str, NodeEntry] = {}
    for node in selected:
        target = systems.get(node)
        entry: NodeEntry = copy.copy(entries[node]) if node in entries else {}
        phase = entry.get("phase")
        if target is None:
            continue
        if phase in IN_FLIGHT:
            out[node] = entry
            continue
        if node in conflicts:
            conflict: NodeEntry = {
                "phase": NodePhase.CONFLICT,
                "target": target,
                "message": "selected by more than one NixOSRollout",
            }
            if "current" in entry:
                conflict["current"] = entry["current"]
            out[node] = conflict
            continue
        if phase in HALTING - {NodePhase.BUILD_FAILED} and entry.get("generation", 0) >= generation:
            out[node] = entry
            continue
        if phase == NodePhase.DONE and entry.get("target") == target:
            out[node] = entry
            continue
        pending = _kept(entry)
        pending.update({"phase": NodePhase.PENDING, "target": target})
        out[node] = pending
    # A node that left the selector mid-switch is still armed: finish it.
    for node, entry in entries.items():
        if node not in out and entry.get("phase") in IN_FLIGHT:
            out[node] = copy.copy(entry)
    return out


def rollout_phase(entries: Mapping[str, NodeEntry]) -> Phase:
    phases = {e.get("phase") for e in entries.values()}
    if phases & HALTING:
        return Phase.HALTED
    if phases <= {NodePhase.DONE}:
        return Phase.COMPLETE
    return Phase.PROGRESSING


ARCH_SYSTEMS: Final = {"amd64": "x86_64-linux", "arm64": "aarch64-linux"}

# Every value arrives with --argstr, so nothing from the CR is spliced into
# Nix source. `allRefs`: a rev on any branch, not only the default one.
BUILD_EXPR: Final = r"""
{ url, rev, attribute, name, labels, system }:
let
  src = builtins.fetchGit { inherit url rev; allRefs = true; };
  path = builtins.filter builtins.isString (builtins.split "\\." attribute);
  f = builtins.foldl' (v: k: v.${k}) (import src) (if attribute == "" then [ ] else path);
in
(f { inherit name system; labels = builtins.fromJSON labels; }).config.system.build.toplevel
"""


def node_system(labels: Mapping[str, str]) -> str:
    arch = labels.get("kubernetes.io/arch", "amd64")
    return ARCH_SYSTEMS.get(arch, f"{arch}-linux")


def build_argv(source: RolloutSource, node: str, labels: Mapping[str, str], store: str) -> list[str]:
    """Evaluate in the pod's own store, build in pynixd's."""
    args = {
        "url": source["url"],
        "rev": source["rev"],
        "attribute": source.get("attribute", ""),
        "name": node,
        "labels": json.dumps(dict(labels), sort_keys=True),
        "system": node_system(labels),
    }
    argv = [
        "nix",
        "build",
        "--extra-experimental-features",
        "nix-command",
        "--no-link",
        "--print-out-paths",
        # `--expr` evaluates in pure mode, where a source cannot name a
        # store path or read `builtins.currentSystem`.
        "--impure",
        "--eval-store",
        "auto",
        "--store",
        store,
        "--expr",
        BUILD_EXPR,
    ]
    for key, value in args.items():
        argv += ["--argstr", key, value]
    return argv


def git_env(kind: str, data: Mapping[str, bytes], directory: Path) -> dict[str, str]:
    """The environment that makes git, run by Nix's fetcher, use a Secret."""
    match kind:
        case "kubernetes.io/ssh-auth":
            if "known_hosts" not in data:
                raise ValueError("an ssh-auth Secret needs a known_hosts key")
            key, known = directory / "key", directory / "known_hosts"
            key.write_bytes(data["ssh-privatekey"])
            key.chmod(0o600)
            known.write_bytes(data["known_hosts"])
            ssh = [
                "ssh",
                "-i",
                str(key),
                "-o",
                "IdentitiesOnly=yes",
                "-o",
                f"UserKnownHostsFile={known}",
                "-o",
                "StrictHostKeyChecking=yes",
            ]
            return {"GIT_SSH_COMMAND": shlex.join(ssh)}
        case "kubernetes.io/basic-auth":
            token = base64.b64encode(data["username"] + b":" + data["password"])
            return {
                "GIT_CONFIG_COUNT": "1",
                "GIT_CONFIG_KEY_0": "http.extraHeader",
                "GIT_CONFIG_VALUE_0": f"Authorization: Basic {token.decode()}",
            }
        case _:
            raise ValueError(f"unsupported Secret type {kind!r}")


@dataclass
class Build:
    """One evaluation and build. Neither field is set while it runs."""

    toplevel: str | None = None
    error: str | None = None


def build_entries(
    entries: Mapping[str, NodeEntry],
    old_nodes: Mapping[str, NodeEntry],
    open_builds: Mapping[str, Build],
    generation: int,
) -> dict[str, NodeEntry]:
    """The entries of nodes whose build is running or failed, beside `plan`'s."""
    out: dict[str, NodeEntry] = {}
    for node, build in open_builds.items():
        if node in entries:
            continue
        old: NodeEntry = old_nodes.get(node) or {}
        # A restarted controller builds again, and until the build returns, a
        # node settled at this generation must stay as it was: a Reverted node
        # that turned Building would be switched again, cordoned or not.
        if old.get("phase") in (NodePhase.DONE, NodePhase.REVERTED) and old.get("generation", 0) >= generation:
            out[node] = copy.copy(old)
            continue
        entry = _kept(old)
        if build.error:
            entry.update({"phase": NodePhase.BUILD_FAILED, "message": build.error})
        else:
            entry["phase"] = NodePhase.BUILDING
        out[node] = entry
    return out


@dataclass(frozen=True)
class Timeouts:
    confirm: int
    drain: int
    reboot: int


def now() -> datetime:
    return datetime.now(UTC)


def stamp(t: datetime) -> str:
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_time(s: str) -> datetime:
    return datetime.fromisoformat(s)


def job_state(job: Job) -> str | None:
    for cond in job_doc(job).get("status", {}).get("conditions") or []:
        if cond.get("status") == "True" and cond.get("type") in ("Complete", "Failed"):
            return cond["type"]
    return None


def node_ready(node: Node) -> bool:
    return any(
        c.get("type") == "Ready" and c.get("status") == "True"
        for c in node_doc(node).get("status", {}).get("conditions") or []
    )


def boot_id(node: Node) -> str:
    return node_doc(node).get("status", {}).get("nodeInfo", {}).get("bootID", "")


def exit_code(pod: PodDoc) -> int | None:
    """The switch container's exit code, once it has terminated."""
    for status in pod.get("status", {}).get("containerStatuses") or []:
        if terminated := (status.get("state") or {}).get("terminated"):
            return terminated.get("exitCode")
    return None


def evictable(pod: PodDoc) -> bool:
    """Whether a drain evicts `pod`, as `kubectl drain` decides.

    Not a finished pod, a static pod's mirror or a DaemonSet's pod, which
    would only come back. Not this controller's own Jobs, which the node
    needs to boot.
    """
    meta = pod.get("metadata", {})
    return not (
        pod.get("status", {}).get("phase") in ("Succeeded", "Failed")
        or MIRROR in (meta.get("annotations") or {})
        or any(o.get("kind") == "DaemonSet" for o in meta.get("ownerReferences") or [])
        or f"{GROUP}/rollout" in (meta.get("labels") or {})
    )


class Controller:
    def __init__(
        self,
        namespace: str,
        revert_delay: int,
        resync: float,
        store: str,
        job_ttl: int,
    ) -> None:
        self.namespace = namespace
        self.job_ttl = job_ttl
        self.revert_delay = revert_delay
        self.resync = resync
        self.store = store
        self.builds: dict[tuple[str, int, str], Build] = {}
        # One at a time: evaluating a NixOS system takes about 850 MB.
        self.build_limit = anyio.CapacityLimiter(1)
        self.tasks: TaskGroup | None = None

    async def run(self) -> None:
        logger.info("started", namespace=self.namespace, revert_delay=self.revert_delay)
        async with anyio.create_task_group() as tasks:
            self.tasks = tasks
            while True:
                try:
                    await self.reconcile_all()
                except Exception:
                    logger.warning("reconcile_failed", exc_info=True)
                await anyio.sleep(self.resync)

    def systems_from_source(
        self,
        rollout: APIObject,
        generation: int,
        selected: list[str],
        nodes: Mapping[str, Node],
    ) -> tuple[dict[str, str], dict[str, Build]]:
        """The toplevels built so far, and the builds still running or failed."""
        uid = rollout_doc(rollout)["metadata"]["uid"]
        for key in [k for k in self.builds if k[0] == uid and k[1] < generation]:
            del self.builds[key]
        built: dict[str, str] = {}
        open_: dict[str, Build] = {}
        for node in selected:
            key = (uid, generation, node)
            build = self.builds.get(key)
            if build is None:
                build = self.builds[key] = Build()
                if self.tasks is None:
                    raise RuntimeError("systems_from_source needs the task group that run() creates")
                self.tasks.start_soon(
                    self.build,
                    build,
                    rollout.name,
                    rollout_doc(rollout)["spec"]["source"],
                    node,
                    dict(nodes[node].labels),
                )
            if build.toplevel:
                built[node] = build.toplevel
            else:
                open_[node] = build
        return built, open_

    async def build(
        self,
        build: Build,
        rollout: str,
        source: RolloutSource,
        node: str,
        labels: Mapping[str, str],
    ) -> None:
        log = logger.bind(rollout=rollout, node=node, rev=source["rev"])
        async with self.build_limit:
            log.info("build_started")
            try:
                with tempfile.TemporaryDirectory() as tmp:
                    env = dict(os.environ)
                    if ref := source.get("secretRef"):
                        secret = await Secret.get(ref["name"], namespace=self.namespace)
                        doc = secret_doc(secret)
                        data = {k: base64.b64decode(v) for k, v in (doc.get("data") or {}).items()}
                        env |= git_env(doc.get("type", ""), data, Path(tmp))
                    result = await anyio.run_process(
                        build_argv(source, node, labels, self.store),
                        env=env,
                        check=False,
                    )
                out = result.stdout.decode().split()
                if result.returncode != 0 or not out:
                    build.error = result.stderr.decode()[-1000:] or "no output"
                    log.warning("build_failed", returncode=result.returncode)
                    return
                build.toplevel = out[-1]
                log.info("build_done", toplevel=build.toplevel)
            # This runs in the controller's task group: an exception that
            # escaped would cancel the reconcile loop.
            except Exception as e:
                build.error = f"{type(e).__name__}: {e}"
                log.warning("build_failed", exc_info=True)

    async def reconcile_all(self) -> None:
        rollouts = [r async for r in NixOSRollout.list() if isinstance(r, NixOSRollout)]
        nodes = {n.name: n async for n in Node.list() if isinstance(n, Node)}
        selected = {
            r.name: sorted(
                name
                for name, node in nodes.items()
                if selects(rollout_doc(r)["spec"].get("nodeSelector") or {}, node.labels)
            )
            for r in rollouts
        }
        owners: dict[str, int] = {}
        for names in selected.values():
            for name in names:
                owners[name] = owners.get(name, 0) + 1
        conflicts = {name for name, n in owners.items() if n > 1}
        for rollout in rollouts:
            log = logger.bind(rollout=rollout.name)
            try:
                await self.reconcile(rollout, nodes, selected[rollout.name], conflicts)
            except Exception:
                log.warning("rollout_reconcile_failed", exc_info=True)

    async def reconcile(
        self,
        rollout: APIObject,
        nodes: Mapping[str, Node],
        selected: list[str],
        conflicts: set[str],
    ) -> None:
        doc = rollout_doc(rollout)
        spec = doc["spec"]
        status: RolloutStatus = doc.get("status") or {}
        generation = doc["metadata"]["generation"]
        timeouts = Timeouts(
            confirm=spec.get("confirmTimeout", 300),
            drain=spec.get("drainTimeout", 300),
            reboot=spec.get("rebootTimeout", 900),
        )
        confirm_timeout = timeouts.confirm
        max_unavailable = spec.get("maxUnavailable", 1)
        fetch_from = self.store if "source" in spec else ""
        open_builds: dict[str, Build] = {}
        systems: dict[str, str]
        if "source" in spec:
            systems, open_builds = self.systems_from_source(rollout, generation, selected, nodes)
        else:
            systems = spec.get("systems") or {}

        entries = plan(status.get("nodes") or {}, systems, selected, conflicts, generation)
        entries |= build_entries(entries, status.get("nodes") or {}, open_builds, generation)
        for node, entry in entries.items():
            if entry["phase"] in IN_FLIGHT:
                await self.step(rollout, node, entry, nodes.get(node), timeouts, fetch_from)

        message = ""
        if confirm_timeout >= self.revert_delay:
            message = f"confirmTimeout {confirm_timeout} must be less than the revert delay {self.revert_delay}"
        unmanaged = sorted(set(selected) - set(systems) - set(open_builds))
        if unmanaged:
            message = (message + "; " if message else "") + "no system for: " + ", ".join(unmanaged)

        halted = rollout_phase(entries) == Phase.HALTED
        startable = not halted and not spec.get("paused") and confirm_timeout < self.revert_delay and not open_builds
        in_flight = sum(e["phase"] in IN_FLIGHT for e in entries.values())
        for node in sorted(entries):
            entry = entries[node]
            if not startable or in_flight >= max_unavailable:
                break
            if entry["phase"] == NodePhase.PENDING:
                await self.start_switch(rollout, node, entry, generation, fetch_from)
                in_flight += 1

        phase = rollout_phase(entries)
        if confirm_timeout >= self.revert_delay:
            phase = Phase.HALTED
        new_status: RolloutStatus = {
            "observedGeneration": generation,
            "phase": phase,
            "message": message,
            "nodes": entries,
        }
        stored = (
            status.get("observedGeneration"),
            status.get("phase"),
            status.get("message"),
            status.get("nodes"),
        )
        if stored != (generation, phase, message, entries):
            # Replaced whole: a merge patch keeps every key it does not name.
            await rollout.patch(
                [{"op": "add", "path": "/status", "value": new_status}],
                subresource="status",
                type="json",
            )
            if phase != status.get("phase"):
                logger.info("rollout_phase", rollout=rollout.name, phase=phase)

    async def start_switch(
        self,
        rollout: APIObject,
        node: str,
        entry: NodeEntry,
        generation: int,
        fetch_from: str,
    ) -> None:
        name = job_name(rollout.name, node, Action.SWITCH, entry["target"], generation)
        await self.create_job(rollout, node, Action.SWITCH, name, entry["target"], fetch_from=fetch_from)
        entry.update(
            {
                "phase": NodePhase.SWITCHING,
                "job": name,
                "since": stamp(now()),
                "generation": generation,
                "message": "",
            }
        )
        logger.info(
            "switch_started",
            rollout=rollout.name,
            node=node,
            target=entry["target"],
            job=name,
        )

    async def step(
        self,
        rollout: APIObject,
        node: str,
        entry: NodeEntry,
        node_obj: Node | None,
        timeouts: Timeouts,
        fetch_from: str,
    ) -> None:
        log = logger.bind(rollout=rollout.name, node=node)
        phase = NodePhase(entry["phase"])
        confirm_timeout = timeouts.confirm
        elapsed = (now() - parse_time(entry["since"])).total_seconds()
        if phase == NodePhase.DRAINING:
            remaining = await self.drain(node, log)
            if not remaining:
                name = job_name(
                    rollout.name,
                    node,
                    Action.BOOT,
                    entry["target"],
                    entry["generation"],
                )
                await self.create_job(
                    rollout,
                    node,
                    Action.BOOT,
                    name,
                    entry["target"],
                    fetch_from=fetch_from,
                )
                entry.update({"phase": NodePhase.BOOTING, "job": name, "message": ""})
                log.info("drained")
            elif elapsed > timeouts.drain:
                entry.update(
                    {
                        "phase": NodePhase.DRAIN_FAILED,
                        "message": f"not drained within {timeouts.drain}s: " + ", ".join(remaining),
                    }
                )
                log.warning("drain_failed", remaining=remaining)
                await self.event(rollout, "DrainFailed", f"{node}: {entry['message']}")
            return
        if phase == NodePhase.REBOOTING:
            current = boot_id(node_obj) if node_obj is not None else ""
            if node_obj is not None and current and current != entry["bootID"] and node_ready(node_obj):
                name = job_name(
                    rollout.name,
                    node,
                    Action.DISARM,
                    entry["target"],
                    entry["generation"],
                )
                await self.create_job(rollout, node, Action.DISARM, name, entry["target"])
                entry.update({"phase": NodePhase.DISARMING, "job": name})
                log.info("rebooted", boot_id=current)
            elif elapsed > timeouts.reboot:
                await self.halt(
                    rollout,
                    node,
                    entry,
                    f"not back within {timeouts.reboot}s of a reboot",
                )
            return
        if phase == NodePhase.CONFIRMING:
            lease = await Lease.get(node, namespace="kube-node-lease")
            renewed = parse_time(lease_doc(lease)["spec"]["renewTime"]) > parse_time(entry["leaseRenewTime"])
            if renewed and node_obj is not None and node_ready(node_obj):
                name = job_name(
                    rollout.name,
                    node,
                    Action.DISARM,
                    entry["target"],
                    entry["generation"],
                )
                await self.create_job(rollout, node, Action.DISARM, name, entry["target"])
                entry.update({"phase": NodePhase.DISARMING, "job": name})
                log.info("switch_confirmed")
            elif elapsed > confirm_timeout:
                name = job_name(
                    rollout.name,
                    node,
                    Action.REVERT,
                    entry["target"],
                    entry["generation"],
                )
                # Best effort: with the kubelet down this pod never runs,
                # and the armed timer reverts the host instead. The deadline
                # keeps a late pod from reverting some later switch.
                await self.create_job(rollout, node, Action.REVERT, name, entry["target"], deadline=60)
                await self.halt(
                    rollout,
                    node,
                    entry,
                    f"not confirmed within {confirm_timeout}s; reverting",
                )
            return

        try:
            job = await Job.get(entry["job"], namespace=self.namespace)
        except NotFoundError:
            await self.halt(rollout, node, entry, f"Job {entry['job']} is gone")
            return
        match job_state(job), phase:
            case None, _:
                return
            case "Complete", NodePhase.SWITCHING:
                # The kubelet's clock, not ours: a renewal later than this
                # one came from a kubelet that ran after the switch.
                lease = await Lease.get(node, namespace="kube-node-lease")
                entry.update(
                    {
                        "phase": NodePhase.CONFIRMING,
                        "since": stamp(now()),
                        "leaseRenewTime": lease_doc(lease)["spec"]["renewTime"],
                    }
                )
                log.info("switch_done", job=entry["job"])
            case "Complete", NodePhase.BOOTING:
                entry.update({"phase": NodePhase.REBOOTING, "since": stamp(now())})
                log.info("reboot_scheduled", job=entry["job"])
            case "Complete", NodePhase.DISARMING:
                entry.update({"phase": NodePhase.DONE, "current": entry["target"], "message": ""})
                entry.pop("leaseRenewTime", None)
                entry.pop("bootID", None)
                if entry.pop("drained", False):
                    with contextlib.suppress(NotFoundError):
                        await (await Node.get(node)).uncordon()
                log.info("node_done", system=entry["target"])
            case "Failed", NodePhase.SWITCHING if await self.job_exit_code(entry["job"]) == REBOOT_NEEDED:
                with contextlib.suppress(NotFoundError):
                    await (await Node.get(node)).cordon()
                entry.update(
                    {
                        "phase": NodePhase.DRAINING,
                        "since": stamp(now()),
                        "bootID": boot_id(node_obj) if node_obj is not None else "",
                        "drained": True,
                        "message": "the kernel, initrd or modules change: draining to reboot",
                    }
                )
                log.info("reboot_needed")
            case "Failed", _:
                tail = await self.job_log(entry["job"])
                await self.halt(rollout, node, entry, f"Job {entry['job']} failed: {tail}")

    async def halt(self, rollout: APIObject, node: str, entry: NodeEntry, message: str) -> None:
        entry.update({"phase": NodePhase.REVERTED, "failedSystem": entry["target"], "message": message})
        entry.pop("leaseRenewTime", None)
        logger.warning("node_reverted", rollout=rollout.name, node=node, message=message)
        with contextlib.suppress(NotFoundError):
            await (await Node.get(node)).cordon()
        await self.event(rollout, "Reverted", f"{node}: {message}")

    async def create_job(
        self,
        rollout: APIObject,
        node: str,
        action: Action,
        name: str,
        target: str,
        deadline: int | None = None,
        fetch_from: str = "",
    ) -> None:
        template = await PodTemplate.get(TEMPLATE, namespace=self.namespace)
        pod = copy.deepcopy(pod_template_doc(template)["template"])
        pod["spec"]["nodeName"] = node
        values = {"ACTION": str(action), "TOPLEVEL": target, "FETCH_FROM": fetch_from}
        for container in pod["spec"].get("initContainers", []) + pod["spec"]["containers"]:
            for env in container.get("env") or []:
                if env["name"] in values:
                    env["value"] = values[env["name"]]
        labels = {
            f"{GROUP}/rollout": rollout.name,
            f"{GROUP}/node": node,
            f"{GROUP}/action": str(action),
        }
        if "metadata" not in pod:
            pod["metadata"] = {}
        if "labels" not in pod["metadata"]:
            pod["metadata"]["labels"] = {}
        pod["metadata"]["labels"].update(labels)
        job = await Job(
            {
                "apiVersion": "batch/v1",
                "kind": "Job",
                "metadata": {
                    "name": name,
                    "namespace": self.namespace,
                    "labels": labels,
                    # A cluster-scoped owner may own a namespaced object.
                    "ownerReferences": [
                        {
                            "apiVersion": VERSION,
                            "kind": "NixOSRollout",
                            "name": rollout.name,
                            "uid": rollout_doc(rollout)["metadata"]["uid"],
                        }
                    ],
                },
                "spec": {
                    # A retry would switch the host a second time.
                    "backoffLimit": 0,
                    "ttlSecondsAfterFinished": self.job_ttl,
                    "template": pod,
                }
                | ({"activeDeadlineSeconds": deadline} if deadline else {}),
            }
        )
        try:
            await job.create()
        except ServerError as e:
            if e.response is None or e.response.status_code != 409:
                raise

    async def job_exit_code(self, job: str) -> int | None:
        async for pod in Pod.list(namespace=self.namespace, label_selector={"job-name": job}):
            if not isinstance(pod, Pod):
                raise RuntimeError(f"Pod.list yielded {type(pod).__name__}, not a Pod")
            return exit_code(pod_doc(pod))
        return None

    async def drain(self, node: str, log: FilteringBoundLogger) -> list[str]:
        """Evict what `evictable` allows from `node`; returns what is still there.

        Through the eviction API, so a PodDisruptionBudget can refuse (429)
        until the next pass. A pod that is terminating counts until it is gone.
        """
        remaining: list[str] = []
        async for pod in Pod.list(namespace=ALL, field_selector={"spec.nodeName": node}):
            if not isinstance(pod, Pod):
                raise RuntimeError(f"Pod.list yielded {type(pod).__name__}, not a Pod")
            doc = pod_doc(pod)
            if not evictable(doc):
                continue
            remaining.append(f"{pod.namespace}/{pod.name}")
            if doc["metadata"].get("deletionTimestamp"):
                continue
            if pod.api is None:
                raise RuntimeError(f"Pod {pod.namespace}/{pod.name} has no API client")
            async with pod.api.call_api(
                "POST",
                version="v1",
                url=f"pods/{pod.name}/eviction",
                namespace=pod.namespace,
                content=json.dumps(
                    {
                        "apiVersion": "policy/v1",
                        "kind": "Eviction",
                        "metadata": {"name": pod.name, "namespace": pod.namespace},
                    }
                ),
                raise_for_status=False,
            ) as resp:
                if resp.status_code >= 400 and resp.status_code != 404:
                    log.info(
                        "eviction_refused",
                        pod=f"{pod.namespace}/{pod.name}",
                        status=resp.status_code,
                    )
        return remaining

    async def job_log(self, job: str) -> str:
        try:
            async for pod in Pod.list(namespace=self.namespace, label_selector={"job-name": job}):
                if not isinstance(pod, Pod):
                    raise RuntimeError(f"Pod.list yielded {type(pod).__name__}, not a Pod")
                lines = [line async for line in pod.logs(tail_lines=5)]
                return " | ".join(lines)[-500:]
        except Exception:
            logger.warning("job_log_failed", job=job, exc_info=True)
        return "no log"

    async def event(self, rollout: APIObject, reason: str, note: str) -> None:
        t = now()
        try:
            event = await ModernEvent(
                {
                    "metadata": {
                        "generateName": "nixosrollout-",
                        "namespace": "default",
                    },
                    "eventTime": t.strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
                    "reportingController": f"{GROUP}/nixos-rollout",
                    "reportingInstance": os.environ.get("HOSTNAME", "nixos-rollout"),
                    "action": "Rollout",
                    "reason": reason,
                    "type": "Warning",
                    "note": note[:1000],
                    "regarding": {
                        "apiVersion": VERSION,
                        "kind": "NixOSRollout",
                        "name": rollout.name,
                        "uid": rollout_doc(rollout)["metadata"]["uid"],
                    },
                }
            )
            await event.create()
        except Exception:
            logger.warning("event_failed", exc_info=True)


def main() -> None:
    controller = Controller(
        namespace=os.environ["NAMESPACE"],
        revert_delay=int(os.environ["REVERT_DELAY"]),
        resync=float(os.environ["RESYNC_SECONDS"]),
        store=os.environ.get("PYNIXD_STORE", "ssh-ng://nix@pynixd"),
        job_ttl=int(os.environ["JOB_TTL_SECONDS"]),
    )
    anyio.run(controller.run, backend="asyncio")
