# SPDX-License-Identifier: Apache-2.0
"""Nix's build sandbox, made by the runtime for one container of a build pod. nixidae/nixidae#1560.

pynixd's build pod names its runner container in `SANDBOX_ANNOTATION`. That container's own process is the
builder, so the runtime makes what Nix's sandbox makes, on a node where an unprivileged user namespace fails:

  - new network, IPC, UTS and cgroup namespaces (an empty path asks the runtime for a new one);
  - Nix's system call filter in place of the container's;
  - no kubelet file and no service account token;
  - no variable that the kubelet or the runtime adds;
  - `/nix/store` of the pod's farm, without its database;
  - `/bin/sh`, the store path in `SHELL_ANNOTATION`, from the farm: central puts each store path the
    template names in the farm;
  - the host name `localhost`, which a createContainer hook sets: NRI has no field for it.

A fixed-output build reaches the network (`NETWORK_ANNOTATION`): it keeps the pod's network namespace,
`/etc/hosts` and `/etc/resolv.conf`, as Nix binds the host's. A container's own `/etc/hosts` mount, not the
kubelet's, stays in either case: pynixd's controller writes the sandbox's.

Measured on containerd 2.4.1 with NRI v0.12.3 (nixkube's `nriprobe` phase): each part applies. The variables
go only with NRI's fix for removing one (containerd/nri 193a7fdfa4, after v0.12.5).
"""

from __future__ import annotations

import errno
import stat
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import TYPE_CHECKING, Final

from nri import nri_pb2

if TYPE_CHECKING:
    from collections.abc import Collection, Iterable, Mapping, Sequence
    from pathlib import Path

SANDBOX_ANNOTATION: Final = "nixkube.nixidae.org/build-sandbox"
"""The name of the pod's container that is the builder."""
NETWORK_ANNOTATION: Final = "nixkube.nixidae.org/build-network"
"""`"true"`: the builder keeps the pod's network, as a fixed-output build does."""
SHELL_ANNOTATION: Final = "nixkube.nixidae.org/build-shell"
"""A file in the farm's store, which the builder gets at `/bin/sh`, as Nix's `sandbox-paths` give it."""

RUNNER_LABEL: Final = "nixkube/build-runner"
"""`"true"` on a node whose runtime runs this plugin; a runner build pod's template selects it. The plugin sets it
each time the runtime configures it, so a `"false"` set by hand lasts until then: for a runtime without NRI's
fix for removing a variable."""

STORE: Final = "/nix/store"
SHELL: Final = "/bin/sh"
FARM: Final = "/nix"
KUBELET_FILES: Final = ("/etc/hostname", "/dev/termination-log", "/var/run/secrets/kubernetes.io/serviceaccount")
NETWORK_FILES: Final = ("/etc/hosts", "/etc/resolv.conf")
KUBELET_HOSTS: Final = "/etc-hosts"
"""The end of the kubelet's `/etc/hosts` source, `pods/<uid>/etc-hosts`. The kubelet writes none for a container
that mounts its own, as pynixd's runner does with the sandbox's."""
ADDED_VARIABLES: Final = ("HOSTNAME",)
"""What the runtime adds besides the kubelet's `KUBERNETES_*`."""
ADDED_PREFIX: Final = "KUBERNETES_"
OWN_NAMESPACES: Final = ("ipc", "uts", "cgroup")

# `setupSeccomp` (`linux-derivation-builder.cc:79`), as pynixd's helper loads it (`pynixd_store.builder.child`).
CHMOD_CALLS: Final = (("chmod", 1), ("fchmod", 1), ("fchmodat", 2), ("fchmodat2", 2))
XATTR_CALLS: Final = (
    "listxattr",
    "llistxattr",
    "flistxattr",
    "getxattr",
    "lgetxattr",
    "fgetxattr",
    "setxattr",
    "lsetxattr",
    "fsetxattr",
)
ARCHITECTURES: Final = {
    "x86_64": ("SCMP_ARCH_X86_64", "SCMP_ARCH_X86", "SCMP_ARCH_X32"),
    "aarch64": ("SCMP_ARCH_AARCH64", "SCMP_ARCH_ARM"),
}


@dataclass(frozen=True, slots=True)
class SandboxRequest:
    """What a pod asks of the sandbox, from its annotations."""

    container: str
    network: bool
    shell: str | None = None


def sandbox_request(annotations: Mapping[str, str]) -> SandboxRequest | None:
    """The pod's sandbox request, or `None` when it asks for none."""
    container = annotations.get(SANDBOX_ANNOTATION)
    if not container:
        return None
    return SandboxRequest(
        container=container,
        network=annotations.get(NETWORK_ANNOTATION) == "true",
        shell=annotations.get(SHELL_ANNOTATION) or None,
    )


