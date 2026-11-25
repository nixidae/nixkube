# SPDX-License-Identifier: Apache-2.0
"""The build sandbox's adjustment (`nri/sandbox.py`). The guest test's `nriprobe` phase shows that it applies."""

from __future__ import annotations

import errno
from pathlib import Path

import pytest

from nri import nri_pb2
from src.nri.sandbox import (
    FARM_ANNOTATION,
    NETWORK_ANNOTATION,
    SANDBOX_ANNOTATION,
    SETUP_FILES,
    SHELL_ANNOTATION,
    SandboxRequest,
    added_variables,
    farm_source,
    network_files,
    sandbox_adjustment,
    sandbox_request,
    seccomp_policy,
    setup_sources,
    unprepared,
)

PODS = Path("/var/lib/kubelet/pods")
FARM_SOURCE = "/var/lib/kubelet/pods/u/volumes/kubernetes.io~csi/nix/mount"
BUSYBOX = Path("/var/lib/nix-csi/nix/store/x-busybox/bin/busybox")


def _container(*mounts: nri_pb2.Mount) -> nri_pb2.Container:
    """A runner as the template makes it: no `/nix` of its own."""
    return nri_pb2.Container(
        name="runner",
        env=["out=/nix/store/a-x", "HOSTNAME=pod", "KUBERNETES_SERVICE_HOST=10.0.0.1", "PATH=/path-not-set"],
        mounts=[nri_pb2.Mount(destination="/build", type="bind", source="/b", options=["rbind"]), *mounts],
    )


def _adjust(request: SandboxRequest, container: nri_pb2.Container | None = None) -> nri_pb2.ContainerAdjustment:
    return sandbox_adjustment(request, container or _container(), machine="x86_64", busybox=BUSYBOX, farm=FARM_SOURCE)


def test_request_reads_the_annotations() -> None:
    assert sandbox_request({}) is None
    assert sandbox_request({SANDBOX_ANNOTATION: "runner"}) == SandboxRequest("runner", network=False)
    assert sandbox_request({SANDBOX_ANNOTATION: "runner", NETWORK_ANNOTATION: "true"}) == SandboxRequest(
        "runner", network=True
    )
    assert sandbox_request({SANDBOX_ANNOTATION: "runner", FARM_ANNOTATION: "nix"}) == SandboxRequest(
        "runner", network=False, farm="nix"
    )


def test_only_added_variables_go() -> None:
    assert added_variables(_container().env) == ["HOSTNAME", "KUBERNETES_SERVICE_HOST"]


def test_sandboxed_build_gets_its_own_network_and_no_network_files() -> None:
    adjust = _adjust(SandboxRequest("runner", network=False))
    assert {n.type for n in adjust.linux.namespaces} == {"network", "ipc", "uts", "cgroup"}
    assert all(n.path == "" for n in adjust.linux.namespaces)
    removed = {m.destination for m in adjust.mounts if m.destination.startswith("-")}
    assert {"-/etc/hosts", "-/etc/resolv.conf", "-/etc/hostname", "-/dev/termination-log"} <= removed
    assert [e.key for e in adjust.env] == ["-HOSTNAME", "-KUBERNETES_SERVICE_HOST"]
    assert [(h.path, list(h.args)) for h in adjust.hooks.create_container] == [
        (str(BUSYBOX), ["hostname", "localhost"])
    ]


def test_network_build_keeps_the_pods_network() -> None:
    adjust = _adjust(SandboxRequest("runner", network=True))
    assert "network" not in {n.type for n in adjust.linux.namespaces}
    removed = {m.destination for m in adjust.mounts}
    assert "-/etc/resolv.conf" not in removed
    assert "-/etc/hosts" not in removed


def test_a_sandboxed_build_keeps_its_own_hosts_and_loses_the_kubelets() -> None:
    kubelet = nri_pb2.Mount(destination="/etc/hosts", source="/var/lib/kubelet/pods/u/etc-hosts")
    own = nri_pb2.Mount(destination="/etc/hosts", source="/var/lib/kubelet/pods/u/volume-subpaths/build/runner/2")

    assert network_files([kubelet]) == ["/etc/hosts", "/etc/resolv.conf"]
    assert network_files([own]) == ["/etc/resolv.conf"]


def test_the_store_comes_from_the_pods_farm() -> None:
    adjust = _adjust(SandboxRequest("runner", network=False))
    (store,) = [m for m in adjust.mounts if not m.destination.startswith("-")]
    assert store.destination == "/nix/store"
    assert store.source == f"{FARM_SOURCE}/store"
    # rbind: each input is a bind inside the farm's store.
    assert list(store.options) == ["rbind", "rprivate", "rw"]
    assert "-/nix" not in {m.destination for m in adjust.mounts}


def test_a_farm_the_builder_mounts_itself_goes() -> None:
    own = nri_pb2.Mount(destination="/nix", type="bind", source=FARM_SOURCE, options=["rbind"])
    adjust = _adjust(SandboxRequest("runner", network=False), _container(own))
    assert "-/nix" in {m.destination for m in adjust.mounts}


def test_the_farm_is_the_pods_csi_volume_on_the_node() -> None:
    assert farm_source(PODS, "u", "nix") == FARM_SOURCE
    for uid, volume in (("u", None), ("u", "../etc"), ("u", "a/b"), ("../x", "nix"), ("", "nix")):
        with pytest.raises(ValueError, match=r"farm volume|uid"):
            farm_source(PODS, uid, volume)


def test_the_build_shell_comes_from_the_farm() -> None:
    shell = "/nix/store/x-busybox/bin/busybox"
    request = sandbox_request({SANDBOX_ANNOTATION: "runner", SHELL_ANNOTATION: shell})
    assert request is not None

    adjust = _adjust(request)

    (sh,) = [m for m in adjust.mounts if m.destination == "/bin/sh"]
    assert sh.source == f"{FARM_SOURCE}/store/x-busybox/bin/busybox"
    assert "ro" in sh.options
    for bad in ("/bin/busybox", "/nix/store", "/nix/store/../etc/passwd"):
        with pytest.raises(ValueError, match="not a file in /nix/store"):
            _adjust(SandboxRequest("runner", network=False, shell=bad))


def test_a_builder_starts_only_on_the_files_its_controller_wrote() -> None:
    subpaths = "/var/lib/kubelet/pods/u/volume-subpaths/build/runner"
    mounts = [nri_pb2.Mount(destination=path, source=f"{subpaths}/{index}") for index, path in enumerate(SETUP_FILES)]
    sources = setup_sources(_container(*mounts).mounts)

    assert set(sources) == set(SETUP_FILES)
    assert unprepared(sources, set(sources.values())) is None
    # The kubelet made a directory for a file the controller did not write.
    refused = unprepared(sources, set(sources.values()) - {sources["/etc/hosts"]})
    assert refused is not None and "/etc/hosts" in refused
    # A builder that mounts none of them is no runner the controller prepared.
    assert unprepared(setup_sources(_container().mounts), set()) is not None


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
