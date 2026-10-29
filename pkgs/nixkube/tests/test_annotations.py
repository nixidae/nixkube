# SPDX-License-Identifier: Apache-2.0

from pathlib import Path
from types import SimpleNamespace
from typing import cast

from nri import nri_pb2
from src.nri.annotations import (
    _parse_store_mounts_for_name,
    extract_container_store_paths,
    parse_nix_exclude,
    parse_nix_rw,
    parse_pod_exclude,
    parse_store_mounts,
)


def _make_req(
    container_name: str,
    annotations: dict[str, str],
    env: list[str] | None = None,
    args: list[str] | None = None,
) -> nri_pb2.CreateContainerRequest:
    """Build a minimal CreateContainerRequest-like object for testing."""
    return cast(
        nri_pb2.CreateContainerRequest,
        SimpleNamespace(
            pod=SimpleNamespace(annotations=annotations),
            container=SimpleNamespace(
                name=container_name,
                env=env or [],
                args=args or [],
            ),
        ),
    )


class TestParseStoreMountsForName:
    """Test _parse_store_mounts_for_name annotation parsing."""

    def test_basic_store_path_mount(self) -> None:
        """Parse a basic store path mount."""
        annotations = {"nixkube/pod": "/etc/ssl=/nix/store/cacert-1.0/etc/ssl"}
        result = _parse_store_mounts_for_name(annotations, "pod", "x86_64-linux")
        assert result == {Path("/etc/ssl"): Path("/nix/store/cacert-1.0/etc/ssl")}

    def test_multiple_suffixes(self) -> None:
        """Parse multiple mounts with different suffixes."""
        annotations = {
            "nixkube/pod-ssl": "/etc/ssl/certs=/nix/store/cacert-1.0/etc/ssl/certs",
            "nixkube/pod-passwd": "/etc/passwd=/nix/store/fakeNss/etc/passwd",
        }
        result = _parse_store_mounts_for_name(annotations, "pod", "x86_64-linux")
        assert len(result) == 2
        assert Path("/etc/ssl/certs") in result
        assert Path("/etc/passwd") in result

    def test_system_specific_mount(self) -> None:
        """Parse system-specific annotations."""
        annotations = {
            "nixkube/pod@x86_64-linux": "/etc/myapp=/nix/store/x86-hash",
            "nixkube/pod@aarch64-linux": "/etc/myapp=/nix/store/aarch64-hash",
        }
        # Should match x86_64-linux
        result_x86 = _parse_store_mounts_for_name(annotations, "pod", "x86_64-linux")
        assert result_x86 == {Path("/etc/myapp"): Path("/nix/store/x86-hash")}

        # Should match aarch64-linux
        result_arm = _parse_store_mounts_for_name(annotations, "pod", "aarch64-linux")
        assert result_arm == {Path("/etc/myapp"): Path("/nix/store/aarch64-hash")}

    def test_system_mismatch_filtered(self) -> None:
        """System-specific annotations are filtered out if system doesn't match."""
        annotations = {
            "nixkube/pod@aarch64-linux": "/etc/myapp=/nix/store/aarch64-hash",
        }
        result = _parse_store_mounts_for_name(annotations, "pod", "x86_64-linux")
        assert result == {}

    def test_all_systems_backward_compat(self) -> None:
        """Annotations without @system apply to all systems."""
        annotations = {"nixkube/pod": "/etc/ssl=/nix/store/cacert/etc/ssl"}
        result_x86 = _parse_store_mounts_for_name(annotations, "pod", "x86_64-linux")
        result_arm = _parse_store_mounts_for_name(annotations, "pod", "aarch64-linux")

        assert result_x86 == {Path("/etc/ssl"): Path("/nix/store/cacert/etc/ssl")}
        assert result_arm == {Path("/etc/ssl"): Path("/nix/store/cacert/etc/ssl")}

    def test_system_specific_with_index(self) -> None:
        """Parse system-specific annotations with index suffix (system before index)."""
        annotations = {
            "nixkube/pod@x86_64-linux-1": "/etc/ssl=/nix/store/x86-cacert",
            "nixkube/pod@aarch64-linux-1": "/etc/ssl=/nix/store/arm-cacert",
        }
        result_x86 = _parse_store_mounts_for_name(annotations, "pod", "x86_64-linux")
        assert result_x86 == {Path("/etc/ssl"): Path("/nix/store/x86-cacert")}
        result_arm = _parse_store_mounts_for_name(annotations, "pod", "aarch64-linux")
        assert result_arm == {Path("/etc/ssl"): Path("/nix/store/arm-cacert")}

    def test_old_format_suffix_before_system_is_unfiltered(self) -> None:
        """Old format (suffix before system: nixkube/pod-1@system) is treated as
        an unqualified annotation (no system filtering) because the @system part
        is absorbed into the index group and doesn't match the system group."""
        annotations = {
            "nixkube/pod-1@aarch64-linux": "/etc/ssl=/nix/store/arm-cacert",
        }
        # The old format is NOT correctly filtered — it matches on any system
        # This test documents the behaviour so users know to use the new format
        result_x86 = _parse_store_mounts_for_name(annotations, "pod", "x86_64-linux")
        result_arm = _parse_store_mounts_for_name(annotations, "pod", "aarch64-linux")
        assert result_x86 == result_arm  # both match: system filter is lost

    def test_container_specific_prefix(self) -> None:
        """Parse container-specific annotations."""
        annotations = {
            "nixkube/myapp": "/etc/myapp=/nix/store/myapp-1.0",
            "nixkube/myapp-ssl": "/etc/ssl=/nix/store/cacert",
        }
        result = _parse_store_mounts_for_name(annotations, "myapp", "x86_64-linux")
        assert len(result) == 2
        assert result[Path("/etc/myapp")] == Path("/nix/store/myapp-1.0")
        assert result[Path("/etc/ssl")] == Path("/nix/store/cacert")

    def test_container_specific_with_system(self) -> None:
        """Parse container-specific system-filtered annotations."""
        annotations = {
            "nixkube/myapp@x86_64-linux": "/etc/myapp=/nix/store/x86-app",
            "nixkube/myapp@aarch64-linux": "/etc/myapp=/nix/store/arm-app",
        }
        result_x86 = _parse_store_mounts_for_name(annotations, "myapp", "x86_64-linux")
        assert result_x86 == {Path("/etc/myapp"): Path("/nix/store/x86-app")}

    def test_invalid_annotation_skipped(self) -> None:
        """Annotations without '=' are skipped."""
        annotations = {
            "nixkube/pod": "invalid",
            "nixkube/pod-valid": "/etc/ssl=/nix/store/cacert",
        }
        result = _parse_store_mounts_for_name(annotations, "pod", "x86_64-linux")
        assert len(result) == 1
        assert result[Path("/etc/ssl")] == Path("/nix/store/cacert")

    def test_flake_reference_detection(self) -> None:
        """Flake references are preserved as source paths."""
        annotations = {"nixkube/pod": "/etc/myapp=github:nixos/nixpkgs#legacyPackages.x86_64-linux.hello"}
        result = _parse_store_mounts_for_name(annotations, "pod", "x86_64-linux")
        assert result == {Path("/etc/myapp"): Path("github:nixos/nixpkgs#legacyPackages.x86_64-linux.hello")}

    def test_nix_expression_detection(self) -> None:
        """Nix expressions are preserved as source paths."""
        annotations = {"nixkube/pod": '/etc/result=builtins.toFile "test" "value"'}
        result = _parse_store_mounts_for_name(annotations, "pod", "x86_64-linux")
        assert result == {Path("/etc/result"): Path('builtins.toFile "test" "value"')}


