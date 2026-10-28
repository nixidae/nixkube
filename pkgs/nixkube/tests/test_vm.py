# SPDX-License-Identifier: Apache-2.0

import os
import subprocess
import time
from pathlib import Path

import anyio
import pytest

from src.constants import NRI_VM_BUSYBOX
from src.nri.vm import (
    ALIVE,
    FAILED,
    READY,
    SCRIPT,
    STATE_DIR,
    host_source,
    is_vm_runtime,
    mark,
    prepare_waiter,
    refusal,
    waiter_args,
)

KATA = frozenset({"kata"})


def test_a_vm_handler_is_a_vm_runtime():
    assert is_vm_runtime("kata", KATA)


def test_runc_and_an_unnamed_handler_keep_the_namespace_mount():
    """A CRI that leaves the handler empty must get today's path, not this one."""
    assert not is_vm_runtime("runc", KATA)
    assert not is_vm_runtime("", KATA)


def test_the_waiter_runs_first_and_the_args_follow():
    assert waiter_args(["/nix/store/x-hello/bin/hello", "--greeting", "hi"]) == [
        f"/nix/{STATE_DIR}/busybox",
        "sh",
        f"/nix/{STATE_DIR}/{SCRIPT}",
        "/nix/store/x-hello/bin/hello",
        "--greeting",
        "hi",
    ]


def test_the_mount_source_is_the_nodes_path():
    tree = Path("/nix/var/nix-csi/containers/abc/nix")
    assert host_source(tree, Path("/var/lib/nix-csi")) == Path("/var/lib/nix-csi/nix/var/nix-csi/containers/abc/nix")


def test_a_writable_nix_is_refused_with_the_reason():
    reason = refusal(True, None)
    assert reason is not None and "hardlinks into the node's store" in reason


def test_store_path_mounts_are_refused():
    reason = refusal(False, {Path("/opt"): Path("/nix/store/x-hello")})
    assert reason is not None and "CSI volume" in reason


def test_a_plain_read_only_pod_is_not_refused():
    assert refusal(False, None) is None
    assert refusal(False, {}) is None


async def waiter(tmp_path: Path) -> Path:
    tree = tmp_path / "nix"
    await prepare_waiter(tree, NRI_VM_BUSYBOX)
    return tree


def run(tree: Path, *args: str) -> subprocess.CompletedProcess[str]:
    state = tree / STATE_DIR
    return subprocess.run(
        [str(state / "busybox"), "sh", str(state / SCRIPT), *args],
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )


@pytest.mark.asyncio
async def test_the_waiter_runs_the_command_once_ready(tmp_path):
    tree = await waiter(tmp_path)
    await mark(tree, READY)
    result = run(tree, "/bin/sh", "-c", "echo ran $0", "arg0")
    assert result.returncode == 0, result.stderr
    assert result.stdout == "ran arg0\n"


@pytest.mark.asyncio
async def test_the_waiter_fails_with_the_builds_reason(tmp_path):
    tree = await waiter(tmp_path)
    await mark(tree, FAILED, "nixkube: no substituter has it\n")
    result = run(tree, "/bin/sh", "-c", "echo ran")
    assert result.returncode == 1
    assert result.stdout == ""
    assert "no substituter has it" in result.stderr


@pytest.mark.asyncio
async def test_the_waiter_gives_up_when_the_node_goes_quiet(tmp_path):
    tree = await waiter(tmp_path)
    stale = time.time() - 60
    os.utime(tree / STATE_DIR / ALIVE, (stale, stale))
    result = run(tree, "/bin/sh", "-c", "echo ran")
    assert result.returncode == 1
    assert "stopped reporting" in result.stderr


@pytest.mark.asyncio
async def test_the_waiter_waits_while_the_node_reports(tmp_path):
    """The negative control for the two above: no marker, fresh heartbeat."""
    tree = await waiter(tmp_path)
    state = tree / STATE_DIR
    async with await anyio.open_process(
        [str(state / "busybox"), "sh", str(state / SCRIPT), "/bin/sh", "-c", "echo ran"]
    ) as proc:
        await anyio.sleep(2.5)
        assert proc.returncode is None
        await mark(tree, READY)
        with anyio.fail_after(10):
            await proc.wait()
        assert proc.stdout is not None
        out = await proc.stdout.receive()
    assert proc.returncode == 0
    assert out == b"ran\n"
