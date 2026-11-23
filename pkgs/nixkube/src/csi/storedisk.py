# SPDX-License-Identifier: Apache-2.0

"""A farm whose store is writable, on the pod's own emptyDir.

The volume attribute `storeVolume` names an emptyDir of the same pod. nixkube binds that emptyDir's
directory as the volume's `nix`, so what the pod writes to /nix lands on the emptyDir, and kubelet
counts it against the pod's ephemeral storage. The inputs stay read-only binds of the node's store.

Kubelet's disk walk (`pkg/volume/util/fs/fs.go`) skips only another st_dev, and a bind keeps its
source's st_dev. The binds attach at the volume root's path, in nixkube's namespace, so the walk of
the emptyDir's own path sees an empty stub for each input and the outputs.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..kube_docs import PodSpec

STORE_VOLUME_ATTRIBUTE = "storeVolume"
"""A volume attribute: the name of an emptyDir in the same pod, which holds the volume's writable `nix`."""


class StoreDiskError(ValueError):
    """A `storeVolume` that nixkube refuses. The message says why, for the pod's event."""


@dataclass(frozen=True, slots=True)
class StoreDisk:
    path: Path
    """The emptyDir's directory on the node."""
    uid: int
    gid: int


def store_disk(spec: PodSpec, name: str, target_path: Path, pod_uid: str) -> StoreDisk:
    """The emptyDir that `storeVolume: name` names, and who owns the store on it.

    Only an emptyDir in this pod's own spec: the attribute is the pod author's input, and nixkube binds
    the directory read-write as root. Its path comes from the publish target, kubelet's
    `<root>/pods/<uid>/volumes/kubernetes.io~csi/<volume>/mount`, so a kubelet root dir other than
    /var/lib/kubelet works."""
    if spec.get("hostUsers") is False:
        raise StoreDiskError("a storeVolume needs hostUsers: true: nixkube cannot idmap the store it writes")

    volume = next((v for v in spec.get("volumes", []) if v.get("name") == name), None)
    if volume is None or "emptyDir" not in volume:
        raise StoreDiskError(f"storeVolume {name!r} is not an emptyDir of this pod")
    if volume["emptyDir"].get("medium", "") != "":
        raise StoreDiskError(f"storeVolume {name!r} is an emptyDir in memory: a store wants the node's disk")

    volume_dir = target_path.parent
    volumes_dir = volume_dir.parent.parent
    pod_dir = volumes_dir.parent
    layout = (target_path.name, volume_dir.parent.name, volumes_dir.name, pod_dir.name, pod_dir.parent.name)
    if layout != ("mount", "kubernetes.io~csi", "volumes", pod_uid, "pods"):
        raise StoreDiskError(f"the publish target {target_path} is not kubelet's layout for pod {pod_uid}")

    uid, gid = store_owner(spec, volume_dir.name)
    return StoreDisk(path=volumes_dir / "kubernetes.io~empty-dir" / name, uid=uid, gid=gid)


def store_owner(spec: PodSpec, csi_volume: str) -> tuple[int, int]:
    """The uid and gid that every container mounting `csi_volume` runs as.

    Required, not defaulted: without `runAsUser` the image decides the user, and nixkube cannot see it.
    The gid is `runAsGroup`, or 0 when unset, as the runtime does."""
    pod_context = spec.get("securityContext", {})
    owners: set[tuple[int | None, int]] = set()
    for container in spec.get("containers", []) + spec.get("initContainers", []):
        if not any(m.get("name") == csi_volume for m in container.get("volumeMounts", [])):
            continue
        context = container.get("securityContext", {})
        uid = context.get("runAsUser", pod_context.get("runAsUser"))
        gid = context.get("runAsGroup", pod_context.get("runAsGroup", 0))
        owners.add((uid, gid))
    if not owners:
        raise StoreDiskError(f"no container mounts volume {csi_volume!r}")
    if len(owners) > 1:
        raise StoreDiskError(
            f"the containers that mount {csi_volume!r} run as different users: {sorted(owners, key=str)}"
        )
    ((uid, gid),) = owners
    if uid is None:
        raise StoreDiskError(f"set runAsUser on the containers that mount {csi_volume!r}: the store's owner")
    return uid, gid
