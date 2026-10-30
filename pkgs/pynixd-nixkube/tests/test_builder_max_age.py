# SPDX-License-Identifier: Apache-2.0

"""Recycling a builder at `max_age`, without a cluster (#69).

A builder Job never completes on its own, so kube-prometheus raises
KubeJobNotCompleted when one outlives 12 hours. The manager replaces a builder
past `max_age`: the replacement starts first, the old one drains once the
replacement is Ready, and its Job goes only when it has nothing in flight.
"""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import TYPE_CHECKING, cast

import anyio
import pytest
from kr8s.asyncio.objects import Job
from pynixd.store import SSHSubprocessStore

from pynixd_nixkube.builder_manager import (
    BuilderManager,
    PodState,
    RecycleView,
    plan_recycle,
)

if TYPE_CHECKING:
    from pynixd.instance import Server

SYSTEM = "x86_64-linux"
HOUR = 3600.0


def view(
    store_id: str,
    system: str = SYSTEM,
    aged: bool = False,
    ready: bool = True,
    draining: bool = False,
    busy: bool = False,
) -> RecycleView:
    return RecycleView(store_id, system, aged, ready, draining, busy)


def test_an_aged_builder_waits_for_a_ready_replacement() -> None:
    assert plan_recycle([view("old", aged=True)], min_builders=1) == ([], [])
    starting = view("new", ready=False)
    assert plan_recycle([view("old", aged=True), starting], 1) == ([], [])
    assert plan_recycle([view("old", aged=True), view("new")], 1) == (["old"], [])


def test_the_replacement_must_be_of_the_same_system() -> None:
    other = view("new", system="aarch64-linux")
    assert plan_recycle([view("old", aged=True), other], 1) == ([], [])


def test_another_aged_builder_is_not_a_replacement() -> None:
    old = [view("a", aged=True), view("b", aged=True)]
    assert plan_recycle(old, 1) == ([], [])


def test_with_no_minimum_an_aged_builder_drains_at_once() -> None:
    assert plan_recycle([view("old", aged=True)], 0) == (["old"], [])


def test_a_draining_builder_is_retired_only_when_idle() -> None:
    busy = view("old", aged=True, draining=True, busy=True)
    assert plan_recycle([busy, view("new")], 1) == ([], [])
    idle = view("old", aged=True, draining=True)
    assert plan_recycle([idle, view("new")], 1) == ([], ["old"])


def test_a_fresh_builder_is_never_touched() -> None:
    assert plan_recycle([view("new")], 1) == ([], [])


class FakeStore(SSHSubprocessStore):
    """A real SSH store, with `in_flight` writable so a test can make it busy."""

    def __init__(self, store_id: str) -> None:
        super().__init__(BuilderManager._builder_store_spec(store_id, "10.0.0.1"))
        self._in_flight = 0

    @property
    def in_flight(self) -> int:
        return self._in_flight

    @in_flight.setter
    def in_flight(self, value: int) -> None:
        self._in_flight = value


class FakeScheduler:
    def __init__(self) -> None:
        self.queue = SimpleNamespace(queue=[])
        self.triggered = 0

    def trigger(self) -> None:
        self.triggered += 1


class FakeServer:
    def __init__(self) -> None:
        self.stores: dict[str, FakeStore] = {}
        self.scheduler = FakeScheduler()

    async def remove_store(self, store_id: str, drain_timeout: float = 300.0) -> None:
        self.stores.pop(store_id, None)

    async def add_store(self, store: SSHSubprocessStore, dynamic: bool = False) -> None:
        self.stores[str(store.store_id)] = FakeStore(str(store.store_id))


def job(name: str, age: float, now: datetime, system: str = SYSTEM) -> Job:
    started = (now - timedelta(seconds=age)).isoformat()
    labels = {"nixkube/system": system}
    return Job({"metadata": {"name": name, "labels": labels}, "status": {"startTime": started}})


