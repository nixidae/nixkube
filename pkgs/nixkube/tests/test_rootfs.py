# SPDX-License-Identifier: Apache-2.0

from src.nri.mount import rootfs_of

BUNDLE = "/run/containerd/io.containerd.runtime.v2.task/k8s.io/abc"


def test_containerd_names_rootfs_in_the_bundle() -> None:
    assert rootfs_of(BUNDLE, {"root": {"path": "rootfs"}}) == f"{BUNDLE}/rootfs"


def test_crio_names_an_absolute_path_elsewhere() -> None:
    """Measured under CRI-O 1.36: the bundle holds no rootfs directory."""
    bundle = "/run/containers/storage/overlay-containers/abc/userdata"
    merged = "/var/lib/containers/storage/overlay/def/merged"
    assert rootfs_of(bundle, {"root": {"path": merged}}) == merged
