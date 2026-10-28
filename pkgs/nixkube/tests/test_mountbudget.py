# SPDX-License-Identifier: Apache-2.0

"""What a node says before its mount table fills up.

`fs.mount-max` counts per mount namespace and a bind farm spends one per
closure path. Past the limit, mount(2) answers ENOSPC and names no limit --
partway through a closure, leaving a store that is half there. Nothing
outside a node can see this coming, so the daemon has to say it.
"""

from pathlib import Path

import pytest
from structlog.testing import capture_logs

from src import mountbudget
from src.mountbudget import Budget, measure


def _mountinfo(tmp_path: Path, lines: int) -> Path:
    path = tmp_path / "mountinfo"
    path.write_text("".join(f"{n} 1 0:1 / /m{n} rw - tmpfs tmpfs rw\n" for n in range(lines)))
    return path


@pytest.fixture
def limit(monkeypatch):
    def _set(value: int) -> None:
        monkeypatch.setattr(mountbudget, "mount_limit", lambda: value)

    return _set


class TestBudgetArithmetic:
    def test_it_fits_when_there_is_room(self):
        assert Budget(used=10, limit=100, needed=50).fits

    def test_it_does_not_fit_when_the_request_is_the_overflow(self):
        """91 into 90 free. The one that matters: the request itself decides."""
        assert not Budget(used=10, limit=100, needed=91).fits

    def test_exactly_filling_the_limit_still_fits(self):
        assert Budget(used=10, limit=100, needed=90).fits

    def test_a_limit_of_zero_does_not_divide_by_it(self):
        assert Budget(used=0, limit=0, needed=1).ratio == 1.0


class TestAffordable:
    """Fitting and being affordable are different questions.

    A farm that exactly fills the mount table works, and then nothing else on
    the node can mount anything. The fallback threshold is what keeps the last
    tenth for everyone else.
    """

    def test_plenty_of_room_is_affordable(self):
        assert Budget(used=10, limit=100, needed=50).affordable

    def test_a_request_that_fits_but_leaves_no_headroom_is_not(self):
        """The case the whole policy turns on: it fits, and it must not run."""
        budget = Budget(used=10, limit=100, needed=85)
        assert budget.fits, "95 of 100 does fit"
        assert not budget.affordable, "and would leave the node 5 mounts"

    def test_exactly_at_the_threshold_is_not_affordable(self):
        assert not Budget(used=0, limit=100, needed=90).affordable

    def test_just_under_the_threshold_is(self):
        assert Budget(used=0, limit=100, needed=89).affordable


class TestMeasure:
    """`capture_logs`, not `caplog`: these are structlog's loggers, and
    nothing in this project routes them through the stdlib handler `caplog`
    reads. A `caplog` assertion here passes against silence."""

    def test_a_request_that_fits_is_quiet(self, tmp_path, limit):
        limit(100_000)
        with capture_logs() as logs:
            budget = measure(2443, mountinfo=_mountinfo(tmp_path, 200))

        assert budget.fits
        assert [entry["event"] for entry in logs] == ["mount_budget"]

    def test_a_request_that_does_not_fit_says_so_at_error_level(self, tmp_path, limit):
        limit(1000)
        with capture_logs() as logs:
            budget = measure(2443, mountinfo=_mountinfo(tmp_path, 200))

        assert not budget.fits
        said = [entry for entry in logs if entry["event"] == "mount_budget_exhausted"]
        assert said, f"stayed quiet about a farm that cannot be built: {logs}"
        assert said[0]["log_level"] == "error"

    def test_a_request_that_fits_but_takes_the_headroom_is_refused_loudly(self, tmp_path, limit):
        """The fallback case, which is silent otherwise.

        7500 held plus 2443 wanted is 99% of 10,000. It fits. Taking it leaves
        the node 57 mounts, and the volume that gets a hardlink tree instead
        is merely slower -- nothing about it says why.
        """
        limit(10_000)
        with capture_logs() as logs:
            budget = measure(2443, mountinfo=_mountinfo(tmp_path, 7500))

        assert budget.fits, "it fits; that is what makes the refusal a policy"
        assert not budget.affordable
        said = [entry for entry in logs if entry["event"] == "mount_budget_exhausted"]
        assert said, f"fell back to hardlinks without saying so: {logs}"
        assert said[0]["log_level"] == "error"

    def test_the_numbers_reach_the_log(self, tmp_path, limit):
        limit(1000)
        with capture_logs() as logs:
            measure(2443, mountinfo=_mountinfo(tmp_path, 200))

        said = next(entry for entry in logs if entry["event"] == "mount_budget_exhausted")
        assert said["needed"] == 2443 and said["limit"] == 1000, (
            f"a reader needs the closure size and the limit, not the verdict alone: {said}"
        )


