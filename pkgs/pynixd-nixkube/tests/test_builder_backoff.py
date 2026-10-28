# SPDX-License-Identifier: Apache-2.0

"""The consecutive-failure backoff and the startup watchdog, without a cluster.

The arithmetic and the state transitions are pure, so they are testable on
their own. Everything that talks to the API server is not tested here.
"""

import time
from datetime import UTC, datetime

import anyio
import pytest

from pynixd_nixkube import builder_manager
from pynixd_nixkube.builder_manager import (
    _BACKOFF_BASE_SECONDS,
    BUILDER_KNOWN_HOSTS,
    BuilderManager,
    PodState,
    _job_age_seconds,
    _pod_is_ready,
)

SYSTEM = "x86_64-linux"


def manager(**kwargs) -> BuilderManager:
    return BuilderManager(server=None, namespace="nixkube", **kwargs)  # type: ignore[arg-type]


def test_no_failures_means_no_delay():
    m = manager()
    assert m._backoff_delay(0) == 0.0
    assert m._backoff_remaining(SYSTEM) == 0.0


def test_delay_doubles_and_stops_at_the_cap():
    m = manager(backoff_cap=600.0)
    assert m._backoff_delay(1) == _BACKOFF_BASE_SECONDS
    assert m._backoff_delay(2) == _BACKOFF_BASE_SECONDS * 2
    assert m._backoff_delay(3) == _BACKOFF_BASE_SECONDS * 4
    assert m._backoff_delay(5) == _BACKOFF_BASE_SECONDS * 16
    assert m._backoff_delay(6) == 600.0
    assert m._backoff_delay(50) == 600.0


def test_each_failure_grows_the_delay():
    m = manager()
    for expected in (1, 2, 3):
        m._record_failure(SYSTEM, f"job-{expected}", reason="JobFailed")
        assert m._failures[SYSTEM] == expected
    # The third failure holds the system back for four base intervals.
    assert m._backoff_remaining(SYSTEM) == pytest.approx(_BACKOFF_BASE_SECONDS * 4, abs=1.0)


def test_one_job_counts_once():
    """A failed Job is seen on every reconcile until its TTL reaps it."""
    m = manager()
    for _ in range(5):
        m._record_failure(SYSTEM, "job-a", reason="JobFailed")
    assert m._failures[SYSTEM] == 1


def test_a_forgotten_job_may_count_again():
    m = manager()
    m._record_failure(SYSTEM, "job-a", reason="JobFailed")
    m._forget_job("job-a")
    m._record_failure(SYSTEM, "job-a", reason="JobFailed")
    assert m._failures[SYSTEM] == 2


def test_ready_clears_the_backoff():
    m = manager()
    m._record_failure(SYSTEM, "job-a", reason="JobFailed")
    m._record_failure(SYSTEM, "job-b", reason="JobFailed")
    m._record_ready(SYSTEM, "job-c")
    assert SYSTEM not in m._failures
    assert m._backoff_remaining(SYSTEM) == 0.0
    # And the next failure starts again at the first step, not where it left off.
    m._record_failure(SYSTEM, "job-d", reason="JobFailed")
    assert m._failures[SYSTEM] == 1


def test_systems_are_independent():
    m = manager()
    m._record_failure("aarch64-linux", "job-a", reason="JobFailed")
    assert m._backoff_remaining("aarch64-linux") > 0
    assert m._backoff_remaining(SYSTEM) == 0.0


def test_the_hold_expires():
    m = manager()
    m._record_failure(SYSTEM, "job-a", reason="JobFailed")
    m._backoff_until[SYSTEM] = time.monotonic() - 1.0
    assert m._backoff_remaining(SYSTEM) == 0.0


