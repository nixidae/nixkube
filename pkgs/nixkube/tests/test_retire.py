# SPDX-License-Identifier: Apache-2.0

import os
from pathlib import Path

import pytest

from src.retire import Located, disk_usage, locate, overlay_lowers, overlay_users, unsafe, users

# The host's own view: / on 8:1, /var on its own filesystem 8:2.
HOST = """\
22 1 8:1 / / rw shared:1 - ext4 /dev/sda1 rw
23 22 8:2 / /var rw shared:2 - xfs /dev/sda2 rw
24 22 0:30 / /proc rw shared:3 - proc proc rw
"""


def test_locate_takes_the_longest_mount_point() -> None:
    assert locate("/var/lib/nix-csi", HOST) == Located("8:2", "/lib/nix-csi")


def test_locate_on_the_root_filesystem() -> None:
    assert locate("/srv/nix-csi", HOST) == Located("8:1", "/srv/nix-csi")


def test_locate_through_a_bind_mount() -> None:
    # /var/lib/kubelet is a bind of /data/kubelet on 8:2.
    host = HOST + "40 23 8:2 /data/kubelet /var/lib/kubelet rw shared:4 - xfs /dev/sda2 rw\n"
    assert locate("/var/lib/kubelet/nix-csi", host) == Located("8:2", "/data/kubelet/nix-csi")


OLD = Located("8:2", "/lib/nix-csi")


@pytest.mark.parametrize(
    ("line", "holds"),
    [
        # A pod's CSI volume: a bind of a directory in the old store.
        (
            "90 80 8:2 /lib/nix-csi/nix/var/nix-csi/volumes/v /pods/p/vol rw - xfs x rw",
            True,
        ),
        # The store itself, as the separate DaemonSet mounts it.
        ("91 80 8:2 /lib/nix-csi/nix /nix rw - xfs x rw", True),
        # One NRI farm bind of a store path.
        ("92 80 8:2 /lib/nix-csi/nix/store/aaa-p /nix/store/aaa-p ro - xfs x rw", True),
        # /var as a whole, or the host's / at /host: parents do not hold it.
        ("93 80 8:2 / /host/var rw - xfs x rw", False),
        ("94 80 8:2 /lib /host-lib rw - xfs x rw", False),
        # A sibling that shares the prefix.
        ("95 80 8:2 /lib/nix-csi-other /x rw - xfs x rw", False),
        # The same path on another filesystem.
        ("96 80 8:1 /lib/nix-csi/nix /y rw - ext4 x rw", False),
    ],
)
def test_only_a_mount_from_inside_the_old_store_holds_it(line: str, holds: bool) -> None:
    assert bool(users(OLD, [line + "\n"])) is holds


def test_users_reads_every_namespace() -> None:
    one = "90 80 8:2 / /host rw - xfs x rw\n"
    two = "91 80 8:2 /lib/nix-csi/nix /nix rw - xfs x rw\n"
    assert users(OLD, [one, two]) == ["/nix"]


@pytest.mark.parametrize(
    ("path", "refused"),
    [
        ("/", True),
        ("/nix", True),
        ("/nix/", True),
        ("/nix/var/nix-csi", True),
        ("relative/path", True),
        ("/var/lib/nix-csi", False),
        ("/nixkube", False),
    ],
)
def test_never_the_root_or_the_host_store(path: str, refused: bool) -> None:
    assert unsafe(path) is refused


def test_disk_usage_counts_a_hardlinked_inode_once(tmp_path: Path) -> None:
    (tmp_path / "a").write_bytes(b"x" * 8192)
    os.link(tmp_path / "a", tmp_path / "b")
    once = disk_usage(tmp_path)
    (tmp_path / "c").write_bytes(b"x" * 8192)
    assert disk_usage(tmp_path) > once
    # The link added nothing.
    assert once == disk_usage(tmp_path) - (tmp_path / "c").stat().st_blocks * 512


# A composefs CSI volume on sg1-n1, 2026-11-19, as the host's mountinfo
# shows it: the source is `overlay`, and only `lowerdir` names the volume.
COMPOSEFS = (
    "5310 105 0:414 / /var/lib/kubelet/pods/25a2/volumes/kubernetes.io~csi/nix/mount"
    " ro,relatime shared:2581 - overlay overlay"
    " ro,lowerdir=/nix/var/nix-csi/volumes/csi-6896/erofs::/nix/store,redirect_dir=on,metacopy=on"
)


def test_overlay_lowers_reads_data_only_layers() -> None:
    assert overlay_lowers(COMPOSEFS) == ["/nix/var/nix-csi/volumes/csi-6896/erofs", "/nix/store"]


def test_overlay_lowers_reads_the_append_options() -> None:
    line = "1 0 0:9 / /m ro - overlay overlay ro,lowerdir+=/nix/var/nix-csi/containers/c/erofs,datadir+=/nix/store"
    assert overlay_lowers(line) == ["/nix/var/nix-csi/containers/c/erofs", "/nix/store"]


def test_overlay_lowers_ignores_other_filesystems() -> None:
    assert overlay_lowers("91 80 8:2 /lib/nix-csi/nix /nix rw - xfs x rw,lowerdir=/x") == []


def test_a_composefs_volume_from_the_old_store_holds_it() -> None:
    old = {"/nix/var/nix-csi/volumes/csi-6896/erofs"}
    assert overlay_users(old.__contains__, [COMPOSEFS]) == [
        "/var/lib/kubelet/pods/25a2/volumes/kubernetes.io~csi/nix/mount"
    ]


def test_a_composefs_volume_from_the_host_store_does_not() -> None:
    # The same layout, made in the host's store: its directory is not in
    # the old one. `/nix/store` is in both and never counts.
    assert overlay_users({"/nix/store"}.__contains__, [COMPOSEFS]) == []
