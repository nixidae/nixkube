# SPDX-License-Identifier: Apache-2.0

import os
import shutil
import tempfile
from pathlib import Path

import anyio
import anyio.lowlevel
import pytest

from src.errors import HardlinkClosureError
from src.hardlinks import (
    _YIELD_EVERY,
    deref_hardlink_tree,
    hardlink_closure,
    hardlink_tree,
)


class TestHardlinkTree:
    """Tests for hardlink_tree function."""

    @pytest.mark.asyncio
    async def test_hardlink_single_file(self) -> None:
        """Hardlink a single file."""
        with tempfile.TemporaryDirectory() as tmpdir:
            src_file = Path(tmpdir) / "src" / "file.txt"
            src_file.parent.mkdir(parents=True)
            src_file.write_text("content")

            dst_file = Path(tmpdir) / "dst" / "file.txt"
            await hardlink_tree(src_file, dst_file)

            assert dst_file.exists()
            assert dst_file.read_text() == "content"
            # Check they're hardlinks (same inode)
            assert os.stat(src_file).st_ino == os.stat(dst_file).st_ino

    @pytest.mark.asyncio
    async def test_hardlink_directory(self) -> None:
        """Hardlink a directory tree."""
        with tempfile.TemporaryDirectory() as tmpdir:
            src_dir = Path(tmpdir) / "src"
            src_dir.mkdir()
            (src_dir / "file1.txt").write_text("data1")
            (src_dir / "subdir").mkdir()
            (src_dir / "subdir" / "file2.txt").write_text("data2")

            dst_dir = Path(tmpdir) / "dst"
            await hardlink_tree(src_dir, dst_dir)

            assert (dst_dir / "file1.txt").read_text() == "data1"
            assert (dst_dir / "subdir" / "file2.txt").read_text() == "data2"

    @pytest.mark.asyncio
    async def test_hardlink_symlink_preserved(self) -> None:
        """Hardlink should preserve symlinks."""
        with tempfile.TemporaryDirectory() as tmpdir:
            src_dir = Path(tmpdir) / "src"
            src_dir.mkdir()
            target_file = src_dir / "target.txt"
            target_file.write_text("target")
            link_path = src_dir / "link"
            os.symlink("target.txt", link_path)

            dst_dir = Path(tmpdir) / "dst"
            await hardlink_tree(src_dir, dst_dir)

            dst_link = dst_dir / "link"
            assert dst_link.is_symlink()
            assert os.readlink(dst_link) == "target.txt"