def test_pod_is_ready_reads_the_condition():
    assert _pod_is_ready({"conditions": [{"type": "Ready", "status": "True"}]})
    assert not _pod_is_ready({"conditions": [{"type": "Ready", "status": "False"}]})
    # A Pod with an IP but no conditions yet is not Ready. This is the case
    # that made registration the wrong reset signal.
    assert not _pod_is_ready({"podIP": "10.0.0.1"})
    assert not _pod_is_ready({})
    assert not _pod_is_ready({"conditions": None})
    assert not _pod_is_ready({"conditions": [{"type": "PodScheduled", "status": "True"}]})


def test_job_age_prefers_start_time():
    now = datetime(2026, 9, 10, 12, 0, 0, tzinfo=UTC)
    raw = {
        "metadata": {"creationTimestamp": "2026-09-10T11:00:00Z"},
        "status": {"startTime": "2026-09-10T11:59:00Z"},
    }
    assert _job_age_seconds(raw, now) == 60.0


def test_job_age_falls_back_to_creation():
    """A Job has a creationTimestamp before the controller writes a status."""
    now = datetime(2026, 9, 10, 12, 0, 0, tzinfo=UTC)
    raw = {"metadata": {"creationTimestamp": "2026-09-10T11:55:00Z"}, "status": {}}
    assert _job_age_seconds(raw, now) == 300.0


def test_job_age_is_none_without_a_timestamp():
    now = datetime(2026, 9, 10, 12, 0, 0, tzinfo=UTC)
    assert _job_age_seconds({}, now) is None
    assert _job_age_seconds({"status": {"startTime": "not a date"}}, now) is None


def job_raw(started: str, uid: str = "uid-1") -> dict:
    return {"metadata": {"uid": uid}, "status": {"startTime": started}}


NOW = datetime(2026, 9, 10, 12, 0, 0, tzinfo=UTC)


def test_a_young_builder_is_left_alone():
    m = manager(startup_timeout=600.0)
    assert not m._startup_expired(job_raw("2026-09-10T11:55:00Z"), "job-a", "Pending", NOW)


def test_a_pod_that_never_left_pending_expires():
    m = manager(startup_timeout=600.0)
    assert m._startup_expired(job_raw("2026-09-10T11:45:00Z"), "job-a", "Pending", NOW)


def test_a_job_with_no_pod_at_all_expires():
    """No Pod and no phase is the unschedulable case."""
    m = manager(startup_timeout=600.0)
    assert m._startup_expired(job_raw("2026-09-10T11:45:00Z"), "job-a", None, NOW)


def test_a_running_builder_never_expires():
    """The reason this is not activeDeadlineSeconds.

    A healthy builder Job runs for hours -- 3h36m measured on a live cluster.
    Age alone would end it. The phase is what says it started, and it comes
    from the API server, so it survives a restart of this process.
    """
    m = manager(startup_timeout=600.0)
    assert not m._startup_expired(job_raw("2026-09-10T08:00:00Z"), "job-a", "Running", NOW)


def test_a_running_builder_with_a_flaking_probe_never_expires():
    """The restart case. `_ever_ready` is empty and the builder is four hours old."""
    m = manager(startup_timeout=600.0)
    assert not m._ever_ready
    assert not m._startup_expired(job_raw("2026-09-10T08:00:00Z"), "job-a", "Running", NOW)


def test_a_builder_seen_ready_never_expires():
    m = manager(startup_timeout=600.0)
    m._ever_ready.add("job-a")
    assert not m._startup_expired(job_raw("2026-09-10T08:00:00Z"), "job-a", "Pending", NOW)


def test_an_expired_builder_is_not_acted_on_twice():
    m = manager(startup_timeout=600.0)
    old = job_raw("2026-09-10T11:00:00Z")
    assert m._startup_expired(old, "job-a", "Pending", NOW)
    m._record_failure(SYSTEM, "job-a", reason="StartupTimeout")
    assert not m._startup_expired(old, "job-a", "Pending", NOW)


def test_a_builder_with_no_timestamp_is_left_alone():
    m = manager(startup_timeout=600.0)
    assert not m._startup_expired({"metadata": {}}, "job-a", "Pending", NOW)