class Cluster:
    """The Jobs the API server holds, and what the manager did to them."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, m: BuilderManager) -> None:
        self.now = datetime.now(UTC)
        self.jobs: dict[str, Job] = {}
        self.pods: dict[str, PodState] = {}
        self.created: list[str] = []
        self.deleted: list[str] = []
        self.m = m
        self.server = FakeServer()

        async def list_jobs() -> list[Job]:
            return list(self.jobs.values())

        async def create(system: str, overrides: object = None) -> str:
            name = f"job-{len(self.created) + 1}"
            self.created.append(name)
            return name

        async def get(name: str, namespace: str | None = None) -> Job:
            return self.jobs[name]

        async def delete(j: Job) -> None:
            self.deleted.append(j.metadata.name)

        async def pod_state(j: Job) -> PodState:
            return self.pods.get(j.metadata.name, PodState())

        async def not_stale(j: Job) -> bool:
            return False

        monkeypatch.setattr(Job, "get", staticmethod(get))
        monkeypatch.setattr(m, "server", self.server)
        monkeypatch.setattr(m, "_list_builder_jobs", list_jobs)
        monkeypatch.setattr(m, "_create_builder_job", create)
        monkeypatch.setattr(m, "_delete_job", delete)
        monkeypatch.setattr(m, "_get_job_pod_state", pod_state)
        monkeypatch.setattr(m, "_is_stale_builder", not_stale)

    def add(self, name: str, age: float, ready: bool = True, system: str = SYSTEM) -> None:
        self.jobs[name] = job(name, age, self.now, system)
        self.pods[name] = PodState(ip="10.0.0.1", ready=ready, phase="Running")

    def store(self, name: str) -> FakeStore:
        return self.server.stores[f"builder-{name}"]

    async def tick(self) -> None:
        """One `_periodic_reconcile` pass."""
        for j in list(self.jobs.values()):
            await self.m._reconcile_job(j)
        await self.m._ensure_min_builders()
        await self.m._recycle_aged()


def manager(
    *, min_builders: int = 1, max_builders: int = 3, max_age: float = 0.0, idle_timeout: int = 300
) -> BuilderManager:
    # The tests replace the server with a fake, so none is given here.
    m = BuilderManager(
        server=cast("Server", None),
        namespace="nixkube",
        min_builders=min_builders,
        max_builders=max_builders,
        max_age=max_age,
        idle_timeout=idle_timeout,
    )
    m._available_systems = {SYSTEM}
    return m


@pytest.mark.asyncio
async def test_the_last_builder_is_replaced_first_and_retired_when_idle(monkeypatch: pytest.MonkeyPatch) -> None:
    m = manager(min_builders=1, max_builders=1, max_age=6 * HOUR)
    cluster = Cluster(monkeypatch, m)
    cluster.add("old", age=7 * HOUR)
    await cluster.tick()
    cluster.store("old").in_flight = 1

    # The old builder is past its age: a replacement starts, even at the
    # maximum, and the old one still takes builds while it does.
    assert cluster.created == ["job-1"]
    assert not cluster.store("old").draining

    cluster.add("job-1", age=10, ready=False)
    await cluster.tick()
    assert not cluster.store("old").draining
    assert cluster.created == ["job-1"]

    # The replacement is Ready: the old one takes no new builds, and keeps
    # the one it has.
    cluster.pods["job-1"].ready = True
    await cluster.tick()
    assert cluster.store("old").draining
    assert cluster.server.scheduler.triggered == 1
    assert cluster.deleted == []

    cluster.store("old").in_flight = 0
    await cluster.tick()
    assert cluster.deleted == ["old"]
    assert "builder-old" not in cluster.server.stores
    assert not cluster.store("job-1").draining
    assert cluster.created == ["job-1"]


@pytest.mark.asyncio
async def test_an_assigned_build_keeps_a_draining_builder(monkeypatch: pytest.MonkeyPatch) -> None:
    """`in_flight` misses a build assigned but not yet sent."""
    m = manager(min_builders=1, max_age=6 * HOUR)
    cluster = Cluster(monkeypatch, m)
    cluster.add("old", age=7 * HOUR)
    cluster.add("new", age=60)
    await cluster.tick()
    assert cluster.store("old").draining

    build = SimpleNamespace(assigned_store_id="builder-old", is_done=False)
    cluster.server.scheduler.queue.queue.append(build)
    await cluster.tick()
    assert cluster.deleted == []

    build.is_done = True
    await cluster.tick()
    assert cluster.deleted == ["old"]


@pytest.mark.asyncio
async def test_a_queued_build_replaces_a_draining_builder_at_the_maximum(monkeypatch: pytest.MonkeyPatch) -> None:
    """With no minimum, an aged builder drains at once, and only the queue
    watch starts another. Counted against the maximum, it would block that."""
    m = manager(min_builders=0, max_builders=1, max_age=6 * HOUR)
    cluster = Cluster(monkeypatch, m)
    cluster.add("old", age=7 * HOUR)
    await cluster.tick()
    assert cluster.store("old").draining

    await m._maybe_create_builder(SYSTEM)
    assert cluster.created == ["job-1"]
    # The cooldown still holds the next one back.
    await m._maybe_create_builder(SYSTEM)
    assert cluster.created == ["job-1"]


@pytest.mark.asyncio
async def test_a_young_builder_is_kept(monkeypatch: pytest.MonkeyPatch) -> None:
    """The negative control: the same path, under the age."""
    m = manager(min_builders=1, max_age=6 * HOUR)
    cluster = Cluster(monkeypatch, m)
    cluster.add("young", age=5 * HOUR)
    await cluster.tick()
    await cluster.tick()
    assert cluster.created == []
    assert cluster.deleted == []
    assert not cluster.store("young").draining


@pytest.mark.asyncio
async def test_zero_means_never(monkeypatch: pytest.MonkeyPatch) -> None:
    m = manager(min_builders=1, max_age=0)
    cluster = Cluster(monkeypatch, m)
    cluster.add("old", age=100 * HOUR)
    await cluster.tick()
    assert cluster.created == []
    assert not cluster.store("old").draining


@pytest.mark.asyncio
async def test_the_idle_reaper_keeps_the_replacement(monkeypatch: pytest.MonkeyPatch) -> None:
    """While the old builder drains a long build, its idle replacement is the
    minimum. Counting the old one would reap the replacement, and the next
    reconcile would start another, every `idle_timeout`."""
    m = manager(min_builders=1, max_age=6 * HOUR, idle_timeout=0)
    cluster = Cluster(monkeypatch, m)
    cluster.add("old", age=7 * HOUR)
    cluster.add("new", age=60)
    await cluster.tick()
    cluster.store("old").in_flight = 1

    with anyio.move_on_after(0.5):
        await m._reap_idle()
    assert cluster.deleted == []


@pytest.mark.asyncio
async def test_the_idle_reaper_stops_at_the_minimum_in_one_pass(monkeypatch: pytest.MonkeyPatch) -> None:
    """A deleted Job stays registered until the watch sees it go, so the
    count is kept by the pass itself."""
    m = manager(min_builders=1, idle_timeout=0)
    cluster = Cluster(monkeypatch, m)
    for name in ("a", "b", "c"):
        cluster.add(name, age=60)
    await cluster.tick()

    with anyio.move_on_after(0.5):
        await m._reap_idle()
    assert len(cluster.deleted) == 2


@pytest.mark.asyncio
async def test_the_idle_reaper_keeps_the_minimum_of_each_system(monkeypatch: pytest.MonkeyPatch) -> None:
    """Measured on sg1: one fresh builder per system and a minimum of one.
    Counted across systems, the reaper deleted the x86_64 builder, which was
    the replacement an aged x86_64 builder waited for, every `idle_timeout`."""
    m = manager(min_builders=1, idle_timeout=0)
    m._available_systems = {SYSTEM, "aarch64-linux"}
    cluster = Cluster(monkeypatch, m)
    cluster.add("x86", age=60)
    cluster.add("arm", age=60, system="aarch64-linux")
    await cluster.tick()

    with anyio.move_on_after(0.5):
        await m._reap_idle()
    assert cluster.deleted == []