class TestDerefHardlinkTree:
    """Tests for deref_hardlink_tree function."""

    @pytest.mark.asyncio
    async def test_dereference_symlink_in_store(self) -> None:
        """Symlink to /nix/store target should be dereferenced."""
        with tempfile.TemporaryDirectory() as tmpdir:
            # Create fake /nix/store structure
            store_dir = Path(tmpdir) / "nix" / "store"
            store_dir.mkdir(parents=True)

            target = store_dir / "target-file"
            target.write_text("target content")

            src_dir = store_dir / "source-dir"
            src_dir.mkdir()
            link = src_dir / "link"
            os.symlink(str(target), str(link))

            dst = Path(tmpdir) / "output"
            await deref_hardlink_tree(src_dir, dst)

            # Symlink should be dereferenced (file should exist, not symlink)
            result_link = dst / "link"
            assert result_link.exists()
            assert result_link.read_text() == "target content"

    @pytest.mark.asyncio
    async def test_broken_symlink_in_store_copied(self) -> None:
        """Broken symlink in /nix/store should be copied as-is."""
        with tempfile.TemporaryDirectory() as tmpdir:
            store_dir = Path(tmpdir) / "nix" / "store"
            store_dir.mkdir(parents=True)

            src_dir = store_dir / "source"
            src_dir.mkdir()
            broken_link = src_dir / "broken"
            # Create symlink to non-existent target in /nix/store
            os.symlink("/nix/store/nonexistent-path", broken_link)

            dst = Path(tmpdir) / "output"
            await deref_hardlink_tree(src_dir, dst)

            result_link = dst / "broken"
            # Should be symlink, not dereferenced
            assert result_link.is_symlink()
            assert os.readlink(result_link) == "/nix/store/nonexistent-path"

    @pytest.mark.asyncio
    async def test_symlink_outside_store_copied(self) -> None:
        """Symlink pointing outside /nix/store should be copied as-is."""
        with tempfile.TemporaryDirectory() as tmpdir:
            store_dir = Path(tmpdir) / "nix" / "store"
            store_dir.mkdir(parents=True)

            src_dir = store_dir / "source"
            src_dir.mkdir()

            # Create symlink to outside /nix/store
            link = src_dir / "external_link"
            os.symlink("/etc/passwd", link)

            dst = Path(tmpdir) / "output"
            await deref_hardlink_tree(src_dir, dst)

            result_link = dst / "external_link"
            assert result_link.is_symlink()
            assert os.readlink(result_link) == "/etc/passwd"

    @pytest.mark.asyncio
    async def test_regular_file_hardlinked(self) -> None:
        """Regular files should be hardlinked."""
        with tempfile.TemporaryDirectory() as tmpdir:
            src_file = Path(tmpdir) / "src_file.txt"
            src_file.write_text("content")

            dst_file = Path(tmpdir) / "dst_file.txt"
            await deref_hardlink_tree(src_file, dst_file)

            assert dst_file.read_text() == "content"
            # Should be hardlinks (same inode)
            assert os.stat(src_file).st_ino == os.stat(dst_file).st_ino

    @pytest.mark.asyncio
    async def test_nested_structure_with_symlinks(self) -> None:
        """Complex nested structure with mixed symlinks and files."""
        with tempfile.TemporaryDirectory() as tmpdir:
            store_dir = Path(tmpdir) / "nix" / "store"
            store_dir.mkdir(parents=True)

            src = store_dir / "app"
            src.mkdir()

            # Regular file
            (src / "main").write_text("executable")

            # Subdir with content
            subdir = src / "lib"
            subdir.mkdir()
            (subdir / "lib.so").write_text("library")

            # Symlink to in-store target
            target = store_dir / "lib-target"
            target.mkdir()
            (target / "real.so").write_text("real library")
            os.symlink(str(target), src / "link_to_target")

            dst = Path(tmpdir) / "output"
            await deref_hardlink_tree(src, dst)

            assert (dst / "main").read_text() == "executable"
            assert (dst / "lib" / "lib.so").read_text() == "library"
            # Symlink to target should be dereferenced
            assert (dst / "link_to_target" / "real.so").read_text() == "real library"

    @pytest.mark.asyncio
    async def test_empty_directory(self) -> None:
        """Empty directory should be created at destination."""
        with tempfile.TemporaryDirectory() as tmpdir:
            src_dir = Path(tmpdir) / "empty"
            src_dir.mkdir()

            dst = Path(tmpdir) / "output"
            await deref_hardlink_tree(src_dir, dst)

            assert dst.exists()
            assert dst.is_dir()


class TestEventLoopStaysAlive:
    """The reason these functions are async at all.

    `prepare_volume` runs on the loop that serves the NRI heartbeat and the
    ZeroMQ REP socket, and `nri-wait` gives up after 30s of silence. A walk
    that never yields kills the container it is linking a store into.
    Issue #45.
    """

    @pytest.mark.asyncio
    async def test_a_walk_lets_another_task_run(self) -> None:
        ticks = 0

        async def ticker() -> None:
            nonlocal ticks
            while True:
                await anyio.lowlevel.checkpoint()
                ticks += 1

        with tempfile.TemporaryDirectory() as tmpdir:
            src_dir = Path(tmpdir) / "src"
            src_dir.mkdir()
            # More than one checkpoint's worth of entries.
            for i in range(_YIELD_EVERY * 3):
                (src_dir / f"f{i:05d}").write_text("x")

            async with anyio.create_task_group() as pump:
                pump.start_soon(ticker)
                await hardlink_tree(src_dir, Path(tmpdir) / "dst")
                pump.cancel_scope.cancel()

        assert ticks > 0, "the walk never gave the loop a turn"