def test_the_job_carries_no_deadline():
    """The rendered Job, not the option that feeds it.

    activeDeadlineSeconds would cap a healthy builder's life, so it is absent
    from both the Job spec and the Pod spec. The Nix side asserts the same
    thing about the PodTemplate it renders.
    """
    m = manager()
    job = m._build_job_resource(SYSTEM, {"spec": {"containers": []}})
    assert "activeDeadlineSeconds" not in job["spec"]
    assert "activeDeadlineSeconds" not in job["spec"]["template"]["spec"]
    assert job["spec"]["backoffLimit"] == 0
    assert job["spec"]["ttlSecondsAfterFinished"] == 300
    assert job["metadata"]["labels"]["nixkube/system"] == SYSTEM
    assert job["metadata"]["name"].startswith("nixkube-builder-")


def test_overrides_still_merge():
    m = manager()
    job = m._build_job_resource(
        SYSTEM,
        {"spec": {}},
        overrides={"spec": {"template": {"spec": {"nodeName": "n1"}}}},
    )
    assert job["spec"]["template"]["spec"]["nodeName"] == "n1"
    assert job["spec"]["backoffLimit"] == 0


def test_pod_state_defaults_are_empty():
    """An absent Pod reads as absent, not as a started one."""
    state = PodState()
    assert state.ip is None
    assert state.ready is False
    assert state.phase is None
    assert state.node is None


def test_a_failure_carries_the_node_but_does_not_count_by_it():
    """The node is for the operator, not for the arithmetic.

    One broken node makes every builder for its system back off. The count
    stays per system -- the scheduler places builders, so per-node state would
    not stop a retry landing on the same node -- and the node goes in the log
    line so "the cluster is slow" can be told from "this node is broken".
    """
    m = manager()
    m._record_failure(SYSTEM, "job-a", reason="StartupTimeout", node="node-1")
    m._record_failure(SYSTEM, "job-b", reason="StartupTimeout", node="node-2")
    assert m._failures[SYSTEM] == 2


def test_the_builder_store_spec_validates():
    """The spec pydantic actually accepts.

    `known_hosts` has no default in pynixd, so a spec that omits it raises
    ValidationError at construction. This call omitted it, and every builder
    registration on a live cluster failed with:

        ValidationError: 1 validation error for SSHSubprocessStoreSpec
        known_hosts
          Field required [type=missing, ...]

    Constructing it here is the whole test: it fails the same way if the
    field goes missing again.
    """
    spec = BuilderManager._builder_store_spec("builder-job-a", "10.0.0.1")
    assert spec.known_hosts == BUILDER_KNOWN_HOSTS
    assert spec.host == "10.0.0.1"
    assert spec.username == "nix"
    assert spec.no_schedule is False


def test_a_probe_store_does_not_take_builds():
    spec = BuilderManager._builder_store_spec("builder-job-a", "10.0.0.1", probe=True)
    assert spec.no_schedule is True
    assert spec.known_hosts == BUILDER_KNOWN_HOSTS


def test_the_known_hosts_file_is_the_mounted_configmap():
    """The path the deployment mounts, not an arbitrary one.

    kubenix/pynixd.nix mounts the ssh-dynauth ConfigMap at /etc/ssh-dynauth,
    and kubenix/secret.nix writes ssh_known_hosts into it. A change to either
    has to change this too, or no builder verifies.
    """
    assert BUILDER_KNOWN_HOSTS == "/etc/ssh-dynauth/ssh_known_hosts"


class _FakeStore:
    """Just enough of an SSHSubprocessStore for the probe path."""

    def __init__(self, features: dict | None = None) -> None:
        self._probe_event = anyio.Event()
        self.feature_matrix = features or {}


class _FakeServer:
    def __init__(self, stores: dict | None = None) -> None:
        self.stores = stores or {}


