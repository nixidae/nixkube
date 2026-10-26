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
"""

from __future__ import annotations

import contextlib
import copy
import hashlib
import os
from datetime import UTC, datetime
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Final

import anyio
import structlog
from kr8s import NotFoundError, ServerError
from kr8s.asyncio.objects import Job, Node, Pod, PodTemplate, new_class

if TYPE_CHECKING:
    from collections.abc import Mapping

logger = structlog.get_logger("nixkube.nixos_rollout")

GROUP: Final = "nixkube.nixidae.org"
VERSION: Final = f"{GROUP}/v1alpha1"
TEMPLATE: Final = "nixkube-nixos-host"
RESYNC_SECONDS: Final = 5
JOB_TTL_SECONDS: Final = 86400

NixOSRollout = new_class(
    "NixOSRollout", version=VERSION, namespaced=False, plural="nixosrollouts"
)
Lease = new_class("Lease", version="coordination.k8s.io/v1", namespaced=True)
ModernEvent = new_class("Event", version="events.k8s.io/v1", namespaced=True)


class Phase(StrEnum):
    PROGRESSING = "Progressing"
    COMPLETE = "Complete"
    HALTED = "Halted"


class NodePhase(StrEnum):
    PENDING = "Pending"
    CONFLICT = "Conflict"
    SWITCHING = "Switching"
    CONFIRMING = "Confirming"
    DISARMING = "Disarming"
    DONE = "Done"
    REVERTED = "Reverted"


class Action(StrEnum):
    SWITCH = "switch"
    DISARM = "disarm"
    REVERT = "revert"


IN_FLIGHT: Final = frozenset(
    {NodePhase.SWITCHING, NodePhase.CONFIRMING, NodePhase.DISARMING}
)


def selects(selector: Mapping[str, Any], labels: Mapping[str, str]) -> bool:
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


def job_name(
    rollout: str, node: str, action: Action, target: str, generation: int
) -> str:
    """One name per step, so a restarted controller finds the Job it made."""
    digest = hashlib.sha256(
        f"{rollout}\0{node}\0{target}\0{generation}".encode()
    ).hexdigest()[:16]
    return f"nixos-{action}-{digest}"


def plan(
    entries: Mapping[str, Mapping[str, Any]],
    systems: Mapping[str, str],
    selected: list[str],
    conflicts: set[str],
    generation: int,
) -> dict[str, dict[str, Any]]:
    """The node entries after a spec change, before any Job is looked at.

    A node in flight keeps its entry. A node whose target moved, or that was
    reverted at an older generation, starts again.
    """
    out: dict[str, dict[str, Any]] = {}
    for node in selected:
        target = systems.get(node)
        entry = dict(entries.get(node) or {})
        phase = entry.get("phase")
        if target is None:
            continue
        if phase in IN_FLIGHT:
            out[node] = entry
            continue
        if node in conflicts:
            out[node] = {
                "phase": NodePhase.CONFLICT,
                "target": target,
                "message": "selected by more than one NixOSRollout",
            } | ({"current": entry["current"]} if "current" in entry else {})
            continue
        if phase == NodePhase.REVERTED and entry.get("generation", 0) >= generation:
            out[node] = entry
            continue
        if phase == NodePhase.DONE and entry.get("target") == target:
            out[node] = entry
            continue
        out[node] = {
            k: v for k, v in entry.items() if k in ("current", "failedSystem")
        } | {"phase": NodePhase.PENDING, "target": target}
    # A node that left the selector mid-switch is still armed: finish it.
    for node, entry in entries.items():
        if node not in out and entry.get("phase") in IN_FLIGHT:
            out[node] = dict(entry)
    return out


def rollout_phase(entries: Mapping[str, Mapping[str, Any]]) -> Phase:
    phases = {e.get("phase") for e in entries.values()}
    if NodePhase.REVERTED in phases:
        return Phase.HALTED
    if phases <= {NodePhase.DONE}:
        return Phase.COMPLETE
    return Phase.PROGRESSING


def now() -> datetime:
    return datetime.now(UTC)


def stamp(t: datetime) -> str:
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_time(s: str) -> datetime:
    return datetime.fromisoformat(s)


def job_state(job: Any) -> str | None:
    for cond in job.raw.get("status", {}).get("conditions") or []:
        if cond.get("status") == "True" and cond.get("type") in ("Complete", "Failed"):
            return cond["type"]
    return None


def node_ready(node: Any) -> bool:
    return any(
        c.get("type") == "Ready" and c.get("status") == "True"
        for c in node.raw.get("status", {}).get("conditions") or []
    )


class Controller:
    def __init__(self, namespace: str, revert_delay: int) -> None:
        self.namespace = namespace
        self.revert_delay = revert_delay

    async def run(self) -> None:
        logger.info("started", namespace=self.namespace, revert_delay=self.revert_delay)
        while True:
            try:
                await self.reconcile_all()
            except Exception:
                logger.warning("reconcile_failed", exc_info=True)
            await anyio.sleep(RESYNC_SECONDS)

    async def reconcile_all(self) -> None:
        rollouts = [r async for r in NixOSRollout.list() if isinstance(r, NixOSRollout)]
        nodes = {n.name: n async for n in Node.list() if isinstance(n, Node)}
        selected = {
            r.name: sorted(
                name
                for name, node in nodes.items()
                if selects(r.raw["spec"].get("nodeSelector") or {}, node.labels)
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
        rollout: Any,
        nodes: Mapping[str, Any],
        selected: list[str],
        conflicts: set[str],
    ) -> None:
        spec = rollout.raw["spec"]
        status = rollout.raw.get("status") or {}
        generation = rollout.raw["metadata"]["generation"]
        systems = spec.get("systems") or {}
        confirm_timeout = spec.get("confirmTimeout", 300)
        max_unavailable = spec.get("maxUnavailable", 1)

        entries = plan(
            status.get("nodes") or {}, systems, selected, conflicts, generation
        )
        for node, entry in entries.items():
            if entry["phase"] in IN_FLIGHT:
                await self.step(rollout, node, entry, nodes.get(node), confirm_timeout)

        message = ""
        if confirm_timeout >= self.revert_delay:
            message = f"confirmTimeout {confirm_timeout} must be less than the revert delay {self.revert_delay}"
        unmanaged = sorted(set(selected) - set(systems))
        if unmanaged:
            message = (
                (message + "; " if message else "")
                + "no system for: "
                + ", ".join(unmanaged)
            )

        halted = rollout_phase(entries) == Phase.HALTED
        startable = (
            not halted
            and not spec.get("paused")
            and confirm_timeout < self.revert_delay
        )
        in_flight = sum(e["phase"] in IN_FLIGHT for e in entries.values())
        for node, entry in sorted(entries.items()):
            if not startable or in_flight >= max_unavailable:
                break
            if entry["phase"] == NodePhase.PENDING:
                await self.start_switch(rollout, node, entry, generation)
                in_flight += 1

        phase = rollout_phase(entries)
        if confirm_timeout >= self.revert_delay:
            phase = Phase.HALTED
        new_status = {
            "observedGeneration": generation,
            "phase": phase,
            "message": message,
            "nodes": entries,
        }
        if new_status != {k: status.get(k) for k in new_status}:
            # Replaced whole: a merge patch keeps every key it does not name.
            await rollout.patch(
                [{"op": "add", "path": "/status", "value": new_status}],
                subresource="status",
                type="json",
            )
            if phase != status.get("phase"):
                logger.info("rollout_phase", rollout=rollout.name, phase=phase)

    async def start_switch(
        self, rollout: Any, node: str, entry: dict[str, Any], generation: int
    ) -> None:
        name = job_name(rollout.name, node, Action.SWITCH, entry["target"], generation)
        await self.create_job(rollout, node, Action.SWITCH, name, entry["target"])
        entry.update(
            phase=NodePhase.SWITCHING,
            job=name,
            since=stamp(now()),
            generation=generation,
            message="",
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
        rollout: Any,
        node: str,
        entry: dict[str, Any],
        node_obj: Any,
        confirm_timeout: int,
    ) -> None:
        log = logger.bind(rollout=rollout.name, node=node)
        phase = NodePhase(entry["phase"])
        if phase == NodePhase.CONFIRMING:
            lease = await Lease.get(node, namespace="kube-node-lease")
            renewed = parse_time(lease.raw["spec"]["renewTime"]) > parse_time(
                entry["leaseRenewTime"]
            )
            if renewed and node_obj is not None and node_ready(node_obj):
                name = job_name(
                    rollout.name,
                    node,
                    Action.DISARM,
                    entry["target"],
                    entry["generation"],
                )
                await self.create_job(
                    rollout, node, Action.DISARM, name, entry["target"]
                )
                entry.update(phase=NodePhase.DISARMING, job=name)
                log.info("switch_confirmed")
            elif (now() - parse_time(entry["since"])).total_seconds() > confirm_timeout:
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
                await self.create_job(
                    rollout, node, Action.REVERT, name, entry["target"], deadline=60
                )
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
                    phase=NodePhase.CONFIRMING,
                    since=stamp(now()),
                    leaseRenewTime=lease.raw["spec"]["renewTime"],
                )
                log.info("switch_done", job=entry["job"])
            case "Complete", NodePhase.DISARMING:
                entry.update(phase=NodePhase.DONE, current=entry["target"], message="")
                entry.pop("leaseRenewTime", None)
                log.info("node_done", system=entry["target"])
            case "Failed", _:
                tail = await self.job_log(entry["job"])
                await self.halt(
                    rollout, node, entry, f"Job {entry['job']} failed: {tail}"
                )

    async def halt(
        self, rollout: Any, node: str, entry: dict[str, Any], message: str
    ) -> None:
        entry.update(
            phase=NodePhase.REVERTED, failedSystem=entry["target"], message=message
        )
        entry.pop("leaseRenewTime", None)
        logger.warning(
            "node_reverted", rollout=rollout.name, node=node, message=message
        )
        with contextlib.suppress(NotFoundError):
            await (await Node.get(node)).cordon()
        await self.event(rollout, "Reverted", f"{node}: {message}")

    async def create_job(
        self,
        rollout: Any,
        node: str,
        action: Action,
        name: str,
        target: str,
        deadline: int | None = None,
    ) -> None:
        template = await PodTemplate.get(TEMPLATE, namespace=self.namespace)
        pod = copy.deepcopy(template.raw["template"])
        pod["spec"]["nodeName"] = node
        for container in pod["spec"]["containers"]:
            for env in container.get("env") or []:
                if env["name"] == "ACTION":
                    env["value"] = str(action)
                elif env["name"] == "TOPLEVEL":
                    env["value"] = target
        labels = {
            f"{GROUP}/rollout": rollout.name,
            f"{GROUP}/node": node,
            f"{GROUP}/action": str(action),
        }
        pod.setdefault("metadata", {}).setdefault("labels", {}).update(labels)
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
                            "uid": rollout.raw["metadata"]["uid"],
                        }
                    ],
                },
                "spec": {
                    "backoffLimit": 0,
                    "ttlSecondsAfterFinished": JOB_TTL_SECONDS,
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

    async def job_log(self, job: str) -> str:
        try:
            async for pod in Pod.list(
                namespace=self.namespace, label_selector={"job-name": job}
            ):
                assert isinstance(pod, Pod)
                lines = [line async for line in pod.logs(tail_lines=5)]
                return " | ".join(lines)[-500:]
        except Exception:
            logger.warning("job_log_failed", job=job, exc_info=True)
        return "no log"

    async def event(self, rollout: Any, reason: str, note: str) -> None:
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
                        "uid": rollout.raw["metadata"]["uid"],
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
    )
    anyio.run(controller.run, backend="asyncio")
