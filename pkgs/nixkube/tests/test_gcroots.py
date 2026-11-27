# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from pathlib import Path

import huggorm
import pytest

from src.errors import InstallGCRootError, InstallResultLinkError
from src.nix import install_gcroots, install_result_link

# A volume's chroot store under the test's directory, as `prepare_volume` leaves it.
pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


async def _volume_with(volume_root: Path, *names: str) -> list[Path]:
    store = huggorm.AsyncStore(str(volume_root))
    try:
        return [Path(await store.print_store_path(await store.add_to_store(n, n.encode()))) for n in names]
    finally:
        await store.aclose()


def _auto_roots(volume_root: Path) -> set[str]:
    return {link.readlink().as_posix() for link in (volume_root / "nix/var/nix/gcroots/auto").iterdir()}


async def test_each_package_is_a_root_named_as_nix_build_names_it(tmp_path: Path) -> None:
    a, b = sorted(await _volume_with(tmp_path, "a", "b"))
    out_link = tmp_path / "nix/var/nix/gcroots/csi"

    await install_gcroots({a, b}, out_link, volume_root=tmp_path)

    assert out_link.readlink() == a
    assert out_link.with_name("csi-1").readlink() == b
    # Through `addPermRoot`, as `nix build --out-link` did: an indirect root per link.
    assert {out_link.as_posix(), out_link.with_name("csi-1").as_posix()} <= _auto_roots(tmp_path)


async def test_the_result_link_points_at_the_primary_package(tmp_path: Path) -> None:
    [primary] = await _volume_with(tmp_path, "primary")

    await install_result_link(tmp_path, primary)

    assert (tmp_path / "nix/var/result").readlink() == primary
    assert (tmp_path / "nix/var/result").as_posix() in _auto_roots(tmp_path)


async def test_a_path_the_volume_lacks_is_refused_and_not_rooted(tmp_path: Path) -> None:
    await _volume_with(tmp_path, "present")
    absent = Path("/nix/store/" + "0" * 32 + "-absent")
    out_link = tmp_path / "nix/var/nix/gcroots/csi"

    with pytest.raises(InstallGCRootError, match="garbage collection root"):
        await install_gcroots({absent}, out_link, volume_root=tmp_path)
    with pytest.raises(InstallResultLinkError):
        await install_result_link(tmp_path, absent)
    assert not out_link.is_symlink()
    assert not (tmp_path / "nix/var/result").is_symlink()
