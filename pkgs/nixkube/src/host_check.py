# SPDX-License-Identifier: Apache-2.0

"""`nixkube-host-check`: the first init container of both node DaemonSets
when `nixkube.hostStore.enable` is on. Issue #25.

A DaemonSet selects nodes by label, and whether a host runs NixOS is not a
label. This sets one: `nixkube/host=nixos` on a node whose host has
/etc/NIXOS, the file NixOS activation writes.

- `--mode separate`, the DaemonSet with its own store: on a NixOS host,
  label the Node and wait. The DaemonSet excludes the label, so its
  controller deletes this pod, and the host-store DaemonSet takes the node.
  Nothing after this container runs, so the separate store is never filled.
- `--mode host`, the DaemonSet that mounts the host's /nix: on a host that
  is not NixOS, remove the label and wait, the same way round.

Otherwise it exits 0 and the pod starts. The wait ends at SIGTERM with 0,
because the pod is being deleted, not failing.

A leaf: kr8s and anyio, nothing from the daemon, so it starts in the time
an init container should take.
"""

from __future__ import annotations

import argparse
import os
import signal
import sys
from enum import StrEnum
from pathlib import Path
from typing import Final

import anyio
import kr8s.asyncio

HOST_LABEL: Final = "nixkube/host"
NIXOS: Final = "nixos"


class Mode(StrEnum):
    SEPARATE = "separate"
    HOST = "host"


def is_nixos(host_root: Path) -> bool:
    return (host_root / "etc/NIXOS").exists()


def label_change(mode: Mode, nixos: bool) -> dict[str, str | None] | None:
    """The label change that moves this node to the other DaemonSet, or
    None when this pod belongs here. `None` as a value removes the label."""
    if mode is Mode.SEPARATE and nixos:
        return {HOST_LABEL: NIXOS}
    if mode is Mode.HOST and not nixos:
        return {HOST_LABEL: None}
    return None


async def check(mode: Mode, host_root: Path, node_name: str) -> int:
    nixos = is_nixos(host_root)
    change = label_change(mode, nixos)
    if change is None:
        print(
            f"nixkube-host-check: {node_name} belongs to the {mode} DaemonSet",
            flush=True,
        )
        return 0
    node = await kr8s.asyncio.objects.Node.get(node_name)
    ((key, value),) = change.items()
    if value is None:
        await node.label(f"{key}-")
    else:
        await node.label({key: value})
    print(
        f"nixkube-host-check: {node_name} {'is' if nixos else 'is not'} NixOS;"
        f" set {change} and wait for the other DaemonSet to take the node",
        flush=True,
    )
    with anyio.open_signal_receiver(signal.SIGTERM, signal.SIGINT) as signals:
        async for _ in signals:
            return 0
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="nixkube-host-check", description=__doc__)
    parser.add_argument("--mode", type=Mode, choices=list(Mode), required=True)
    parser.add_argument(
        "--host-root",
        type=Path,
        default=Path(os.environ.get("HOST_ROOT", "/host")),
        help="where the host's / is mounted",
    )
    args = parser.parse_args(argv)
    node_name = os.environ.get("KUBE_NODE_NAME")
    if not node_name:
        print("nixkube-host-check: KUBE_NODE_NAME is unset", file=sys.stderr)
        return 2
    return anyio.run(check, args.mode, args.host_root, node_name)


if __name__ == "__main__":
    sys.exit(main())
