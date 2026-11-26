# SPDX-License-Identifier: Apache-2.0

import subprocess
from pathlib import Path

import pytest

from src.constants import CSI_ROOT
from src.csi import cleanup, composefs
from src.csi.composefs import Tools, Verdict, expression, overlay_options, probe_root_is_exact

TOOLS = Tools(
    nixkube=Path("/nix/store/aaa-nixkube-wrapped"),
    composefs=Path("/nix/store/bbb-composefs-1.0.8"),
    nix=Path("/nix/store/ccc-nix-2.34"),
)


def test_the_expression_names_the_tools_and_sorts_the_roots() -> None:
    expr = expression(
        {Path("/nix/store/zzz-b"), Path("/nix/store/yyy-a")},
        Path("/nix/store/yyy-a"),
        TOOLS,
    )
    assert '"${(builtins.storePath "/nix/store/aaa-nixkube-wrapped")}/bin/nixkube-composefs-build"' in expr
    assert '"${(builtins.storePath "/nix/store/bbb-composefs-1.0.8")}/bin/mkcomposefs"' in expr
    assert '"${(builtins.storePath "/nix/store/ccc-nix-2.34")}/bin/nix-store"' in expr
    # Sorted, as nix/composefs.nix sorts them: the same set is one derivation.
    closure = next(line for line in expr.splitlines() if "exportReferencesGraph" in line)
    assert closure.index("yyy-a") < closure.index("zzz-b")
    assert 'primary = "${(builtins.storePath "/nix/store/yyy-a")}";' in expr


def test_without_a_primary_it_is_the_empty_string() -> None:
    expr = expression({Path("/nix/store/yyy-a")}, None, TOOLS)
    assert '  primary = "";' in expr.splitlines()


def test_the_store_is_a_data_only_lower(tmp_path: Path) -> None:
    ro = overlay_options(tmp_path / "erofs", "/nix/store", None)
    # `::`, not `:`: with one colon the store is an ordinary lower layer and
    # every store path shows at the volume's root.
    assert f"lowerdir={tmp_path}/erofs::/nix/store" in ro
    assert "metacopy=on" in ro and "redirect_dir=on" in ro
    assert "upperdir" not in ro


def test_read_write_writes_in_the_volume(tmp_path: Path) -> None:
    rw = overlay_options(tmp_path / "erofs", "/nix/store", tmp_path)
    assert f"upperdir={tmp_path}/upper" in rw and f"workdir={tmp_path}/work" in rw


@pytest.mark.parametrize(
    ("listing", "content", "accepted"),
    [
        (["probe"], b"nixkube", True),
        # libcomposefs's fallback without data-only lowers: the base
        # directory shows at the root. On a node that is the whole store.
        (["probe", "probe-object"], b"nixkube", False),
        (["probe"], b"", False),
        ([], b"", False),
    ],
)
def test_the_probe_accepts_only_the_one_file(listing: list[str], content: bytes, accepted: bool) -> None:
    assert probe_root_is_exact(listing, content) is accepted


@pytest.fixture
def csi_dirs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    gcroots, volumes = tmp_path / "gcroots", tmp_path / "volumes"
    gcroots.mkdir()
    volumes.mkdir()
    monkeypatch.setattr(cleanup, "CSI_GCROOTS", gcroots)
    monkeypatch.setattr(cleanup, "CSI_VOLUMES", volumes)
    monkeypatch.setattr(cleanup, "is_mount_source", lambda _path: False)
    return gcroots, volumes


def test_the_sweep_keeps_a_composefs_volume_whose_target_is_mounted(csi_dirs: tuple[Path, Path]) -> None:
    gcroots, volumes = csi_dirs
    for name, target in [("live", "/proc"), ("gone", "/no/such/target")]:
        (gcroots / name).mkdir()
        (volumes / name).mkdir()
        (volumes / name / cleanup.TARGET_FILE).write_text(target)

    # Neither is in kubelet's list: a restarting kubelet can leave it short.
    cleanup.cleanup_stale_entries(active_handles=set())

    assert (volumes / "live").exists(), "deleted a mounted volume's upper directory"
    assert (gcroots / "live").exists(), "unrooted a closure a mount still reads"
    assert not (volumes / "gone").exists()
    assert not (gcroots / "gone").exists()


def test_target_is_mounted_without_a_target_file_is_no(tmp_path: Path) -> None:
    assert cleanup.target_is_mounted(tmp_path) is False


@pytest.fixture
def probes(monkeypatch: pytest.MonkeyPatch) -> list[Verdict]:
    """The verdicts `_probe` gives, in turn; the test sees how many it asked for."""
    answers: list[Verdict] = []
    monkeypatch.setattr(composefs, "_answer", None)
    monkeypatch.setattr(composefs, "tools", lambda: TOOLS)

    def probe(where: Path) -> Verdict:
        # The store's filesystem, where a volume's image is: not the container's /tmp.
        assert where == CSI_ROOT
        return answers.pop(0)

    monkeypatch.setattr(composefs, "_probe", probe)
    return answers


@pytest.mark.asyncio
async def test_a_probe_that_could_not_set_up_is_asked_again(probes: list[Verdict]) -> None:
    # sg1-n1: the probe found no loop device, and the node refused every volume until its plugin restarted.
    probes.extend(
        [
            Verdict(ok=False, why="failed to set up loop device", final=False),
            Verdict(ok=True, why="one file, read through the image", final=True),
        ]
    )
    assert await composefs.available() is False
    assert "loop device" in composefs.reason()
    assert await composefs.available() is True
    assert await composefs.available() is True
    assert probes == []


@pytest.mark.asyncio
async def test_a_kernel_verdict_is_kept(probes: list[Verdict]) -> None:
    probes.append(Verdict(ok=False, why="the root shows ['probe', 'probe-object']", final=True))
    assert await composefs.available() is False
    assert await composefs.available() is False
    assert "probe-object" in composefs.reason()


def test_the_probe_image_is_on_the_store_filesystem(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """In the container's /tmp, an overlay, EROFS takes a loop device that no volume takes (sg1-n1)."""
    images: list[Path] = []

    def compose(image: Path, *_args: object) -> None:
        images.append(image)
        raise OSError(32, "failed to set up loop device")

    def mkcomposefs(argv: list[str], **_kwargs: object) -> subprocess.CompletedProcess[bytes]:
        Path(argv[-1]).write_bytes(b"")
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(composefs, "compose", compose)
    monkeypatch.setattr(subprocess, "run", mkcomposefs)
    verdict = composefs._probe(tmp_path / "state")
    assert verdict.final is False
    assert images and images[0].is_relative_to(tmp_path / "state")
