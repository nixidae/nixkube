# SPDX-License-Identifier: Apache-2.0

from pathlib import Path

import pytest

from src.build_pods import PROFILE_NAME, reconcile_profiles, wanted_profiles

PROFILE = b'{"defaultAction": "SCMP_ACT_ERRNO"}'


def test_on_installs_the_profile_and_removes_what_it_does_not_want(tmp_path: Path) -> None:
    seccomp = tmp_path / "seccomp"
    ours = seccomp / "nixkube"
    ours.mkdir(parents=True)
    (ours / "old.json").write_bytes(b"{}")
    (ours / PROFILE_NAME).write_bytes(b"stale")
    (seccomp / "theirs.json").write_bytes(b"{}")

    reconcile_profiles(ours, {PROFILE_NAME: PROFILE})

    assert sorted(p.name for p in ours.iterdir()) == [PROFILE_NAME]
    assert (ours / PROFILE_NAME).read_bytes() == PROFILE
    assert (ours / PROFILE_NAME).stat().st_mode & 0o777 == 0o644
    assert (seccomp / "theirs.json").exists()


def test_off_removes_the_directory_and_nothing_beside_it(tmp_path: Path) -> None:
    seccomp = tmp_path / "seccomp"
    ours = seccomp / "nixkube"
    ours.mkdir(parents=True)
    (ours / PROFILE_NAME).write_bytes(PROFILE)
    (seccomp / "theirs.json").write_bytes(b"{}")

    reconcile_profiles(ours, {})

    assert not ours.exists()
    assert (seccomp / "theirs.json").exists()


def test_off_on_a_node_that_never_had_it_creates_nothing(tmp_path: Path) -> None:
    reconcile_profiles(tmp_path / "seccomp" / "nixkube", {})

    assert not (tmp_path / "seccomp").exists()


def test_on_without_a_profile_refuses() -> None:
    with pytest.raises(RuntimeError, match="NIXKUBE_BUILD_POD_SECCOMP"):
        wanted_profiles(enabled=True, profile="")


def test_off_wants_nothing_even_without_a_profile() -> None:
    assert wanted_profiles(enabled=False, profile="") == {}
