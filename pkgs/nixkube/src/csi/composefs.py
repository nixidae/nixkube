# SPDX-License-Identifier: Apache-2.0

"""A CSI volume as one composefs mount. Issues #68 and #25.

`src/composefs.py` says what the image holds; `nix/composefs.nix` is the
derivation that builds it. This is the node's side: write that derivation
from the tools on this node's PATH, build it -- which substitutes an image
pynixd or CI already built -- and mount it at the pod's target.

**Same tools, same derivation.** `expression` must give exactly the
derivation `nix/composefs.nix` gives for the same roots, or a node never
finds the image in the cache and builds its own. The nixkube wrapper names
the tools in its environment, the same packages `composefsImage` passes.

**nixkube mounts it, not mount.composefs.** On a kernel without overlayfs
data-only lower layers (before 6.5, and no backport), libcomposefs retries
with a plain lower directory, the mount succeeds, and the whole base
directory -- the node's whole store -- shows at the volume's root. And
measured on an image whose file sits on overlayfs, as the test guests'
store does: EROFS refuses the file, libcomposefs takes a loop device and
then fails with EBUSY, where the same three mounts by hand succeed. So
`compose` makes them itself, and the probe goes through it: an image of one
file over a base directory of one other file, accepted only when the root
holds exactly the one file.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import os
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

import anyio
import anyio.to_thread
import structlog

from ..constants import MNT_DETACH, MS_RDONLY, NIX_BUILD_TIMEOUT
from ..errors import MountError
from ..metrics import COMPOSEFS_AVAILABLE
from ..nix.build import _run_nix_build

logger = structlog.get_logger("nixkube.csi.composefs")

IMAGE = "image.cfs"
BASEDIR = "/nix/store"

_libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
_libc.mount.argtypes = [
    ctypes.c_char_p,
    ctypes.c_char_p,
    ctypes.c_char_p,
    ctypes.c_ulong,
    ctypes.c_char_p,
]
_libc.umount2.argtypes = [ctypes.c_char_p, ctypes.c_int]


@dataclass(frozen=True)
class Tools:
    nixkube: Path
    composefs: Path
    nix: Path


def tools() -> Tools:
    """The packages `nix/composefs.nix` takes as `tools`, exactly as the
    nixkube wrapper names them. Not looked up on PATH: a PATH that finds
    another nixkube -- a test's venv, measured -- writes another derivation,
    and the node never finds CI's image."""
    try:
        return Tools(
            nixkube=Path(os.environ["NIXKUBE_PACKAGE"]),
            composefs=Path(os.environ["NIXKUBE_COMPOSEFS"]),
            nix=Path(os.environ["NIXKUBE_NIX"]),
        )
    except KeyError as missing:
        raise FileNotFoundError(f"{missing} is not set: not run by the nixkube wrapper") from None


def expression(roots: set[Path], primary: Path | None, tools: Tools) -> str:
    """The derivation of `nix/composefs.nix`, as an expression a node can
    evaluate with nothing but store paths."""

    def path(p: Path) -> str:
        return f'(builtins.storePath "{p}")'

    def under(p: Path, rest: str = "") -> str:
        """A string with `p`'s context, as `"${p}/rest"` is in Nix."""
        return '"${' + path(p) + "}" + rest + '"'

    closure = " ".join(path(p) for p in sorted(roots))
    lines = [
        "derivation {",
        '  name = "nixkube-composefs";',
        "  system = builtins.currentSystem;",
        f"  builder = {under(tools.nixkube, '/bin/nixkube-composefs-build')};",
        "  __structuredAttrs = true;",
        f"  exportReferencesGraph.closure = map toString [ {closure} ];",
        f"  primary = {under(primary) if primary else '""'};",
        f"  nixStore = {under(tools.nix, '/bin/nix-store')};",
        f"  mkcomposefs = {under(tools.composefs, '/bin/mkcomposefs')};",
        "}",
    ]
    return "\n".join(lines) + "\n"


async def build_image(roots: set[Path], primary: Path | None, out_link: Path, extra_args: list[str]) -> Path:
    """The volume's image, substituted or built, rooted at `out_link`."""
    return await _run_nix_build(
        [
            "--impure",
            "--expr",
            expression(roots, primary, tools()),
            "--out-link",
            out_link,
            "--print-out-paths",
            *extra_args,
        ],
        timeout=NIX_BUILD_TIMEOUT,
        timeout_msg=f"composefs image timeout after {NIX_BUILD_TIMEOUT}s",
        error_msg="Failed to build the volume's composefs image",
        kind="composefs_image",
    )


# What libcomposefs passes: metacopy and redirect_dir, so the image's
# overlay xattrs point each file at its object in the data-only lower.
_OVERLAY = "metacopy=on,redirect_dir=on"


def overlay_options(erofs: Path, basedir: str, upper: Path | None) -> str:
    """The overlay over an image mounted at `erofs`. `::` makes `basedir` a
    data-only lower: its files are reachable through the image's redirects
    and never listed. A kernel before 6.5 refuses it, which is the point:
    libcomposefs would retry without it and show the whole base directory."""
    options = f"{_OVERLAY},lowerdir={erofs}::{basedir}"
    if upper is None:
        return options
    return f"{options},upperdir={upper / 'upper'},workdir={upper / 'work'}"


