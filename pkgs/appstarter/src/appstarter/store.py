# SPDX-License-Identifier: Apache-2.0
"""The Nix operations `init` performs, and the check that they worked."""

import logging
import os
import subprocess
import sys
from collections import deque
from pathlib import Path

# How much of a failing fetch's stderr `StoreError` carries. The whole of it
# is already in the log; this is for a caller that only sees the exception.
_TAIL_LINES = 20

log = logging.getLogger("appstarter.store")

# This container's own `/nix/store`, opened as a store in its own right.
#
# **`appstarter init` runs no Nix daemon.** `local` is the store that reads
# `/nix/store` directly; `auto` and `daemon` both go through
# `/nix/var/nix/daemon-socket/socket`, which does not exist here. Anything
# this module hands to Nix as a store has to be a local one -- this, or the
# path of the node's own store.
LOCAL_STORE = "local"

# How long a fetch may run before the image's fallback is used instead.
#
# **`nix build` does not fail fast when a substituter cannot be reached.** It
# sits on the connect and retries, and `subprocess.run` with no timeout waits
# for as long as that takes. Seen on nixlab2, which is IPv6-only: an init
# container logged `fetching <cacheEnv> (pynixd: disabled)` and printed
# nothing for the next thirteen minutes, so the Pod stayed in `Init:0/1` and
# the fallback that exists for exactly this case never ran. `seed.run`
# catches `StoreError` and seeds from the image; with no bound here there was
# no `StoreError` to catch.
#
# Ten minutes, because a real cold fetch of a cache environment over a slow
# link is minutes and seeding from the image instead is a downgrade -- that
# node then runs behind until the next start. Settable, because how long is
# too long is a property of the link and not of this code.
BUILD_TIMEOUT = float(os.environ.get("APPSTARTER_BUILD_TIMEOUT", "600"))


class StoreError(RuntimeError):
    """A Nix operation failed, or left the store incomplete."""


def _run(*args: str, timeout: float | None = None) -> subprocess.CompletedProcess[str]:
    log.debug("running %s", " ".join(args))
    return subprocess.run(args, capture_output=True, text=True, check=False, timeout=timeout)


def _run_streaming(*args: str, timeout: float | None = None) -> subprocess.CompletedProcess[str]:
    """Run *args*, copying its stderr to ours as it arrives, and keeping it.

    **Capturing hides a fetch completely.** Nix writes its progress and its
    diagnosis to stderr, so capturing means the only thing anyone sees is
    what this module logs afterwards -- a fetch that takes a long time, or
    never returns, is one line and then silence. An init container that sat
    for 22 minutes on nixlab2 printed exactly one line, and the reason it
    was failing never left the process:

        warning: ignoring substitute for '...-cacheEnv' from
        'https://nixkube.cachix.org', as it's not signed by any of the keys
        in 'trusted-public-keys'

    Both, and not one: the log is what someone watching a Pod reads, and the
    tail is what `StoreError` carries to a caller that never saw the log.
    `_TAIL_LINES` is a bound, because a failing fetch can write a lot.
    """
    log.debug("running %s", " ".join(args))
    tail: deque[str] = deque(maxlen=_TAIL_LINES)
    with subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) as proc:
        if proc.stderr is None:
            raise RuntimeError("Popen was given stderr=PIPE but returned no stderr pipe")
        for line in proc.stderr:
            sys.stderr.write(line)
            tail.append(line)
        sys.stderr.flush()
        try:
            stdout, _ = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
            raise
    return subprocess.CompletedProcess(args, proc.returncode, stdout or "", "".join(tail))


def ping(store: str, timeout: float = 30.0) -> bool:
    """Whether `store` answers a `nix store ping`."""
    try:
        return _run("nix", "store", "ping", "--store", store, timeout=timeout).returncode == 0
    except subprocess.TimeoutExpired:
        log.warning("%s did not answer a ping within %.0fs", store, timeout)
        return False


