# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from src.csi.storedisk import StoreDisk, StoreDiskError, store_disk

if TYPE_CHECKING:
    from src.kube_docs import PodSpec

UID = "0f3c"
TARGET = Path(f"/var/lib/kubelet/pods/{UID}/volumes/kubernetes.io~csi/nix/mount")


def spec(**changes: object) -> PodSpec:
    doc: PodSpec = {
        "securityContext": {"runAsUser": 1000},
        "containers": [
            {"name": "sandbox", "volumeMounts": [{"name": "nix", "mountPath": "/nix"}]},
            {"name": "other", "securityContext": {"runAsUser": 5}, "volumeMounts": []},
        ],
        "volumes": [{"name": "nix", "csi": {"driver": "nixkube"}}, {"name": "store", "emptyDir": {}}],
    }
    doc.update(changes)  # type: ignore[typeddict-item]  # a test's partial override
    return doc


def test_the_store_is_the_named_emptydir_beside_the_publish_target() -> None:
    assert store_disk(spec(), "store", TARGET, UID) == StoreDisk(
        path=Path(f"/var/lib/kubelet/pods/{UID}/volumes/kubernetes.io~empty-dir/store"), uid=1000, gid=0
    )


def test_a_kubelet_root_elsewhere_moves_the_store_with_it() -> None:
    target = Path(f"/data/kubelet/pods/{UID}/volumes/kubernetes.io~csi/nix/mount")
    assert store_disk(spec(), "store", target, UID).path.parts[:3] == ("/", "data", "kubelet")


@pytest.mark.parametrize(
    ("name", "volumes", "message"),
    [
        ("missing", None, "not an emptyDir"),
        ("nix", None, "not an emptyDir"),
        ("../../../../etc", None, "not an emptyDir"),
        ("store", [{"name": "store", "emptyDir": {"medium": "Memory"}}], "in memory"),
    ],
)
def test_only_an_emptydir_on_disk_of_this_pod_is_a_store(name: str, volumes: object, message: str) -> None:
    doc = spec() if volumes is None else spec(volumes=volumes)
    with pytest.raises(StoreDiskError, match=message):
        store_disk(doc, name, TARGET, UID)


def test_a_target_of_another_pod_is_refused() -> None:
    with pytest.raises(StoreDiskError, match="not kubelet's layout"):
        store_disk(spec(), "store", TARGET, "another-uid")


def test_a_user_namespace_is_refused() -> None:
    with pytest.raises(StoreDiskError, match="hostUsers"):
        store_disk(spec(hostUsers=False), "store", TARGET, UID)


def test_the_owner_is_the_container_s_own_user_over_the_pod_s() -> None:
    doc = spec(
        containers=[
            {
                "name": "sandbox",
                "securityContext": {"runAsUser": 30000, "runAsGroup": 30000},
                "volumeMounts": [{"name": "nix", "mountPath": "/nix"}],
            }
        ]
    )
    disk = store_disk(doc, "store", TARGET, UID)
    assert (disk.uid, disk.gid) == (30000, 30000)


def test_no_known_user_is_refused() -> None:
    with pytest.raises(StoreDiskError, match="set runAsUser"):
        store_disk(spec(securityContext={}), "store", TARGET, UID)


def test_two_users_on_one_store_are_refused() -> None:
    doc = spec(
        initContainers=[
            {
                "name": "init",
                "securityContext": {"runAsUser": 0},
                "volumeMounts": [{"name": "nix", "mountPath": "/nix"}],
            }
        ]
    )
    with pytest.raises(StoreDiskError, match="different users"):
        store_disk(doc, "store", TARGET, UID)
