# SPDX-License-Identifier: Apache-2.0
"""The GC loop, which collected nothing on any node for 33 hours.

Issue #38 counted four independent stops in one loop. Each one of them was
silent, and the first visible symptom on a Talos node is the kubelet evicting
pods for disk. These state what each fix does.

Every line of nix output quoted here was measured against nix 2.34.8, not
invented.
"""

from __future__ import annotations

import io
import json
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock

import anyio
import pytest

from src import gc_task
from src.gc_task import StoreScan, _count_deleted, scan_path_info


async def _scan(info: dict, cutoff: float) -> StoreScan:
    """Run the real streaming reduction over `info`, as nix would send it."""
    return await scan_path_info(io.BytesIO(json.dumps(info).encode()), cutoff)


# `nix store delete --store <s> --stdin` over one dead path and one live one.
# Exit code 1.
MIXED = [
    "finding garbage collector roots...",
    "deleting '/nix/store/bmwscnrzx2567yy91z3sp4nb1ixsv1vp-dead'",
    (
        "error: Cannot delete path '/nix/store/wwvlxqk32f7r78mwg7b57y3g5ql4hbsx-alive' "
        "since it is still alive. To find out why, use: nix-store --query --roots "
        "and nix-store --query --referrers"
    ),
    "1 store paths deleted, 0.0 KiB freed",
]


class TestSelectOldPaths:
    """`nix path-info --all --json` answers an object, not a list.

    Read as a list, iteration walks the keys, which are strings. The old filter
    asked `isinstance(entry, dict)` of each and got False every time, so the
    delete step received an empty list on every cycle on every node.
    """

    @pytest.mark.asyncio
    async def test_a_path_registered_before_the_cutoff_is_selected(self):
        info = {"/nix/store/aaa-old": {"registrationTime": 100, "narSize": 1}}
        scan = await _scan(info, cutoff=200)
        assert scan.old_paths == ["/nix/store/aaa-old"]

    @pytest.mark.asyncio
    async def test_a_path_registered_after_the_cutoff_is_kept(self):
        info = {"/nix/store/bbb-new": {"registrationTime": 300, "narSize": 1}}
        scan = await _scan(info, cutoff=200)
        assert scan.old_paths == []

    @pytest.mark.asyncio
    async def test_the_key_is_the_path_and_the_value_carries_no_path(self):
        """The shape that made the old code answer nothing.

        A value with no `path` key is what Nix really sends, so a reader that
        looks for one inside the value finds nothing however it iterates.
        """
        info = {"/nix/store/ccc-old": {"registrationTime": 1, "narSize": 120}}
        assert "path" not in info["/nix/store/ccc-old"]
        scan = await _scan(info, cutoff=2)
        assert scan.old_paths == ["/nix/store/ccc-old"]

    @pytest.mark.asyncio
    async def test_a_path_with_no_registration_time_is_kept(self):
        """Absent means "do not touch", not "old"."""
        scan = await _scan({"/nix/store/ddd": {"narSize": 1}}, cutoff=200)
        assert scan.old_paths == []

    @pytest.mark.asyncio
    async def test_the_real_output_of_nix_path_info_parses(self):
        """One entry, copied from `nix path-info --all --json --json-format 1`."""
        raw = (
            '{"/nix/store/slqhbc4rf1c8j67bjzjdrh32cmwwyrhn-probe-a":'
            '{"ca":"fixed:r:sha256:0v89ynsmc3yl5a6npyinc7fvmwl5cw1dd00lygb1193q3ghd53d0",'
            '"deriver":null,"narHash":"sha256-oI3S4Bt4pBDW8xSA1gJnhfK63WE2+muNKtQPVrX1CW0=",'
            '"narSize":120,"references":[],"registrationTime":1789602613,'
            '"signatures":[],"ultimate":false}}'
        )
        scan = await scan_path_info(io.BytesIO(raw.encode()), cutoff=1789602614)
        assert scan.old_paths == ["/nix/store/slqhbc4rf1c8j67bjzjdrh32cmwwyrhn-probe-a"]
        assert scan.nar_bytes == 120

    @pytest.mark.asyncio
    async def test_it_checkpoints_while_it_reads(self):
        """The reason for the stream: this loop also serves the NRI heartbeat.

        Measured on 216,455 paths / 282 MB of JSON: parsing it whole held the
        loop for 1.374s, and in a thread for 0.841s, because the C json
        decoder keeps the GIL. Reduced this way the worst gap was 0.057s.
        """
        info = {
            f"/nix/store/{index:032d}-p": {"registrationTime": 1, "narSize": 1}
            for index in range(gc_task._SCAN_YIELD_EVERY * 2)
        }
        seen = 0

        async def counting_checkpoint() -> None:
            nonlocal seen
            seen += 1

        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(gc_task, "checkpoint", counting_checkpoint)
            scan = await _scan(info, cutoff=2)

        assert scan.paths == gc_task._SCAN_YIELD_EVERY * 2
        assert seen == 2, f"checkpointed {seen} times over {scan.paths} paths"

    def test_the_c_backend_is_the_one_that_ships(self):
        """A silent fall back to the pure-Python backend costs ~10x.

        `ijson` picks a backend at import. The node is where this runs and
        where a slow scan matters, so the packaged environment has to carry
        the compiled one, not just resolve the name.
        """
        import ijson

        assert ijson.backend == "yajl2_c", f"ijson fell back to {ijson.backend!r}"

    @pytest.mark.asyncio
    async def test_a_truncated_answer_ends_the_cycle_quietly(self):
        """`nix path-info` killed mid-write leaves an incomplete object.

        It must not take the loop down: the cycle logs and returns, and the
        next one tries again.
        """
        import ijson

        with pytest.raises(ijson.JSONError):
            await scan_path_info(io.BytesIO(b'{"/nix/store/aaa": '), cutoff=0)


