"""An NRI plugin that makes one container into a sandbox like Nix's, for phases/nriprobe.py (nixidae/nixidae#1560).

It changes the container `rewritten` of a pod labelled `nixkube.test/nri-probe`, and nothing else:
  - a new network, IPC, UTS and cgroup namespace (an empty path asks the runtime for a new one)
  - a seccomp policy that refuses a setuid or setgid chmod with EPERM, as Nix's filter does
  - no kubelet file mounts and no service account token
  - no `KUBERNETES_*` variables

It prints, once per such container, the mounts and variables the runtime offered, as one JSON line.
"""

from __future__ import annotations

import json
import stat
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Final

import anyio
from grpclib_nri.plugin import NriPlugin
from grpclib_nri.server import NriServer
from nri import nri_pb2

if TYPE_CHECKING:
    from grpclib.server import Stream

LABEL: Final = "nixkube.test/nri-probe"
READY: Final = "/run/nri-probe.ready"
CONTAINER: Final = "rewritten"
NAMESPACES: Final = ("network", "ipc", "uts", "cgroup")
MOUNTS: Final = (
    "/etc/hosts",
    "/etc/hostname",
    "/etc/resolv.conf",
    "/dev/termination-log",
    "/var/run/secrets/kubernetes.io/serviceaccount",
)
ENV: Final = ("KUBERNETES_SERVICE_HOST", "KUBERNETES_SERVICE_PORT", "KUBERNETES_PORT")
EPERM: Final = 1
# The mode argument of each call, as Nix's `setupSeccomp` names them.
CHMODS: Final = {"chmod": 1, "fchmod": 1, "fchmodat": 2, "fchmodat2": 2}


def _seccomp() -> nri_pb2.LinuxSeccomp:
    rules = [
        nri_pb2.LinuxSyscall(
            names=[name],
            action="SCMP_ACT_ERRNO",
            errno_ret=nri_pb2.OptionalUInt32(value=EPERM),
            args=[nri_pb2.LinuxSeccompArg(index=index, value=bit, value_two=bit, op="SCMP_CMP_MASKED_EQ")],
        )
        for name, index in CHMODS.items()
        for bit in (stat.S_ISUID, stat.S_ISGID)
    ]
    return nri_pb2.LinuxSeccomp(
        default_action="SCMP_ACT_ALLOW",
        architectures=["SCMP_ARCH_X86_64", "SCMP_ARCH_AARCH64"],
        syscalls=rules,
    )


def adjustment() -> nri_pb2.ContainerAdjustment:
    return nri_pb2.ContainerAdjustment(
        mounts=[nri_pb2.Mount(destination=f"-{path}") for path in MOUNTS],
        env=[nri_pb2.KeyValue(key=f"-{key}") for key in ENV],
        linux=nri_pb2.LinuxContainerAdjustment(
            namespaces=[nri_pb2.LinuxNamespace(type=kind, path="") for kind in NAMESPACES],
            seccomp_policy=_seccomp(),
        ),
    )


class Probe(NriPlugin):
    def __init__(self) -> None:
        super().__init__([nri_pb2.Event.CREATE_CONTAINER])

    async def CreateContainer(
        self, stream: Stream[nri_pb2.CreateContainerRequest, nri_pb2.CreateContainerResponse]
    ) -> None:
        req = await stream.recv_message()
        if req is None:
            raise RuntimeError("CreateContainer received no request")
        if LABEL not in req.pod.labels or req.container.name != CONTAINER:
            await stream.send_message(nri_pb2.CreateContainerResponse())
            return
        offered = {
            "pod": req.pod.name,
            "mounts": [m.destination for m in req.container.mounts],
            "env": sorted(e.partition("=")[0] for e in req.container.env),
            "namespaces": {n.type: n.path for n in req.container.linux.namespaces},
        }
        sys.stdout.write(json.dumps(offered) + "\n")
        sys.stdout.flush()
        await stream.send_message(nri_pb2.CreateContainerResponse(adjust=adjustment()))

    async def Synchronize(self, stream: Stream[nri_pb2.SynchronizeRequest, nri_pb2.SynchronizeResponse]) -> None:
        # The runtime synchronizes a plugin once it registers, and sends it no container before that.
        await super().Synchronize(stream)
        await anyio.Path(READY).touch()

    async def StateChange(self, stream: Stream[nri_pb2.StateChangeEvent, nri_pb2.Empty]) -> None:
        await stream.recv_message()
        await stream.send_message(nri_pb2.Empty())


async def main() -> None:
    server = NriServer(Probe(), socket_path=Path("/var/run/nri/nri.sock"), plugin_name="nri-probe", plugin_idx="90")
    try:
        await server.start()
    finally:
        await server.close()


if __name__ == "__main__":
    anyio.run(main)