def _mount(source: str, target: Path, fstype: str, flags: int, data: str | None) -> None:
    ret = _libc.mount(
        source.encode(),
        os.fsencode(target),
        fstype.encode(),
        flags,
        data.encode() if data else None,
    )
    if ret != 0:
        err = ctypes.get_errno()
        raise OSError(err, f"mount {fstype} at {target}: {os.strerror(err)}")


def _mount_image(image: Path, at: Path) -> None:
    """EROFS straight from the file where the kernel can (6.12 and later,
    and not every filesystem under the file), through a loop device
    otherwise. util-linux's mount makes the loop device, and frees it when
    the last user goes."""
    try:
        _mount(str(image), at, "erofs", MS_RDONLY, None)
        return
    except OSError as error:
        logger.debug("erofs_from_file_refused", image=str(image), error=str(error))
    subprocess.run(
        ["mount", "-t", "erofs", "-o", "ro,loop", str(image), str(at)],
        check=True,
        capture_output=True,
        timeout=30,
    )


def compose(image: Path, target: Path, basedir: str, scratch: Path, upper: Path | None) -> None:
    """The volume at `target`: the image at `scratch`/erofs, the overlay over
    it at `target`, then the image's own mount detached. The overlay holds
    the image, so the volume is one mount, and the loop device, if any, goes
    with it."""
    erofs = scratch / "erofs"
    erofs.mkdir(parents=True, exist_ok=True)
    target.mkdir(parents=True, exist_ok=True)
    if upper is not None:
        (upper / "upper").mkdir(parents=True, exist_ok=True)
        (upper / "work").mkdir(parents=True, exist_ok=True)
    _mount_image(image, erofs)
    try:
        flags = MS_RDONLY if upper is None else 0
        _mount("overlay", target, "overlay", flags, overlay_options(erofs, basedir, upper))
    finally:
        _libc.umount2(os.fsencode(erofs), MNT_DETACH)


async def mount(image: Path, target: Path, readonly: bool, volume_root: Path) -> None:
    """One mount at `target`. A read-write volume writes to an upper
    directory in its volume root, never to the store."""
    try:
        await anyio.to_thread.run_sync(
            compose,
            image / IMAGE,
            target,
            BASEDIR,
            volume_root,
            None if readonly else volume_root,
        )
    except (OSError, subprocess.SubprocessError) as error:
        stderr = getattr(error, "stderr", b"") or b""
        raise MountError(
            f"composefs mount at {target} failed: {error}",
            logs=stderr.decode(errors="replace"),
        ) from error


# The probe's image: one file, backed by an object whose name differs, so a
# root that shows the object is a root that shows the base directory.
_PROBE_DUMP = "/ 0 40555 2 0 0 0 1.0 - - -\n/probe 7 100444 1 0 0 0 1.0 probe-object - -\n"
_PROBE_CONTENT = b"nixkube"


def probe_root_is_exact(listing: list[str], content: bytes) -> bool:
    """The probe's verdict on what the mounted root held."""
    return sorted(listing) == ["probe"] and content == _PROBE_CONTENT


_answer: bool | None = None


async def available() -> bool:
    """Whether this kernel mounts composefs as the volume layout needs it.
    Asked once per process: a kernel does not change under it. Two first
    publishes at once may both probe, which costs a mount and nothing else.
    CSI_COMPOSEFS and NRI_COMPOSEFS are the callers' to check."""
    global _answer
    if _answer is None:
        try:
            tools()
            ok, why = await anyio.to_thread.run_sync(_probe)
        except FileNotFoundError as error:
            ok, why = False, str(error)
        COMPOSEFS_AVAILABLE.set(1 if ok else 0)
        log = logger.info if ok else logger.warning
        log("composefs_probe", available=ok, why=why)
        _answer = ok
    return _answer


def _probe() -> tuple[bool, str]:
    with tempfile.TemporaryDirectory(prefix="nixkube-composefs-probe-") as tmp:
        work = Path(tmp)
        base = work / "objects"
        base.mkdir()
        (base / "probe-object").write_bytes(_PROBE_CONTENT)
        (work / "dump").write_text(_PROBE_DUMP)
        target = work / "mnt"
        target.mkdir()
        try:
            subprocess.run(
                ["mkcomposefs", "--from-file", str(work / "dump"), str(work / IMAGE)],
                check=True,
                capture_output=True,
                timeout=30,
            )
            compose(work / IMAGE, target, str(base), work, None)
        except (OSError, subprocess.SubprocessError) as error:
            stderr = getattr(error, "stderr", b"") or b""
            return False, f"{error}: {stderr.decode(errors='replace').strip()}"
        try:
            listing = os.listdir(target)
            content = (target / "probe").read_bytes() if "probe" in listing else b""
        finally:
            subprocess.run(["umount", str(target)], check=False, capture_output=True)
        if probe_root_is_exact(listing, content):
            return True, "one file, read through the image"
        return False, f"the root shows {sorted(listing)}: no data-only lower layers"