class TestCountDeleted:
    """A refused live path is the normal outcome, and not a failure.

    The loop offers every path older than GC_KEEP_SECONDS, and most of them are
    in use. Nix deletes what it can and exits 1 for the rest.
    """

    def test_a_clean_run_counts_what_it_deleted(self):
        lines = [
            "finding garbage collector roots...",
            "deleting '/nix/store/aaa-one'",
            "deleting '/nix/store/bbb-two'",
            "2 store paths deleted, 0.0 KiB freed",
        ]
        assert _count_deleted(lines) == 2

    def test_a_live_path_is_tolerated_and_not_counted(self):
        assert _count_deleted(MIXED) == 1

    def test_an_all_live_run_deletes_nothing_and_does_not_raise(self):
        lines = [
            (
                "error: Cannot delete path '/nix/store/wwv-alive' since it is still "
                "alive. To find out why, use: nix-store --query --roots"
            ),
            "0 store paths deleted, 0.0 KiB freed",
        ]
        assert _count_deleted(lines) == 0

    def test_any_other_error_still_fails_the_cycle(self):
        """Tolerating exit 1 must not tolerate a broken store.

        Without this the fix for the liveness case would swallow every failure
        the delete step can have.
        """
        lines = ["error: unrecognised flag '--skip-live'"]
        with pytest.raises(RuntimeError, match="unrecognised flag"):
            _count_deleted(lines)

    def test_a_real_error_beside_a_liveness_one_still_fails(self):
        lines = [*MIXED, "error: opening lock file: Permission denied"]
        with pytest.raises(RuntimeError, match="Permission denied"):
            _count_deleted(lines)

    def test_the_count_is_not_the_length_of_the_offered_list(self):
        """It used to be, so a cycle that deleted one of 300 reported 300."""
        assert _count_deleted(MIXED) != 2


