# SPDX-License-Identifier: Apache-2.0
"""The build sandbox's adjustment (`nri/sandbox.py`). The guest test's `nriprobe` phase shows that it applies."""

from __future__ import annotations

import errno
from pathlib import Path

import pytest

from nri import nri_pb2
from src.nri.sandbox import (
    NETWORK_ANNOTATION,
    SANDBOX_ANNOTATION,
    SandboxRequest,
    added_variables,
    sandbox_adjustment,
    sandbox_request,
    seccomp_policy,
)

FARM_SOURCE = "/var/lib/kubelet/pods/u/volumes/kubernetes.io~csi/nix/mount"
BUSYBOX = Path("/var/lib/nix-csi/nix/store/x-busybox/bin/busybox")


def _container() -> nri_pb2.Container:
    return nri_pb2.Container(
        name="runner",
        env=["out=/nix/store/a-x", "HOSTNAME=pod", "KUBERNETES_SERVICE_HOST=10.0.0.1", "PATH=/path-not-set"],
        mounts=[
            nri_pb2.Mount(destination="/nix", type="bind", source=FARM_SOURCE, options=["rbind", "rprivate", "rw"]),
            nri_pb2.Mount(destination="/build", type="bind", source="/b", options=["rbind"]),
        ],
    )


def test_request_reads_the_annotations() -> None:
    assert sandbox_request({}) is None
    assert sandbox_request({SANDBOX_ANNOTATION: "runner"}) == SandboxRequest("runner", network=False)
    assert sandbox_request({SANDBOX_ANNOTATION: "runner", NETWORK_ANNOTATION: "true"}) == SandboxRequest(
        "runner", network=True
    )


def test_only_added_variables_go() -> None:
    assert added_variables(_container().env) == ["HOSTNAME", "KUBERNETES_SERVICE_HOST"]


def test_sandboxed_build_gets_its_own_network_and_no_network_files() -> None:
    adjust = sandbox_adjustment(
        SandboxRequest("runner", network=False), _container(), machine="x86_64", busybox=BUSYBOX
    )
    assert {n.type for n in adjust.linux.namespaces} == {"network", "ipc", "uts", "cgroup"}
    assert all(n.path == "" for n in adjust.linux.namespaces)
    removed = {m.destination for m in adjust.mounts if m.destination.startswith("-")}
    assert {"-/nix", "-/etc/hosts", "-/etc/resolv.conf", "-/etc/hostname", "-/dev/termination-log"} <= removed
    assert [e.key for e in adjust.env] == ["-HOSTNAME", "-KUBERNETES_SERVICE_HOST"]
    assert [(h.path, list(h.args)) for h in adjust.hooks.create_container] == [
        (str(BUSYBOX), ["hostname", "localhost"])
    ]


def test_network_build_keeps_the_pods_network() -> None:
    adjust = sandbox_adjustment(SandboxRequest("runner", network=True), _container(), machine="x86_64", busybox=BUSYBOX)
    assert "network" not in {n.type for n in adjust.linux.namespaces}
    removed = {m.destination for m in adjust.mounts}
    assert "-/etc/resolv.conf" not in removed
    assert "-/etc/hosts" not in removed


def test_store_replaces_the_farm() -> None:
    adjust = sandbox_adjustment(
        SandboxRequest("runner", network=False), _container(), machine="x86_64", busybox=BUSYBOX
    )
    (store,) = [m for m in adjust.mounts if not m.destination.startswith("-")]
    assert store.destination == "/nix/store"
    assert store.source == f"{FARM_SOURCE}/store"
    # rbind: each input is a bind inside the farm's store.
    assert list(store.options) == ["rbind", "rprivate", "rw"]


def test_no_farm_is_refused() -> None:
    container = nri_pb2.Container(name="runner")
    with pytest.raises(ValueError, match="no /nix"):
        sandbox_adjustment(SandboxRequest("runner", network=False), container, machine="x86_64", busybox=BUSYBOX)


def test_filter_is_nixs() -> None:
    policy = seccomp_policy("aarch64")
    assert policy.default_action == "SCMP_ACT_ALLOW"
    assert list(policy.architectures) == ["SCMP_ARCH_AARCH64", "SCMP_ARCH_ARM"]
    chmods = [s for s in policy.syscalls if s.errno_ret.value == errno.EPERM]
    assert len(chmods) == 8
    (xattrs,) = [s for s in policy.syscalls if s.errno_ret.value == errno.ENOTSUP]
    assert "fsetxattr" in xattrs.names
    with pytest.raises(ValueError, match="riscv64"):
        seccomp_policy("riscv64")
