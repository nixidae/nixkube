# SPDX-License-Identifier: Apache-2.0

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.csi.server import check_csi_volume_mounts
from src.kube_docs import Container, Volume, VolumeMount


def make_pod(spec: dict[str, object]) -> MagicMock:
    """Create a mock Pod with a .raw property returning the given spec dict."""
    pod = MagicMock()
    pod.raw = {"spec": spec}
    return pod


def nixkube_volume(name: str, driver: str = "nixkube") -> Volume:
    return {"name": name, "csi": {"driver": driver}}


def non_csi_volume(name: str) -> Volume:
    return {"name": name, "emptyDir": {}}


def container_with_mounts(name: str, *mounts: VolumeMount) -> Container:
    return {"name": name, "volumeMounts": list(mounts)}


def mount(vol_name: str, mount_path: str, sub_path: str | None = None) -> VolumeMount:
    m: VolumeMount = {"name": vol_name, "mountPath": mount_path}
    if sub_path is not None:
        m["subPath"] = sub_path
    return m


class TestCheckCsiVolumeMounts:
    """Tests for check_csi_volume_mounts() warning event logic."""

    @pytest.mark.asyncio
    async def test_no_volumes_no_events(self) -> None:
        """Pod with no volumes emits no events."""
        pod = make_pod({"containers": [], "volumes": []})
        with patch("src.csi.server.report_event", new_callable=AsyncMock) as mock_event:
            await check_csi_volume_mounts(pod)
            mock_event.assert_not_called()

    @pytest.mark.asyncio
    async def test_non_csi_volume_no_events(self) -> None:
        """Non-CSI volumes (emptyDir) are ignored."""
        pod = make_pod(
            {
                "containers": [container_with_mounts("app", mount("data", "/data"))],
                "volumes": [non_csi_volume("data")],
            }
        )
        with patch("src.csi.server.report_event", new_callable=AsyncMock) as mock_event:
            await check_csi_volume_mounts(pod)
            mock_event.assert_not_called()

    @pytest.mark.asyncio
    async def test_non_nixkube_csi_driver_no_events(self) -> None:
        """CSI volumes from other drivers are ignored."""
        pod = make_pod(
            {
                "containers": [container_with_mounts("app", mount("nfs", "/data"))],
                "volumes": [{"name": "nfs", "csi": {"driver": "nfs.csi.k8s.io"}}],
            }
        )
        with patch("src.csi.server.report_event", new_callable=AsyncMock) as mock_event:
            await check_csi_volume_mounts(pod)
            mock_event.assert_not_called()

    @pytest.mark.asyncio
    async def test_correct_config_no_events(self) -> None:
        """Correct nixkube volume (subPath='nix', mountPath='/nix') emits no events."""
        pod = make_pod(
            {
                "containers": [container_with_mounts("app", mount("nix-store", "/nix", "nix"))],
                "volumes": [nixkube_volume("nix-store")],
            }
        )
        with patch("src.csi.server.report_event", new_callable=AsyncMock) as mock_event:
            await check_csi_volume_mounts(pod)
            mock_event.assert_not_called()

    @pytest.mark.asyncio
    async def test_missing_subpath_emits_warning(self) -> None:
        """Volume mount without subPath emits MissingSubPath warning."""
        pod = make_pod(
            {
                "containers": [container_with_mounts("app", mount("nix-store", "/nix"))],
                "volumes": [nixkube_volume("nix-store")],
            }
        )
        with patch("src.csi.server.report_event", new_callable=AsyncMock) as mock_event:
            await check_csi_volume_mounts(pod)
            reasons = [c.kwargs["reason"] for c in mock_event.call_args_list]
            assert "MissingSubPath" in reasons

    @pytest.mark.asyncio
    async def test_missing_nix_mount_emits_warning(self) -> None:
        """Volume mounted elsewhere but not at /nix emits MissingNixMount warning."""
        pod = make_pod(
            {
                "containers": [container_with_mounts("app", mount("nix-store", "/opt/nix", "nix"))],
                "volumes": [nixkube_volume("nix-store")],
            }
        )
        with patch("src.csi.server.report_event", new_callable=AsyncMock) as mock_event:
            await check_csi_volume_mounts(pod)
            reasons = [c.kwargs["reason"] for c in mock_event.call_args_list]
            assert "MissingNixMount" in reasons

    @pytest.mark.asyncio
    async def test_no_mounts_at_all_emits_missing_nix_mount(self) -> None:
        """nixkube volume with no volumeMounts in any container emits MissingNixMount."""
        pod = make_pod(
            {
                "containers": [{"name": "app", "volumeMounts": []}],
                "volumes": [nixkube_volume("nix-store")],
            }
        )
        with patch("src.csi.server.report_event", new_callable=AsyncMock) as mock_event:
            await check_csi_volume_mounts(pod)
            reasons = [c.kwargs["reason"] for c in mock_event.call_args_list]
            assert "MissingNixMount" in reasons
            assert "MissingSubPath" not in reasons

    @pytest.mark.asyncio
    async def test_both_warnings_when_missing_subpath_and_not_at_nix(self) -> None:
        """Mount missing subPath and not at /nix triggers both warnings."""
        pod = make_pod(
            {
                "containers": [container_with_mounts("app", mount("nix-store", "/opt/nix"))],
                "volumes": [nixkube_volume("nix-store")],
            }
        )
        with patch("src.csi.server.report_event", new_callable=AsyncMock) as mock_event:
            await check_csi_volume_mounts(pod)
            reasons = [c.kwargs["reason"] for c in mock_event.call_args_list]
            assert "MissingSubPath" in reasons
            assert "MissingNixMount" in reasons

    @pytest.mark.asyncio
    async def test_compat_driver_name_also_checked(self) -> None:
        """nix.csi.store driver is treated the same as nixkube."""
        pod = make_pod(
            {
                "containers": [container_with_mounts("app", mount("nix-store", "/nix"))],
                "volumes": [nixkube_volume("nix-store", driver="nix.csi.store")],
            }
        )
        with patch("src.csi.server.report_event", new_callable=AsyncMock) as mock_event:
            await check_csi_volume_mounts(pod)
            reasons = [c.kwargs["reason"] for c in mock_event.call_args_list]
            # Mount at /nix but no subPath → MissingSubPath; mount IS at /nix → no MissingNixMount
            assert "MissingSubPath" in reasons
            assert "MissingNixMount" not in reasons

    @pytest.mark.asyncio
    async def test_init_container_mount_checked(self) -> None:
        """Mounts in initContainers are also checked for missing subPath."""
        pod = make_pod(
            {
                "containers": [],
                "initContainers": [container_with_mounts("init", mount("nix-store", "/nix"))],
                "volumes": [nixkube_volume("nix-store")],
            }
        )
        with patch("src.csi.server.report_event", new_callable=AsyncMock) as mock_event:
            await check_csi_volume_mounts(pod)
            reasons = [c.kwargs["reason"] for c in mock_event.call_args_list]
            assert "MissingSubPath" in reasons

    @pytest.mark.asyncio
    async def test_init_container_nix_mount_satisfies_missing_nix_check(self) -> None:
        """A /nix mount in an initContainer prevents MissingNixMount."""
        pod = make_pod(
            {
                "containers": [],
                "initContainers": [container_with_mounts("init", mount("nix-store", "/nix", "nix"))],
                "volumes": [nixkube_volume("nix-store")],
            }
        )
        with patch("src.csi.server.report_event", new_callable=AsyncMock) as mock_event:
            await check_csi_volume_mounts(pod)
            mock_event.assert_not_called()

    @pytest.mark.asyncio
    async def test_multiple_containers_one_missing_subpath(self) -> None:
        """Only the container with a missing subPath triggers MissingSubPath."""
        pod = make_pod(
            {
                "containers": [
                    container_with_mounts("good", mount("nix-store", "/nix", "nix")),
                    container_with_mounts("bad", mount("nix-store", "/nix")),
                ],
                "volumes": [nixkube_volume("nix-store")],
            }
        )
        with patch("src.csi.server.report_event", new_callable=AsyncMock) as mock_event:
            await check_csi_volume_mounts(pod)
            missing_subpath_calls = [c for c in mock_event.call_args_list if c.kwargs["reason"] == "MissingSubPath"]
            assert len(missing_subpath_calls) == 1
            assert "bad" in missing_subpath_calls[0].kwargs["note"]

    @pytest.mark.asyncio
    async def test_multiple_nixkube_volumes_checked_independently(self) -> None:
        """Each nixkube volume is checked independently."""
        pod = make_pod(
            {
                "containers": [
                    container_with_mounts(
                        "app",
                        mount("vol-a", "/nix", "nix"),  # correct
                        mount("vol-b", "/nix2"),  # missing subPath + wrong path
                    )
                ],
                "volumes": [
                    nixkube_volume("vol-a"),
                    nixkube_volume("vol-b"),
                ],
            }
        )
        with patch("src.csi.server.report_event", new_callable=AsyncMock) as mock_event:
            await check_csi_volume_mounts(pod)
            reasons = [c.kwargs["reason"] for c in mock_event.call_args_list]
            # vol-a is fine, vol-b is missing subPath and missing /nix
            assert reasons.count("MissingSubPath") == 1
            assert reasons.count("MissingNixMount") == 1

    @pytest.mark.asyncio
    async def test_events_are_warnings(self) -> None:
        """All emitted events have event_type='Warning'.

        This mount is missing its subPath and is not at /nix, so it is worth
        two events. Counting them first is the point: a loop over an empty
        call list asserts nothing, so a check that stopped reporting would
        pass here.
        """
        pod = make_pod(
            {
                "containers": [container_with_mounts("app", mount("nix-store", "/opt/nix"))],
                "volumes": [nixkube_volume("nix-store")],
            }
        )
        with patch("src.csi.server.report_event", new_callable=AsyncMock) as mock_event:
            await check_csi_volume_mounts(pod)
            assert len(mock_event.call_args_list) == 2
            for c in mock_event.call_args_list:
                assert c.kwargs["event_type"] == "Warning"