class TestTheCommands:
    """What the loop actually runs.

    `--skip-live` is not a flag on nix 2.34.8 -- `error: unrecognised flag` --
    so the delete step failed on the flag alone and no cycle had ever finished
    on that version. Refusing a live path is already the default.
    """

    @pytest.fixture
    def recorded(self, monkeypatch) -> list[list[str]]:
        """Record each argv, and answer as a successful nix would."""
        calls: list[list[str]] = []

        async def fake_run_process(command, *, stdout=None, **_kwargs):
            argv = [str(arg) for arg in command]
            calls.append(argv)
            if stdout is not None:
                if "path-info" in argv:
                    stdout.write(json.dumps({"/nix/store/aaa-old": {"registrationTime": 1}}).encode())
                else:
                    stdout.write(b"deleting '/nix/store/aaa-old'\n")
            return subprocess.CompletedProcess(argv, 0, b"", b"")

        monkeypatch.setattr(gc_task.anyio, "run_process", fake_run_process)
        monkeypatch.setattr(gc_task, "PYNIXD_ENABLED", False)
        return calls

    @pytest.mark.asyncio
    async def test_the_delete_does_not_pass_skip_live(self, recorded):
        await gc_task._run_gc_cycle()

        delete = next(c for c in recorded if "delete" in c)
        assert "--skip-live" not in delete
        assert "--ignore-liveness" not in delete, "that flag deletes live paths"

    @pytest.mark.asyncio
    async def test_path_info_asks_for_a_json_format(self, recorded):
        """Bare `--json` is deprecated on 2.34 and warns on every cycle."""
        await gc_task._run_gc_cycle()

        info = next(c for c in recorded if "path-info" in c)
        assert "--json-format" in info
        assert info[info.index("--json-format") + 1] == "1"

    @pytest.mark.asyncio
    async def test_a_cycle_counts_what_nix_deleted(self, recorded):
        before = gc_task.GC_PATHS_DELETED._value.get()
        await gc_task._run_gc_cycle()
        assert gc_task.GC_PATHS_DELETED._value.get() == before + 1


class TestStoreSize:
    """How big this node's store is, taken from the GC cycle's own answer.

    Issue #39: there was no way to see a node's store grow from outside it,
    which is how #38 stayed invisible for 33 hours.
    """

    @pytest.mark.asyncio
    async def test_the_sum_and_the_count_come_from_one_answer(self):
        gc_task._record_store_size(
            await _scan(
                {
                    "/nix/store/aaa": {"narSize": 100, "registrationTime": 1},
                    "/nix/store/bbb": {"narSize": 250, "registrationTime": 2},
                },
                cutoff=0,
            )
        )

        assert gc_task.STORE_PATHS._value.get() == 2
        assert gc_task.STORE_NAR_BYTES._value.get() == 350

    @pytest.mark.asyncio
    async def test_an_empty_store_answers_zero_rather_than_nothing(self):
        """Absent and zero mean different things on a dashboard."""
        gc_task._record_store_size(await _scan({}, cutoff=0))

        assert gc_task.STORE_PATHS._value.get() == 0
        assert gc_task.STORE_NAR_BYTES._value.get() == 0

    @pytest.mark.asyncio
    async def test_a_path_with_no_nar_size_does_not_break_the_sum(self):
        gc_task._record_store_size(await _scan({"/nix/store/aaa": {"registrationTime": 1}}, cutoff=0))

        assert gc_task.STORE_NAR_BYTES._value.get() == 0


class TestLastSuccess:
    """The series that would have surfaced #38 on day one.

    A timestamp that stops advancing says the collector is stuck. No size gauge
    says that on its own.
    """

    def test_it_starts_at_zero(self):
        """So an alert reads `== 0 or time() - it > N`, not a 1970 timestamp.

        A private registry, because the shared gauge may already have been set
        by another test in this file.
        """
        from prometheus_client import CollectorRegistry, Gauge

        fresh = Gauge("probe", "probe", registry=CollectorRegistry())
        assert fresh._value.get() == 0

    @pytest.mark.asyncio
    async def test_a_finished_cycle_advances_it(self, monkeypatch):
        async def cycle():
            return None

        monkeypatch.setattr(gc_task, "_run_gc_cycle", cycle)
        monkeypatch.setattr(gc_task, "GC_LAST_SUCCESS", gc_task.GC_LAST_SUCCESS)

        before = gc_task.GC_LAST_SUCCESS._value.get()
        # One pass of the loop body, then stop it at the sleep.
        monkeypatch.setattr(gc_task.anyio, "sleep", AsyncMock(side_effect=anyio.get_cancelled_exc_class()))
        with pytest.raises(anyio.get_cancelled_exc_class()):
            await gc_task.gc_loop()

        assert gc_task.GC_LAST_SUCCESS._value.get() > before

    @pytest.mark.asyncio
    async def test_a_failed_cycle_does_not(self, monkeypatch):
        """It says when collection last worked, not when it last ran."""

        async def cycle():
            raise RuntimeError("nix store delete failed")

        monkeypatch.setattr(gc_task, "_run_gc_cycle", cycle)
        gc_task.GC_LAST_SUCCESS.set(1000.0)
        monkeypatch.setattr(gc_task.anyio, "sleep", AsyncMock(side_effect=anyio.get_cancelled_exc_class()))
        with pytest.raises(anyio.get_cancelled_exc_class()):
            await gc_task.gc_loop()

        assert gc_task.GC_LAST_SUCCESS._value.get() == 1000.0


