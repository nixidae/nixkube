# SPDX-License-Identifier: Apache-2.0
"""A container's /nix under a runtime that runs the pod in a VM, such as Kata.

The namespace mount in `mount.py` cannot reach a VM: the pid the hook reports
is the hypervisor's. So the container's tree goes in as an OCI bind mount at
/nix, which the runtime shares into the VM over virtio-fs, and the build
fills it from the node side while the container waits.

The wait is in the container and not in the hook. Kata runs the agent's
CreateContainer before any createRuntime hook (katautils/create.go, kata
3.32), and the agent refuses there a command it cannot find
(rustjail/container.rs). A command that lives in /nix is not there yet. So
the container's args become a static busybox from the tree, running `WAITER`,
which then execs the original args.

Measured under CRI-O and Kata 3.32: a file and a hardlink made on the node
after the pod started are visible in the VM at once.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import anyio
import anyio.to_thread

# In the container's /nix, beside store/ and var/.
STATE_DIR = ".nixkube"
READY = "ready"
FAILED = "failed"
ALIVE = "alive"
BUSYBOX = "busybox"
SCRIPT = "wait"

# The build task touches ALIVE with each progress heartbeat, every 10s; this
# gives up after three missed beats, as nri-wait does.
STALE_SECONDS = 30

# The state directory is the script's own, so a test can run it anywhere.
WAITER = f"""\
d="${{0%/*}}"
bb="$d/{BUSYBOX}"
while [ ! -e "$d/{READY}" ]; do
  if [ -e "$d/{FAILED}" ]; then
    "$bb" cat "$d/{FAILED}" >&2
    exit 1
  fi
  age=$(( $("$bb" date +%s) - $("$bb" stat -c %Y "$d/{ALIVE}") ))
  if [ "$age" -gt {STALE_SECONDS} ]; then
    echo "nixkube: the node stopped reporting on this container's /nix" >&2
    exit 1
  fi
  "$bb" sleep 1
done
exec "$@"
"""


def is_vm_runtime(handler: str, vm_handlers: frozenset[str]) -> bool:
    """Whether a pod's runtime handler runs it in a VM."""
    return handler in vm_handlers


def waiter_args(args: list[str]) -> list[str]:
    """The container's args, behind the waiter."""
    state = f"/nix/{STATE_DIR}"
    return [f"{state}/{BUSYBOX}", "sh", f"{state}/{SCRIPT}", *args]


def host_source(tree: Path, host_mount_path: Path) -> Path:
    """Where the node sees *tree*, a path inside this container's /nix."""
    return host_mount_path / tree.relative_to("/")


def refusal(nix_rw: bool, store_mounts: dict[Path, Path] | None) -> str | None:
    """Why a VM pod cannot have what it asked for, or None."""
    if nix_rw:
        # The tree is hardlinks into the node's store. A writable share would
        # let the guest change store inodes on the node.
        return "a writable /nix is not offered under a VM runtime: the tree is hardlinks into the node's store."
    if store_mounts:
        return "store path mounts are not offered under a VM runtime; mount the store with a CSI volume instead."
    return None


async def prepare_waiter(tree: Path, busybox: str) -> None:
    """Put the busybox and the script in *tree* before the container exists."""
    state = anyio.Path(tree / STATE_DIR)
    await state.mkdir(parents=True, exist_ok=True)
    await anyio.to_thread.run_sync(shutil.copy, busybox, str(state / BUSYBOX))
    await (state / SCRIPT).write_text(WAITER)
    await (state / ALIVE).touch()


async def mark(tree: Path, name: str, text: str = "") -> None:
    """Write one of READY, FAILED or ALIVE for the waiter to read."""
    await anyio.Path(tree / STATE_DIR / name).write_text(text)
