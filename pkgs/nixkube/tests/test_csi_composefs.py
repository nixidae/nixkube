# SPDX-License-Identifier: Apache-2.0

from pathlib import Path

import pytest

from src.csi import cleanup
from src.csi.composefs import Tools, expression, overlay_options, probe_root_is_exact

TOOLS = Tools(
    nixkube=Path("/nix/store/aaa-nixkube-wrapped"),
    composefs=Path("/nix/store/bbb-composefs-1.0.8"),
    nix=Path("/nix/store/ccc-nix-2.34"),
)


def test_the_expression_names_the_tools_and_sorts_the_roots():
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


def test_without_a_primary_it_is_the_empty_string():
    expr = expression({Path("/nix/store/yyy-a")}, None, TOOLS)
    assert '  primary = "";' in expr.splitlines()


def test_the_store_is_a_data_only_lower(tmp_path: Path):
    ro = overlay_options(tmp_path / "erofs", "/nix/store", None)
    # `::`, not `:`: with one colon the store is an ordinary lower layer and
    # every store path shows at the volume's root.
    assert f"lowerdir={tmp_path}/erofs::/nix/store" in ro
    assert "metacopy=on" in ro and "redirect_dir=on" in ro
    assert "upperdir" not in ro


def test_read_write_writes_in_the_volume(tmp_path: Path):
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
def test_the_probe_accepts_only_the_one_file(listing, content, accepted):
    assert probe_root_is_exact(listing, content) is accepted


@pytest.fixture
def csi_dirs(tmp_path: Path, monkeypatch) -> tuple[Path, Path]:
    gcroots, volumes = tmp_path / "gcroots", tmp_path / "volumes"
    gcroots.mkdir()
    volumes.mkdir()
    monkeypatch.setattr(cleanup, "CSI_GCROOTS", gcroots)
    monkeypatch.setattr(cleanup, "CSI_VOLUMES", volumes)
    monkeypatch.setattr(cleanup, "is_mount_source", lambda _path: False)
    return gcroots, volumes


def test_the_sweep_keeps_a_composefs_volume_whose_target_is_mounted(csi_dirs):
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


def test_target_is_mounted_without_a_target_file_is_no(tmp_path: Path):
    assert cleanup.target_is_mounted(tmp_path) is False