def build(target: str, into: Path, substituters: list[str], out_link: Path) -> None:
    """Realise `target` in the store at `into`, from `substituters`.

    `--out-link` writes the pointer the main container starts from. Nix
    replaces a symlink by renaming over it, so a restart part way through this
    reads the previous version rather than half of this one.
    """
    try:
        result = _build(target, into, substituters, out_link)
    except subprocess.TimeoutExpired:
        raise StoreError(f"cannot get {target}: nothing answered within {BUILD_TIMEOUT:.0f}s") from None
    if result.returncode != 0:
        raise StoreError(f"cannot get {target}: {result.stderr.strip()}")


def _build(target: str, into: Path, substituters: list[str], out_link: Path) -> subprocess.CompletedProcess[str]:
    return _run_streaming(
        "nix",
        "build",
        "--extra-substituters",
        " ".join(substituters),
        "--max-jobs",
        "auto",
        # A node's store is a chroot store this process owns. The Nix sandbox
        # wants a second one inside it, which needs mounts this container
        # cannot make.
        "--option",
        "sandbox",
        "false",
        "--store",
        str(into),
        "--out-link",
        str(out_link),
        # Build what no substituter has, rather than failing. A node that can
        # build is slower than one that substitutes and faster than one that
        # does not start.
        "--fallback",
        target,
        timeout=BUILD_TIMEOUT,
    )


def copy(target: str, into: Path, out_link: Path) -> None:
    """Copy `target` out of this container's own store into the one at `into`.

    The fallback. It reads no substituter and cannot fail for a reason outside
    this pod, which is the whole point of it.

    **Both ends are local stores, and `--from` has to say so.** Without it
    `nix copy` reads the default store, which goes through the daemon socket
    -- and there is none here. Measured on nixkube's `test-qemu-ci-cache`:
    `cannot copy <path>: error: cannot connect to socket at
    '/nix/var/nix/daemon-socket/socket'`, on the one path that exists to
    survive a store the node cannot reach.
    """
    result = _run(
        "nix",
        "copy",
        "--no-check-sigs",
        "--from",
        LOCAL_STORE,
        "--to",
        str(into),
        target,
    )
    if result.returncode != 0:
        raise StoreError(f"cannot copy {target}: {result.stderr.strip()}")
    link(target, out_link)


def link(target: str, out_link: Path) -> None:
    """Point `out_link` at `target`, atomically.

    `os.replace` on a symlink is a rename, so a reader sees the old target or
    the new one and never a missing link. `nix build --out-link` does the same
    thing, and this is for the copy path, which has no `--out-link`.
    """
    out_link.parent.mkdir(parents=True, exist_ok=True)
    staging = out_link.with_name(f".{out_link.name}.new")
    staging.unlink(missing_ok=True)
    staging.symlink_to(target)
    os.replace(staging, out_link)


def verify(target: str, into: Path) -> None:
    """Check that every path of `target`'s closure is on disk under `into`.

    **`nix path-info --recursive` alone is not enough.** It answers from the
    database, so a path that is registered and absent passes -- which is issue
    #8: a node that starts a container which cannot exec out of
    `/nix/var/result/bin`, and reports a `$PATH` that means nothing. Nothing is
    hashed here; `nix store verify` would, and costs minutes on a closure this
    size at every node start.

    `-e` alone is wrong, because a store path can itself be a symlink to an
    absolute path that resolves only once the store is mounted at `/nix`.
    `nix-*-man` is one, and it is why `os.path.lexists` and not `Path.exists`.
    """
    result = _run("nix", "path-info", "--store", str(into), "--recursive", target)
    if result.returncode != 0:
        raise StoreError(f"cannot list {target}: {result.stderr.strip()}")

    missing = [path for path in result.stdout.split() if not os.path.lexists(f"{into}{path}")]
    if missing:
        for path in missing:
            log.error("registered and absent: %s", path)
        raise StoreError(f"{len(missing)} of {target}'s closure are absent from {into}")
