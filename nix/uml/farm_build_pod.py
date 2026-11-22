"""The daemon container of a pynixd build pod over a nixkube farm (phases/buildpods.py). nixidae/nixidae#1560.

The pod's `/nix` is a CSI volume with `presentation: farm`: one read-only bind per closure path, and the
closure's database. pynixd reads it as the lower store of its own store in `/build`, with no Nix daemon,
and builds in the sibling sandbox container. Nothing reaches the pod from a store elsewhere: a build
whose input is busybox runs only if the farm's path is valid here and binds into the sandbox.

Two steps. A pynixd daemon serves `nix-instantiate`, which writes the derivation into the pod's store.
Then this process builds it as `pynixd build-one` does, in process (`LocalDBStore.build_here`). A Nix
client builds nothing here: on a Unix socket it reads a `.drv` and an input from one store directory
(`UDSRemoteStore` is a `LocalFSStore`), and this pod keeps them in two. Measured: "store path
'…-farm-built.drv' does not exist" without `?root=`, and "opening file /build/nix/store" with it.

argv: this program's store directory (`farm-build-pod.nix`), which names the rest.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import anyio
from nix_daemon_protocol.build_derivation import BuildDerivationRequest
from nix_daemon_protocol.ids import LOCAL_STORE_ID
from nix_daemon_protocol.logs import LogNext
from nix_daemon_protocol.store_path import StorePath
from pynixd.build_result import result_succeeded
from pynixd.cli.base import load_settings
from pynixd.instance import Server
from pynixd.serde.protocol import Verbosity
from pynixd.store.local_db import LocalDBStore
from pynixd_store.drv_parser import to_basic_derivation

SOCKET = Path("/build/pynixd.sock")
SOCKET_SECONDS = 120.0


def instantiate(root: Path) -> str | None:
    """The derivation's path, written into the pod's store by a pynixd daemon; None when it failed."""
    with Path("/build/pynixd.log").open("wb") as log:
        daemon = subprocess.Popen([str(root / "pynixd" / "bin" / "pynixd"), "daemon"], stdout=log, stderr=log)
        try:
            # The daemon writes no ready line a client could wait on: the socket is the signal.
            deadline = time.monotonic() + SOCKET_SECONDS
            while not SOCKET.exists():
                if daemon.poll() is not None or time.monotonic() > deadline:
                    print(f"pynixd did not listen (exit {daemon.returncode})")
                    return None
                time.sleep(0.2)
            busybox = (root / "busybox").resolve()
            expression = (
                'derivation { name = "farm-built"; system = builtins.currentSystem;'
                f' builder = "${{builtins.storePath "{busybox}"}}/bin/sh";'
                ' args = [ "-c" "echo built-from-the-farm > $out" ]; }'
            )
            done = subprocess.run(
                ["nix-instantiate", "--expr", expression],
                env={**os.environ, "NIX_REMOTE": f"unix://{SOCKET}"},
                capture_output=True,
                text=True,
                check=False,
            )
            print(f"instantiate exit {done.returncode}: {done.stdout.strip()} {done.stderr.strip()}")
            return done.stdout.strip() if done.returncode == 0 else None
        finally:
            daemon.terminate()
            daemon.wait(timeout=30)


async def show(message: object) -> None:
    if isinstance(message, LogNext):
        print(message.text.rstrip("\n"), flush=True)


async def build(drv_path: str) -> int:
    settings = load_settings()
    async with Server(stores=settings.to_stores(), settings=settings) as server:
        scheduler = server.ctx.scheduler
        if scheduler is not None and scheduler.stores_opened is not None:
            await scheduler.stores_opened.wait()
        local = server.ctx.stores[LOCAL_STORE_ID]
        if not isinstance(local, LocalDBStore):
            print("the local store is not one pynixd builds into")
            return 1
        parsed = await local.engine.read_derivation(drv_path)
        if parsed is None:
            print(f"pynixd cannot read {drv_path}")
            return 1
        request = BuildDerivationRequest(
            drv_path=StorePath(drv_path),
            derivation=await to_basic_derivation(parsed, local.layout),
            build_mode=0,
        )
        print(f"inputs: {sorted(str(p) for p in request.derivation.input_srcs)}")
        build_settings = await local.local_build_settings(request, None)
        response = await local.build_here(request, build_settings, show, Verbosity.INFO)
        print(f"build: status {response.result.status} {response.result.error_msg}")
        if not result_succeeded(response.result):
            return 1
        for out in request.derivation.output_paths().values():
            print(f"output says: {(Path('/build/nix/store') / out.name).read_text().strip()}")
        return 0


def main() -> int:
    root = Path(sys.argv[1])
    # Nix takes HOME only if it owns it, and asks passwd else; the image has no entry for this uid, and the
    # emptyDir is root's. Measured: "cannot determine user's home directory".
    home = Path("/build/home")
    home.mkdir(exist_ok=True)
    os.environ.update(
        {
            "PYNIXD_CONFIG": str(root / "pynixd.json"),
            "PATH": f"{root / 'nix' / 'bin'}:{root / 'busybox' / 'bin'}",
            "HOME": str(home),
        }
    )
    try:
        drv_path = instantiate(root)
        return 1 if drv_path is None else anyio.run(build, drv_path)
    finally:
        for line in Path("/build/pynixd.log").read_text(errors="replace").splitlines():
            if "lower_store" in line or "error" in line.lower():
                print(f"pynixd: {line}")


if __name__ == "__main__":
    sys.exit(main())
