# SPDX-License-Identifier: Apache-2.0

"""`nixkube-userns-probe`: whether a build pod on this node gets Nix's sandbox. nixidae/nixidae#1560.

A container of the node DaemonSet while build pods are on. It runs with the security context of a build
pod's sandbox container: that uid, no capability, the build pods' seccomp profile, and the runtime's default
AppArmor profile. So it meets what that container meets, not what the host allows a process outside a
container. On Ubuntu that differs: the host restricts unconfined processes, and containerd's profile does
not mediate user namespaces (ABI 3.0).

It makes the namespaces the sandbox helper makes, maps its uid, and mounts and names a host inside them.
Then it labels the Node `nixkube/userns` `true` or `false`, with the error in the annotation
`nixkube/userns-error`, and waits for SIGTERM. A build pod template selects on the label.

A leaf: kr8s and anyio, nothing from the daemon.
"""

from __future__ import annotations

import ctypes
import os
import signal
import socket
import sys
from typing import Final

import anyio
import kr8s.asyncio

USERNS_LABEL: Final = "nixkube/userns"
ERROR_ANNOTATION: Final = "nixkube/userns-error"

_NAMESPACES: Final = os.CLONE_NEWUSER | os.CLONE_NEWNS | os.CLONE_NEWNET | os.CLONE_NEWIPC | os.CLONE_NEWUTS
_MS_REC: Final = 0x4000
_MS_PRIVATE: Final = 0x40000


def _in_namespaces() -> None:
    """Fail as the sandbox helper would. Ubuntu's restriction lets an unconfined process make the user
    namespace and takes its capabilities inside, so the mount and the host name test those."""
    uid, gid = os.getuid(), os.getgid()
    os.unshare(_NAMESPACES)
    with open("/proc/self/setgroups", "w") as setgroups:
        setgroups.write("deny")
    with open("/proc/self/uid_map", "w") as uid_map:
        uid_map.write(f"0 {uid} 1")
    with open("/proc/self/gid_map", "w") as gid_map:
        gid_map.write(f"0 {gid} 1")
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.mount(None, b"/", None, _MS_REC | _MS_PRIVATE, None) != 0:
        errno = ctypes.get_errno()
        raise OSError(errno, f"mount --make-rprivate /: {os.strerror(errno)}")
    socket.sethostname("nixkube-userns-probe")


def probe() -> str | None:
    """`None` when the namespaces work, else what failed. In a child, so this process keeps its own."""
    read, write = os.pipe()
    pid = os.fork()
    if pid == 0:
        os.close(read)
        code = 0
        try:
            _in_namespaces()
        except OSError as error:
            os.write(write, str(error).encode())
            code = 1
        os._exit(code)
    os.close(write)
    with os.fdopen(read, "rb") as pipe:
        message = pipe.read().decode(errors="replace")
    _, status = os.waitpid(pid, 0)
    if os.waitstatus_to_exitcode(status) == 0:
        return None
    return message or f"the probe exited with status {status}"


def node_patch(error: str | None) -> dict[str, dict[str, dict[str, str | None]]]:
    """The merge patch that records *error* on the Node; `None` as a value removes the key."""
    return {
        "metadata": {
            "labels": {USERNS_LABEL: "false" if error else "true"},
            "annotations": {ERROR_ANNOTATION: error},
        }
    }


async def record(node_name: str, error: str | None) -> None:
    node = await kr8s.asyncio.objects.Node.get(node_name)
    await node.patch(node_patch(error))
    print(f"nixkube-userns-probe: {node_name}: {error or 'user namespaces work'}", flush=True)
    with anyio.open_signal_receiver(signal.SIGTERM, signal.SIGINT) as signals:
        async for _ in signals:
            return


def main() -> int:
    node_name = os.environ.get("KUBE_NODE_NAME")
    if not node_name:
        print("nixkube-userns-probe: KUBE_NODE_NAME is unset", file=sys.stderr)
        return 2
    # Before the event loop starts any thread: the fork copies only this one.
    error = probe()
    anyio.run(record, node_name, error)
    return 0


if __name__ == "__main__":
    sys.exit(main())
