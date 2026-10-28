# SPDX-License-Identifier: Apache-2.0

from pathlib import Path

import pytest

from src.nri.farm import FarmError, mount_points, place


@pytest.fixture
def store(tmp_path: Path) -> Path:
    """Three store paths: a directory, a file, a symlink."""
    store = tmp_path / "store"
    store.mkdir()
    (store / "aaa-dir").mkdir()
    (store / "aaa-dir" / "bin").write_text("#!/bin/sh\n")
    (store / "bbb-file").write_text("a file")
    (store / "ccc-link").symlink_to("/nix/store/aaa-dir/bin")
    return store


def test_place_makes_empty_mountpoints_and_copies_links(store: Path, tmp_path: Path) -> None:
    farm = tmp_path / "farm"
    place(farm, sorted(store.iterdir()))
    assert (farm / "aaa-dir").is_dir()
    # A place to land on, not a copy of the path.
    assert list((farm / "aaa-dir").iterdir()) == []
    assert (farm / "bbb-file").is_file()
    assert (farm / "bbb-file").read_text() == ""
    assert (farm / "ccc-link").readlink() == Path("/nix/store/aaa-dir/bin")


def test_place_twice_leaves_what_is_there(store: Path, tmp_path: Path) -> None:
    farm = tmp_path / "farm"
    place(farm, sorted(store.iterdir()))
    (farm / "aaa-dir" / "written-by-a-pod").touch()
    place(farm, sorted(store.iterdir()))
    assert (farm / "aaa-dir" / "written-by-a-pod").exists()


def test_place_refuses_a_path_that_is_not_there(store: Path, tmp_path: Path) -> None:
    with pytest.raises(FarmError, match="not there"):
        place(tmp_path / "farm", [store / "ddd-missing"])


def test_mount_points_reads_field_five(tmp_path: Path) -> None:
    mountinfo = tmp_path / "mountinfo"
    mountinfo.write_text(
        "22 1 8:1 / / rw,relatime shared:1 - ext4 /dev/sda1 rw\n"
        "95 22 0:44 /nix/store/aaa-dir /var/farm/nix/store/aaa-dir ro,relatime shared:1 - overlay overlay rw\n"
    )
    assert mount_points(mountinfo) == {"/", "/var/farm/nix/store/aaa-dir"}