class TestItNeverTakesTheCallerDown:
    """Reading /proc is not a reason to fail a container."""

    def test_an_unreadable_mountinfo_counts_zero(self, tmp_path):
        assert mountbudget.mounts_used(tmp_path / "not-there") == 0

    def test_an_unreadable_limit_falls_back_to_the_kernel_default(self, monkeypatch):
        monkeypatch.setattr(mountbudget, "MOUNT_MAX_PATH", Path("/nonexistent/mount-max"))
        assert mountbudget.mount_limit() == 100_000


class TestTheVolumeFallsBackRatherThanFailing:
    """A farm the node cannot afford must cost one volume time, not a container.

    Refusing outright was the first shape of this and it was worse: the pod
    does not start at all, for a reason that is about the node rather than
    about the pod.
    """

    @pytest.mark.asyncio
    async def test_an_unaffordable_farm_becomes_a_hardlink_tree(self, tmp_path, monkeypatch):
        from src import volume as volume_module

        linked: list[set[Path]] = []

        async def closure(paths):
            return {Path(f"/nix/store/{n:032d}-p") for n in range(3)}

        async def link(store_paths, dst):
            linked.append(set(store_paths))

        async def nothing(*_args, **_kwargs):
            return None

        monkeypatch.setattr(volume_module, "get_closure_paths", closure)
        monkeypatch.setattr(volume_module, "hardlink_closure", link)
        monkeypatch.setattr(volume_module, "init_database", nothing)
        monkeypatch.setattr(volume_module, "install_gcroots", nothing)
        monkeypatch.setattr(volume_module, "affordable", lambda _needed: False)

        with capture_logs() as logs:
            prepared = await volume_module.prepare_volume(
                tmp_path / "vol", {Path("/nix/store/aaa-pkg")}, None, bind_farm=True
            )

        assert prepared.bind_farm is False, "asked for a farm it could not afford"
        assert linked, "fell back and then linked nothing"
        said = [e for e in logs if e["event"] == "farm_fell_back_to_hardlinks"]
        assert said and said[0]["log_level"] == "error", f"fell back without saying so: {logs}"

    @pytest.mark.asyncio
    async def test_an_affordable_farm_copies_nothing(self, tmp_path, monkeypatch):
        from src import volume as volume_module

        linked: list[set[Path]] = []

        async def closure(paths):
            return {Path("/nix/store/aaa-pkg")}

        async def link(store_paths, dst):
            linked.append(set(store_paths))

        async def nothing(*_args, **_kwargs):
            return None

        monkeypatch.setattr(volume_module, "get_closure_paths", closure)
        monkeypatch.setattr(volume_module, "hardlink_closure", link)
        monkeypatch.setattr(volume_module, "init_database", nothing)
        monkeypatch.setattr(volume_module, "install_gcroots", nothing)
        monkeypatch.setattr(volume_module, "affordable", lambda _needed: True)

        prepared = await volume_module.prepare_volume(
            tmp_path / "vol", {Path("/nix/store/aaa-pkg")}, None, bind_farm=True
        )

        assert prepared.bind_farm is True
        assert not linked, "a farm must not link anything"
        assert prepared.paths == [Path("/nix/store/aaa-pkg")]

    @pytest.mark.asyncio
    async def test_a_host_store_refuses_rather_than_falls_back(self, tmp_path, monkeypatch):
        from src import volume as volume_module
        from src.errors import MountError

        async def closure(paths):
            return {Path("/nix/store/aaa-pkg")}

        async def nothing(*_args, **_kwargs):
            return None

        monkeypatch.setattr(volume_module, "get_closure_paths", closure)
        monkeypatch.setattr(volume_module, "hardlink_closure", nothing)
        monkeypatch.setattr(volume_module, "init_database", nothing)
        monkeypatch.setattr(volume_module, "affordable", lambda _needed: False)

        with pytest.raises(MountError, match="fall back"):
            await volume_module.prepare_volume(
                tmp_path / "vol",
                {Path("/nix/store/aaa-pkg")},
                None,
                bind_farm=True,
                allow_hardlinks=False,
            )
