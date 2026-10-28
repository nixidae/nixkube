# SPDX-License-Identifier: Apache-2.0

from pathlib import Path

from src.volume import is_mount, is_mount_source


class TestIsMount:
    """Tests for is_mount() using a temp mounts file."""

    def write_mounts(self, tmp_path: Path, content: str) -> Path:
        mounts_file = tmp_path / "mounts"
        mounts_file.write_text(content)
        return mounts_file

    def test_path_present_returns_true(self, tmp_path: Path):
        """Path listed as mountpoint should return True."""
        target = tmp_path / "target"
        target.mkdir()
        mounts = self.write_mounts(
            tmp_path,
            f"tmpfs {target.resolve()} tmpfs rw,nosuid,nodev 0 0\n",
        )
        assert is_mount(target, mounts_file=mounts) is True

    def test_path_absent_returns_false(self, tmp_path: Path):
        """Path not listed as mountpoint should return False."""
        target = tmp_path / "target"
        target.mkdir()
        other = tmp_path / "other"
        other.mkdir()
        mounts = self.write_mounts(
            tmp_path,
            f"tmpfs {other.resolve()} tmpfs rw 0 0\n",
        )
        assert is_mount(target, mounts_file=mounts) is False

    def test_empty_mounts_file_returns_false(self, tmp_path: Path):
        """Empty mounts file should return False."""
        target = tmp_path / "target"
        target.mkdir()
        mounts = self.write_mounts(tmp_path, "")
        assert is_mount(target, mounts_file=mounts) is False

    def test_malformed_line_skipped(self, tmp_path: Path):
        """Lines with fewer than 2 fields should be skipped gracefully."""
        target = tmp_path / "target"
        target.mkdir()
        mounts = self.write_mounts(
            tmp_path,
            f"incomplete\n"  # only 1 field
            f"tmpfs {target.resolve()} tmpfs rw 0 0\n",
        )
        assert is_mount(target, mounts_file=mounts) is True

    def test_oserror_returns_false(self, tmp_path: Path):
        """OSError reading the mounts file should return False, not raise."""
        target = tmp_path / "target"
        target.mkdir()
        nonexistent = tmp_path / "does_not_exist"
        assert is_mount(target, mounts_file=nonexistent) is False

    def test_multiple_mounts_finds_correct_one(self, tmp_path: Path):
        """Correct path is found among multiple mount entries."""
        target = tmp_path / "target"
        target.mkdir()
        other1 = tmp_path / "other1"
        other1.mkdir()
        other2 = tmp_path / "other2"
        other2.mkdir()
        mounts = self.write_mounts(
            tmp_path,
            f"tmpfs {other1.resolve()} tmpfs rw 0 0\n"
            f"tmpfs {target.resolve()} tmpfs rw 0 0\n"
            f"tmpfs {other2.resolve()} tmpfs rw 0 0\n",
        )
        assert is_mount(target, mounts_file=mounts) is True

    def test_partial_path_match_not_counted(self, tmp_path: Path):
        """A path that is a prefix of a mount should not match."""
        target = tmp_path / "target"
        target.mkdir()
        subdir = target / "subdir"
        subdir.mkdir()
        mounts = self.write_mounts(
            tmp_path,
            f"tmpfs {subdir.resolve()} tmpfs rw 0 0\n",
        )
        assert is_mount(target, mounts_file=mounts) is False


class TestIsMountSource:
    """Tests for is_mount_source() using a temp mountinfo file.

    Field 4 is the mounted subtree inside its filesystem. For a bind mount
    that is the source directory, which is what a volume root is.
    """

    def write_mountinfo(self, tmp_path: Path, content: str) -> Path:
        mountinfo = tmp_path / "mountinfo"
        mountinfo.write_text(content)
        return mountinfo

    def line(self, root: str, target: str) -> str:
        return f"81 26 0:52 {root} {target} rw,relatime shared:1 - ext4 /dev/ubda rw\n"

    def test_source_present_returns_true(self, tmp_path: Path):
        """A volume root something is mounted from is live."""
        volume = tmp_path / "volume"
        volume.mkdir()
        info = self.write_mountinfo(tmp_path, self.line(str(volume.resolve()), "/var/lib/kubelet/x/mount"))
        assert is_mount_source(volume, mountinfo_file=info) is True

    def test_deleted_source_still_returns_true(self, tmp_path: Path):
        """The kernel marks an unlinked source "//deleted". It is still live.

        This is the state that used to make a pod unrepairable: the mount
        serves an unlinked directory, and no later mount can replace it.
        """
        volume = tmp_path / "volume"
        volume.mkdir()
        info = self.write_mountinfo(
            tmp_path,
            self.line(f"{volume.resolve()}//deleted", "/var/lib/kubelet/x/mount"),
        )
        assert is_mount_source(volume, mountinfo_file=info) is True

    def test_target_is_not_a_source(self, tmp_path: Path):
        """Being a mount *point* says nothing about being a mount *source*."""
        volume = tmp_path / "volume"
        volume.mkdir()
        info = self.write_mountinfo(tmp_path, self.line("/somewhere/else", str(volume.resolve())))
        assert is_mount_source(volume, mountinfo_file=info) is False

    def test_absent_source_returns_false(self, tmp_path: Path):
        """Nothing mounts it, so it is collectable."""
        volume = tmp_path / "volume"
        volume.mkdir()
        info = self.write_mountinfo(tmp_path, self.line("/other", "/mnt/x"))
        assert is_mount_source(volume, mountinfo_file=info) is False

    def test_unreadable_mountinfo_returns_true(self, tmp_path: Path):
        """Unknown must not authorise a delete.

        The opposite default loses a running pod's store whenever the mount
        table cannot be read, which is the moment it is least safe to guess.
        """
        volume = tmp_path / "volume"
        volume.mkdir()
        assert is_mount_source(volume, mountinfo_file=tmp_path / "missing") is True