class TestParseStoreMounts:
    """Test parse_store_mounts with wildcard and container-specific merging."""

    def test_wildcard_and_container_merge(self) -> None:
        """Container-specific mounts override wildcard mounts."""
        annotations = {
            "nixkube/pod": "/etc/ssl=/nix/store/cacert-default",
            "nixkube/myapp": "/etc/ssl=/nix/store/cacert-myapp",
        }
        result = parse_store_mounts(annotations, "myapp", "x86_64-linux")
        # Container-specific should override wildcard
        assert result[Path("/etc/ssl")] == Path("/nix/store/cacert-myapp")

    def test_wildcard_plus_container_specific_paths(self) -> None:
        """Merge wildcard and container-specific mounts for different paths."""
        annotations = {
            "nixkube/pod": "/etc/ssl=/nix/store/cacert",
            "nixkube/myapp": "/opt/app=/nix/store/myapp",
        }
        result = parse_store_mounts(annotations, "myapp", "x86_64-linux")
        assert len(result) == 2
        assert result[Path("/etc/ssl")] == Path("/nix/store/cacert")
        assert result[Path("/opt/app")] == Path("/nix/store/myapp")

    def test_system_specific_wildcard_override(self) -> None:
        """System-specific wildcard annotations apply when system matches."""
        annotations = {
            "nixkube/pod": "/etc/ssl=/nix/store/cacert-default",
            "nixkube/pod@x86_64-linux": "/etc/ssl=/nix/store/cacert-x86",
        }
        result_x86 = parse_store_mounts(annotations, "myapp", "x86_64-linux")
        result_arm = parse_store_mounts(annotations, "myapp", "aarch64-linux")

        # x86 should get system-specific version
        assert result_x86[Path("/etc/ssl")] == Path("/nix/store/cacert-x86")
        # aarch64 should get default (no system-specific match)
        assert result_arm[Path("/etc/ssl")] == Path("/nix/store/cacert-default")

    def test_system_specific_container_override(self) -> None:
        """System-specific container annotations override wildcards."""
        annotations = {
            "nixkube/pod@x86_64-linux": "/opt/lib=/nix/store/lib-x86",
            "nixkube/myapp@x86_64-linux": "/opt/lib=/nix/store/lib-myapp-x86",
        }
        result = parse_store_mounts(annotations, "myapp", "x86_64-linux")
        assert result[Path("/opt/lib")] == Path("/nix/store/lib-myapp-x86")