def seccomp_policy(machine: str) -> nri_pb2.LinuxSeccomp:
    """Nix's filter: no setuid or setgid file, and no extended attribute, which a NAR cannot hold."""
    if machine not in ARCHITECTURES:
        raise ValueError(f"the build sandbox has no system call filter for {machine}")
    chmods = [
        nri_pb2.LinuxSyscall(
            names=[call],
            action="SCMP_ACT_ERRNO",
            errno_ret=nri_pb2.OptionalUInt32(value=errno.EPERM),
            args=[nri_pb2.LinuxSeccompArg(index=index, value=bit, value_two=bit, op="SCMP_CMP_MASKED_EQ")],
        )
        for bit in (stat.S_ISUID, stat.S_ISGID)
        for call, index in CHMOD_CALLS
    ]
    xattrs = nri_pb2.LinuxSyscall(
        names=list(XATTR_CALLS), action="SCMP_ACT_ERRNO", errno_ret=nri_pb2.OptionalUInt32(value=errno.ENOTSUP)
    )
    return nri_pb2.LinuxSeccomp(
        default_action="SCMP_ACT_ALLOW", architectures=list(ARCHITECTURES[machine]), syscalls=[*chmods, xattrs]
    )


def _farm(mounts: Sequence[nri_pb2.Mount]) -> nri_pb2.Mount:
    farm = next((m for m in mounts if m.destination == FARM), None)
    if farm is None:
        raise ValueError(f"the builder container has no {FARM} to take {STORE} from")
    return farm


def _store_mount(farm: nri_pb2.Mount) -> nri_pb2.Mount:
    """`/nix/store` of the container's farm at `/nix`, with what it holds: each input is a bind in it."""
    options = [o for o in farm.options if o not in {"bind", "rbind"}]
    return nri_pb2.Mount(destination=STORE, type="bind", source=f"{farm.source}/store", options=["rbind", *options])


def _shell_mount(farm: nri_pb2.Mount, shell: str) -> nri_pb2.Mount:
    """*shell* at `/bin/sh`, read-only, from the farm's bind of its store path."""
    path = PurePosixPath(shell)
    if not path.is_relative_to(STORE) or len(path.parts) < 4 or ".." in path.parts:
        raise ValueError(f"the build shell {shell!r} is not a file in {STORE}")
    source = f"{farm.source}/{path.relative_to(FARM)}"
    return nri_pb2.Mount(destination=SHELL, type="bind", source=source, options=["bind", "ro", "rprivate"])


def network_files(mounts: Sequence[nri_pb2.Mount]) -> list[str]:
    """What a build without the network loses: the runtime's and the kubelet's files, not the container's own."""
    own = {m.destination for m in mounts if m.destination == "/etc/hosts" and not m.source.endswith(KUBELET_HOSTS)}
    return [path for path in NETWORK_FILES if path not in own]


SETUP_FILES: Final = ("/etc/passwd", "/etc/group", "/etc/hosts", "/etc/nsswitch.conf")
"""The builder's own files, which pynixd's controller writes before it lets the builder start (`etc_files`)."""


def setup_sources(mounts: Sequence[nri_pb2.Mount]) -> dict[str, str]:
    """The source of each of `SETUP_FILES` that the container mounts."""
    return {m.destination: m.source for m in mounts if m.destination in SETUP_FILES}


def unprepared(sources: Mapping[str, str], files: Collection[str]) -> str | None:
    """Why the builder must not start: a file of `SETUP_FILES` is no file, of the *sources* that are *files*.

    A failed postStart hook does not stop the kubelet: it kills the controller and starts the next container
    (`kuberuntime_container.go:327`, `kuberuntime_manager.go:1955`). For a subPath whose file is missing, the
    kubelet makes a directory. So a builder whose controller died before its check finds a directory here."""
    missing = [path for path in SETUP_FILES if sources.get(path) not in files]
    if not missing:
        return None
    return f"the builder's {', '.join(missing)} are not the files pynixd's controller writes before it starts"


def added_variables(env: Iterable[str]) -> list[str]:
    """The names of *env* that the kubelet or the runtime added: central writes every other one."""
    names = (entry.partition("=")[0] for entry in env)
    return sorted({name for name in names if name.startswith(ADDED_PREFIX) or name in ADDED_VARIABLES})


def sandbox_adjustment(
    request: SandboxRequest, container: nri_pb2.Container, *, machine: str, busybox: Path
) -> nri_pb2.ContainerAdjustment:
    """The adjustment that makes *container* Nix's sandbox. *busybox* is a static busybox, as the runtime sees it.

    A createContainer hook runs in the container's namespaces before the runtime drops its capabilities
    (measured), and its path resolves in the runtime's mount namespace.
    """
    removed = [*KUBELET_FILES, FARM, *(() if request.network else network_files(container.mounts))]
    namespaces = [*OWN_NAMESPACES, *(() if request.network else ("network",))]
    hooks = nri_pb2.Hooks(create_container=[nri_pb2.Hook(path=str(busybox), args=["hostname", "localhost"])])
    farm = _farm(container.mounts)
    added = [_store_mount(farm), *(() if request.shell is None else (_shell_mount(farm, request.shell),))]
    return nri_pb2.ContainerAdjustment(
        hooks=hooks,
        mounts=[*(nri_pb2.Mount(destination=f"-{path}") for path in removed), *added],
        env=[nri_pb2.KeyValue(key=f"-{name}") for name in added_variables(container.env)],
        linux=nri_pb2.LinuxContainerAdjustment(
            namespaces=[nri_pb2.LinuxNamespace(type=kind, path="") for kind in namespaces],
            seccomp_policy=seccomp_policy(machine),
        ),
    )