async def _probe_run(m, monkeypatch, node="node-1"):
    """Run _probe_node with the cluster calls stubbed out.

    The `finally` fetches the Job to delete it. Without this the test would
    depend on an API call failing, which is a slow way to be right.
    """
    created = []
    deleted = []

    async def fake_create(system, overrides=None):
        created.append(overrides)
        return "nixkube-builder-probe1"

    async def fake_get(name, namespace=None):
        return f"job:{name}"

    async def fake_delete(job):
        deleted.append(job)

    async def fake_reconcile_systems():
        return None

    monkeypatch.setattr(builder_manager.Job, "get", staticmethod(fake_get))
    m._create_builder_job = fake_create
    m._delete_job = fake_delete
    m._reconcile_systems = fake_reconcile_systems
    m._pending_probes.add(node)
    await m._probe_node(node, SYSTEM)
    return created, deleted


@pytest.mark.asyncio
async def test_a_probe_waits_the_builder_budget_then_gives_up(monkeypatch):
    """A probe gave a builder 30 seconds to register, and a builder takes longer.

    On nixlab2 all four nodes logged `probe_store_not_found` 31 seconds after
    `probe_node_started`, with the probe Pod still ContainerCreating and 11
    seconds old. No node was ever labelled, so `_watch_nodes` saw it
    unlabelled and started again four seconds later, for ever.
    """
    m = manager(startup_timeout=0.3)
    m.server = _FakeServer()

    started = time.monotonic()
    created, deleted = await _probe_run(m, monkeypatch)
    waited = time.monotonic() - started

    # The budget, not a hardcoded 30 seconds.
    assert waited >= 0.3
    assert waited < 5.0
    # It let go, and it cleaned up.
    assert "node-1" not in m._pending_probes
    assert deleted == ["job:nixkube-builder-probe1"]
    # A probe Job asks the scheduler for its node, and is labelled a probe.
    #
    # `nodeName` would skip the scheduler: the Pod is placed rather than
    # scheduled, so taints, cordons and resources never enter the decision.
    # That is how a probe reached a control-plane node and sat in
    # ContainerCreating for three and a half hours. Asserted as an absence as
    # well as a presence, because putting `nodeName` back would otherwise
    # pass -- both keys can coexist and `nodeName` wins.
    probe_spec = created[0]["spec"]["template"]["spec"]
    assert "nodeName" not in probe_spec
    terms = probe_spec["affinity"]["nodeAffinity"]["requiredDuringSchedulingIgnoredDuringExecution"][
        "nodeSelectorTerms"
    ]
    assert terms == [
        {
            "matchExpressions": [
                {
                    "key": "kubernetes.io/hostname",
                    "operator": "In",
                    "values": ["node-1"],
                }
            ]
        }
    ]
    assert created[0]["metadata"]["labels"]["nixkube/probe"] == "true"


@pytest.mark.asyncio
async def test_a_probe_that_registers_but_never_answers_lets_go(monkeypatch):
    """The `_probe_event.wait()` that had no timeout.

    A store that registered and never answered held this coroutine open, so
    the `finally` never ran, the probe Job leaked pinned to a node, and
    `_pending_probes` blocked every later probe of that node.
    """
    m = manager(startup_timeout=0.3)
    store = _FakeStore()
    m.server = _FakeServer({"builder-nixkube-builder-probe1": store})

    _created, deleted = await _probe_run(m, monkeypatch)

    assert not store._probe_event.is_set()
    assert "node-1" not in m._pending_probes
    assert deleted == ["job:nixkube-builder-probe1"]


@pytest.mark.asyncio
async def test_a_probe_that_answers_labels_the_node(monkeypatch):
    m = manager(startup_timeout=5.0)
    store = _FakeStore(features={SYSTEM: {"big-parallel"}})
    store._probe_event.set()
    m.server = _FakeServer({"builder-nixkube-builder-probe1": store})

    labelled = []

    async def fake_label(node_name, system, features):
        labelled.append((node_name, system, features))

    m._label_node = fake_label
    await _probe_run(m, monkeypatch)

    assert labelled == [("node-1", SYSTEM, {SYSTEM: {"big-parallel"}})]
    assert "node-1" not in m._pending_probes