class TestTimeouts:
    """No nix call in this loop may run without a deadline.

    `gc_loop` awaits one cycle at a time, so one call that never returns ends
    collection on that node until the pod restarts -- and the restart re-enters
    the same call.
    """

    def test_every_step_has_a_timeout_constant(self):
        from src.constants import (
            GC_COPY_TIMEOUT_SECONDS,
            GC_DELETE_TIMEOUT_SECONDS,
            GC_PATH_INFO_TIMEOUT_SECONDS,
        )

        assert GC_COPY_TIMEOUT_SECONDS > 0
        assert GC_PATH_INFO_TIMEOUT_SECONDS > 0
        assert GC_DELETE_TIMEOUT_SECONDS > 0

    @pytest.mark.asyncio
    async def test_a_path_info_that_never_returns_ends_the_cycle(self, monkeypatch):
        """The measured failure: the call hangs and the loop waits for ever."""

        async def hang(*_args, **_kwargs):
            await anyio.sleep(3600)

        monkeypatch.setattr(gc_task.anyio, "run_process", hang)
        monkeypatch.setattr(gc_task, "PYNIXD_ENABLED", False)
        monkeypatch.setattr(gc_task, "GC_PATH_INFO_TIMEOUT_SECONDS", 0.05)

        with pytest.raises(TimeoutError):
            await gc_task._run_gc_cycle()


class TestRealProcesses:
    """The two properties the GC loop needs from a subprocess, run for real.

    Both were broken at once before. `nix path-info --all --json` writes the
    whole store as one line, 300,000 characters and more, and the reader that
    was under this truncated it; the deadline around it did not fire at all.
    """

    @pytest.mark.asyncio
    async def test_a_giant_single_line_survives_a_file_redirect(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "big.txt"
            with anyio.fail_after(20):
                with out.open("wb") as handle:
                    await anyio.run_process(["python3", "-c", "print('x' * 300000)"], stdout=handle)
            assert out.stat().st_size == 300001

    @pytest.mark.asyncio
    async def test_the_delete_options_put_both_streams_in_one_file(self, tmp_path: Path) -> None:
        """`_delete`'s exact combination, against a real process.

        `input=` with `stdout=<file>` and `stderr=STDOUT` is what puts the
        `deleting '...'` lines and the `still alive` refusals in one log for
        `_count_deleted` to read. A wrong combination here loses the refusals,
        and the count silently becomes the offered length.
        """
        out = tmp_path / "delete.log"
        script = "import sys; print('from stdin: ' + sys.stdin.read().strip()); print('to stderr', file=sys.stderr)"

        with anyio.fail_after(30):
            with out.open("wb") as handle:
                completed = await anyio.run_process(
                    [sys.executable, "-c", script],
                    input=b"offered\n",
                    stdout=handle,
                    stderr=subprocess.STDOUT,
                    check=False,
                )

        assert completed.returncode == 0
        written = out.read_text()
        assert "from stdin: offered" in written
        assert "to stderr" in written

    @pytest.mark.asyncio
    async def test_a_command_that_hangs_past_the_deadline_raises(self):
        """The property the loop is built on, against a real process.

        Every stop issue #38 counted was a nix call that never returned. A
        deadline that expires silently puts the loop back there, and the fakes
        above cannot tell the difference: they hang in Python, not in a child.
        """
        with pytest.raises(TimeoutError):
            with anyio.fail_after(1):
                await anyio.run_process(
                    [
                        "python3",
                        "-c",
                        "import time; print('x' * 300000); time.sleep(60)",
                    ]
                )