class TestParseNixExclude:
    """`parse_nix_exclude`, for a container that brings its own store.

    `appstarter-init` is the case. It mounts the claim at `/nix-volume`, so
    the `/nix` check in `CreateContainer` does not cover it, and it cannot
    mount `/nix` either — that shadows the image store its fallback lives in.
    Without an opt-out, NRI reads `APPSTARTER_WANTED` from its environment
    and realises pynixd's closure into the node's store, whose only
    substituter is the pynixd this pod is starting. Issue #55.
    """

    def test_nothing_is_excluded_by_default(self) -> None:
        """The negative control. A default of true would silence NRI
        everywhere and look like a working cluster with no store mounted."""
        assert parse_nix_exclude({}, "myapp", "x86_64-linux") is False

    def test_a_named_container_is_excluded(self) -> None:
        annotations = {"nixkube/appstarter-init-exclude": "true"}
        assert parse_nix_exclude(annotations, "appstarter-init", "x86_64-linux") is True

    def test_a_named_container_does_not_exclude_its_neighbours(self) -> None:
        """The pynixd pod carries this annotation and its main container must
        still be handled normally."""
        annotations = {"nixkube/appstarter-init-exclude": "true"}
        assert parse_nix_exclude(annotations, "pynixd", "x86_64-linux") is False

    def test_pod_wide_exclude_covers_every_container(self) -> None:
        annotations = {"nixkube/pod-exclude": "true"}
        assert parse_nix_exclude(annotations, "anything", "x86_64-linux") is True

    def test_a_container_can_opt_back_in(self) -> None:
        """Same precedence as `-rw`: container-specific wins, including an
        explicit false against a pod-wide true."""
        annotations = {
            "nixkube/pod-exclude": "true",
            "nixkube/myapp-exclude": "false",
        }
        assert parse_nix_exclude(annotations, "myapp", "x86_64-linux") is False

    def test_the_system_variant_is_honoured(self) -> None:
        annotations = {
            "nixkube/myapp-exclude@x86_64-linux": "true",
            "nixkube/myapp-exclude@aarch64-linux": "false",
        }
        assert parse_nix_exclude(annotations, "myapp", "x86_64-linux") is True
        assert parse_nix_exclude(annotations, "myapp", "aarch64-linux") is False


class TestParseNixRW:
    """Test parse_nix_rw RW flag parsing."""

    def test_pod_wide_rw_enabled(self) -> None:
        """Pod-wide RW flag enables RW for all containers."""
        annotations = {"nixkube/pod-rw": "true"}
        assert parse_nix_rw(annotations, "myapp", "x86_64-linux") is True

    def test_pod_wide_rw_disabled(self) -> None:
        """Absent RW flag defaults to false."""
        annotations: dict[str, str] = {}
        assert parse_nix_rw(annotations, "myapp", "x86_64-linux") is False

    def test_pod_wide_rw_explicit_false(self) -> None:
        """Explicit false disables RW."""
        annotations = {"nixkube/pod-rw": "false"}
        assert parse_nix_rw(annotations, "myapp", "x86_64-linux") is False

    def test_container_specific_override(self) -> None:
        """Container-specific RW overrides pod-wide setting."""
        annotations = {
            "nixkube/pod-rw": "true",
            "nixkube/myapp-rw": "false",
        }
        assert parse_nix_rw(annotations, "myapp", "x86_64-linux") is False

    def test_system_specific_pod_rw(self) -> None:
        """System-specific pod-wide RW flag applies to matching system."""
        annotations = {
            "nixkube/pod-rw@x86_64-linux": "true",
            "nixkube/pod-rw@aarch64-linux": "false",
        }
        assert parse_nix_rw(annotations, "myapp", "x86_64-linux") is True
        assert parse_nix_rw(annotations, "myapp", "aarch64-linux") is False

    def test_system_specific_container_rw(self) -> None:
        """System-specific container-specific RW overrides pod-wide."""
        annotations = {
            "nixkube/pod-rw": "true",
            "nixkube/myapp-rw@aarch64-linux": "false",
        }
        # x86 gets pod-wide true
        assert parse_nix_rw(annotations, "myapp", "x86_64-linux") is True
        # aarch64 gets container-specific false
        assert parse_nix_rw(annotations, "myapp", "aarch64-linux") is False

    def test_container_takes_precedence_over_system_pod(self) -> None:
        """Container-specific (any system) takes precedence over system-specific pod."""
        annotations = {
            "nixkube/pod-rw@x86_64-linux": "true",
            "nixkube/myapp-rw": "false",
        }
        assert parse_nix_rw(annotations, "myapp", "x86_64-linux") is False

    def test_system_specific_container_without_system_specific_pod(self) -> None:
        """System-specific container RW works with default pod RW."""
        annotations = {
            "nixkube/pod-rw": "false",
            "nixkube/myapp-rw@x86_64-linux": "true",
        }
        assert parse_nix_rw(annotations, "myapp", "x86_64-linux") is True
        assert parse_nix_rw(annotations, "myapp", "aarch64-linux") is False