class TestWhichSideWasMissing:
    """A failed link says whether the store path or the volume went away.

    `os.link` names the source first in both directions, so the message on
    its own reads as store corruption either way. That ambiguity cost two
    sessions on one incident: a verified closure appeared to lose a file,
    and the log could not say whether the file or its destination was gone.
    """

    @pytest.mark.asyncio
    async def test_a_store_path_that_is_not_there_fails_the_volume(self) -> None:
        """Not a no-op: silence here gives the container an empty /nix."""
        with tempfile.TemporaryDirectory() as tmpdir:
            gone = Path(tmpdir) / "store" / "abc-gone"
            dst = Path(tmpdir) / "vol" / "nix/store"
            with pytest.raises(HardlinkClosureError) as caught:
                await hardlink_closure({gone}, dst)

        logs = caught.value.logs
        assert logs is not None, "the hardlink failure carries no logs"
        assert f"source {gone} exists=False" in logs

    @pytest.mark.asyncio
    async def test_deref_also_refuses_a_path_that_is_not_there(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            gone = Path(tmpdir) / "store" / "abc-gone"
            with pytest.raises(FileNotFoundError):
                await deref_hardlink_tree(gone, Path(tmpdir) / "vol")

    @pytest.mark.asyncio
    async def test_deref_still_copies_a_broken_symlink(self) -> None:
        """The one absent target that is not an error: issue-documented."""
        with tempfile.TemporaryDirectory() as tmpdir:
            src = Path(tmpdir) / "link"
            src.symlink_to("/nix/store/0000000000000000000000000000000-nope")
            dst = Path(tmpdir) / "out" / "link"
            await deref_hardlink_tree(src, dst)
            assert dst.is_symlink()

    @pytest.mark.asyncio
    async def test_a_missing_target_does_not_accuse_the_source(self) -> None:
        """The case the incident actually needed telling apart."""
        with tempfile.TemporaryDirectory() as tmpdir:
            src = Path(tmpdir) / "store" / "abc-pkg"
            src.mkdir(parents=True)
            (src / "file").write_text("x")

            dst_root = Path(tmpdir) / "vol" / "nix/store"

            real_link = Path.hardlink_to

            def vanishing(self: Path, target: Path) -> None:
                # The volume goes away between the walk and the link, which
                # is what a teardown racing a CreateContainer handler does.
                shutil.rmtree(dst_root, ignore_errors=True)
                real_link(self, target)

            with pytest.MonkeyPatch.context() as patch:
                patch.setattr(Path, "hardlink_to", vanishing)
                with pytest.raises(HardlinkClosureError) as caught:
                    await hardlink_closure({src}, dst_root)

        logs = caught.value.logs
        assert logs is not None, "the hardlink failure carries no logs"
        assert f"source {src / 'file'} exists=True" in logs, "the store file is still there and the log must say so"
        assert "parent" in logs and "exists=False" in logs, "the missing side is the target's parent directory"


class TestAnInterruptedWalkIsNotMistakenForAFinishedOne:
    """A store path that exists must be a store path that is complete.

    The dedup check skips a path whose directory is already there. A walk
    killed partway -- an OOM kill, a node reboot -- leaves exactly that
    shape, so without the staging rename the retry hands the container a
    truncated closure and logs nothing at all.
    """

    @staticmethod
    def _store_path(root: Path, count: int = 3) -> Path:
        src = root / "store" / "abc-pkg"
        src.mkdir(parents=True)
        for index in range(count):
            (src / f"f{index:05d}").write_text("x")
        return src

    @pytest.mark.asyncio
    async def test_a_cancelled_walk_leaves_no_store_path_behind(self) -> None:
        """Cancellation is the case the staging rename is for.

        `CancelledError` is a BaseException, so the `except Exception` that
        tidies up after a failure never runs. A removed container now
        cancels its own build (issue #64), so this is a path the code
        takes, not a hypothetical one.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            # Enough entries to span checkpoints, so the cancel lands inside
            # the walk rather than after it.
            src = self._store_path(root, _YIELD_EVERY * 2)
            dst = root / "vol" / "nix/store"

            started = anyio.Event()
            real_link = Path.hardlink_to

            def notice(self: Path, target: Path) -> None:
                real_link(self, target)
                started.set()

            with pytest.MonkeyPatch.context() as patch:
                patch.setattr(Path, "hardlink_to", notice)
                async with anyio.create_task_group() as tg:
                    tg.start_soon(hardlink_closure, {src}, dst)
                    await started.wait()
                    tg.cancel_scope.cancel()

            assert not os.path.lexists(dst / src.name), "a cancelled walk left a store path the next attempt will skip"

    @pytest.mark.asyncio
    async def test_it_leaves_nothing_of_its_own_behind(self) -> None:
        """The staging name must not reach the container's store."""
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            src = self._store_path(root)
            dst = root / "vol" / "nix/store"

            await hardlink_closure({src}, dst)

            assert sorted(p.name for p in dst.iterdir()) == [src.name]

    @pytest.mark.asyncio
    async def test_a_broken_symlink_store_path_survives_a_second_call(self) -> None:
        """`exists` answers False for one, so it used to be relinked."""
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            src = root / "store" / "abc-link"
            src.parent.mkdir(parents=True)
            src.symlink_to("/nix/store/0000000000000000000000000000000-nope")
            dst = root / "vol" / "nix/store"

            await hardlink_closure({src}, dst)
            await hardlink_closure({src}, dst)

            assert (dst / src.name).is_symlink()