class TestExtractContainerStorePaths:
    """Test extract_container_store_paths combining env, args, and annotations."""

    STORE_PATH = "/nix/store/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa-hello-1.0"

    def test_extracts_from_env(self) -> None:
        req = _make_req("myapp", {}, env=[f"NIX_PATH={self.STORE_PATH}"])
        result = extract_container_store_paths(req, "x86_64-linux")
        assert Path(self.STORE_PATH) in result

    def test_extracts_from_args(self) -> None:
        req = _make_req("myapp", {}, args=[self.STORE_PATH])
        result = extract_container_store_paths(req, "x86_64-linux")
        assert Path(self.STORE_PATH) in result

    def test_extracts_from_pod_annotation(self) -> None:
        req = _make_req("myapp", {"nixkube/pod": self.STORE_PATH})
        result = extract_container_store_paths(req, "x86_64-linux")
        assert Path(self.STORE_PATH) in result

    def test_extracts_from_container_annotation(self) -> None:
        req = _make_req("myapp", {"nixkube/myapp": self.STORE_PATH})
        result = extract_container_store_paths(req, "x86_64-linux")
        assert Path(self.STORE_PATH) in result

    def test_system_filtered_annotation(self) -> None:
        store_x86 = "/nix/store/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa-x86-1.0"
        store_arm = "/nix/store/bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb-arm-1.0"
        req = _make_req(
            "myapp",
            {
                "nixkube/pod@x86_64-linux": store_x86,
                "nixkube/pod@aarch64-linux": store_arm,
            },
        )
        result_x86 = extract_container_store_paths(req, "x86_64-linux")
        result_arm = extract_container_store_paths(req, "aarch64-linux")
        assert Path(store_x86) in result_x86
        assert Path(store_arm) not in result_x86
        assert Path(store_arm) in result_arm
        assert Path(store_x86) not in result_arm

    def test_other_container_annotation_ignored(self) -> None:
        """Annotations for a different container are not extracted."""
        req = _make_req("myapp", {"nixkube/otherapp": self.STORE_PATH})
        result = extract_container_store_paths(req, "x86_64-linux")
        assert result == set()

    def test_deduplication(self) -> None:
        """Same store path appearing in env and annotation is deduplicated."""
        req = _make_req(
            "myapp",
            {"nixkube/pod": self.STORE_PATH},
            env=[f"PATH={self.STORE_PATH}/bin"],
        )
        result = extract_container_store_paths(req, "x86_64-linux")
        assert result == {Path(self.STORE_PATH)}

class TestParsePodExclude:
    """`parse_pod_exclude`: the CSI driver fetches nothing a pod's spec names.

    The switch Job is the case. Its `TOPLEVEL` names the host system it
    switches to, which may be in the host's store only, and its volume holds
    the switch program alone. Fetching the spec's paths into the volume
    failed the mount: no substituter has that system.
    """

    def test_nothing_is_excluded_by_default(self) -> None:
        assert parse_pod_exclude({}, "x86_64-linux") is False

    def test_pod_wide_exclude(self) -> None:
        assert parse_pod_exclude({"nixkube/pod-exclude": "true"}, "x86_64-linux") is True

    def test_a_container_exclude_is_not_pod_wide(self) -> None:
        """A volume belongs to the pod, so only the pod-wide annotation speaks for it."""
        assert parse_pod_exclude({"nixkube/switch-exclude": "true"}, "x86_64-linux") is False

    def test_a_system_qualified_exclude(self) -> None:
        annotations = {"nixkube/pod-exclude@aarch64-linux": "true"}
        assert parse_pod_exclude(annotations, "aarch64-linux") is True
        assert parse_pod_exclude(annotations, "x86_64-linux") is False
