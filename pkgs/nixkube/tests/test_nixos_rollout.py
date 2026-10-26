# SPDX-License-Identifier: Apache-2.0

import json
from pathlib import Path

import pytest

from src.nixos_rollout import (
    Action,
    Build,
    NodePhase,
    Phase,
    build_argv,
    build_entries,
    git_env,
    job_name,
    plan,
    rollout_phase,
    selects,
)

A = "/nix/store/" + "a" * 32 + "-nixos-system-a"
B = "/nix/store/" + "b" * 32 + "-nixos-system-b"


def test_selects():
    labels = {"role": "worker", "zone": "x"}
    assert selects({}, labels)
    assert selects({"matchLabels": {"role": "worker"}}, labels)
    assert not selects({"matchLabels": {"role": "cp"}}, labels)
    assert selects(
        {"matchExpressions": [{"key": "zone", "operator": "In", "values": ["x", "y"]}]},
        labels,
    )
    assert not selects(
        {"matchExpressions": [{"key": "zone", "operator": "NotIn", "values": ["x"]}]},
        labels,
    )
    assert selects(
        {"matchExpressions": [{"key": "gpu", "operator": "DoesNotExist"}]}, labels
    )
    assert not selects(
        {"matchExpressions": [{"key": "gpu", "operator": "Exists"}]}, labels
    )


def test_job_name_is_stable_and_short():
    name = job_name("r", "n" * 253, Action.SWITCH, A, 1)
    assert name == job_name("r", "n" * 253, Action.SWITCH, A, 1)
    assert name != job_name("r", "n" * 253, Action.SWITCH, A, 2)
    assert len(name) <= 63


def test_plan_new_node_is_pending():
    out = plan({}, {"n1": A}, ["n1", "n2"], set(), 1)
    assert out == {"n1": {"phase": NodePhase.PENDING, "target": A}}


def test_plan_done_stays_until_target_moves():
    done = {"n1": {"phase": "Done", "target": A, "current": A}}
    assert plan(done, {"n1": A}, ["n1"], set(), 2) == done
    out = plan(done, {"n1": B}, ["n1"], set(), 2)
    assert out["n1"] == {"phase": NodePhase.PENDING, "target": B, "current": A}


def test_plan_keeps_in_flight_even_when_deselected():
    entries = {"n1": {"phase": "Confirming", "target": A, "generation": 1}}
    assert plan(entries, {"n1": B}, ["n1"], {"n1"}, 2) == entries
    assert plan(entries, {}, [], set(), 2) == entries


def test_plan_reverted_resumes_on_a_new_generation():
    entries = {
        "n1": {"phase": "Reverted", "target": A, "failedSystem": A, "generation": 3}
    }
    assert plan(entries, {"n1": A}, ["n1"], set(), 3) == entries
    out = plan(entries, {"n1": A}, ["n1"], set(), 4)
    assert out["n1"]["phase"] == NodePhase.PENDING
    assert out["n1"]["failedSystem"] == A


def test_plan_conflict():
    out = plan({}, {"n1": A}, ["n1"], {"n1"}, 1)
    assert out["n1"]["phase"] == NodePhase.CONFLICT


def test_rollout_phase():
    assert rollout_phase({}) == Phase.COMPLETE
    assert rollout_phase({"a": {"phase": "Done"}}) == Phase.COMPLETE
    assert (
        rollout_phase({"a": {"phase": "Done"}, "b": {"phase": "Pending"}})
        == Phase.PROGRESSING
    )
    assert (
        rollout_phase({"a": {"phase": "Reverted"}, "b": {"phase": "Switching"}})
        == Phase.HALTED
    )


def test_build_failure_halts():
    assert rollout_phase({"a": {"phase": NodePhase.BUILD_FAILED}}) == Phase.HALTED
    assert rollout_phase({"a": {"phase": NodePhase.BUILDING}}) == Phase.PROGRESSING


def test_build_argv_passes_every_value_as_a_string():
    labels = {"kubernetes.io/arch": "arm64", "role": "worker"}
    argv = build_argv(
        {"url": "https://x/y.git", "rev": "a" * 40, "attribute": "hosts.all"},
        "n1",
        labels,
        "ssh-ng://nix@pynixd",
    )
    args = {argv[i + 1]: argv[i + 2] for i, a in enumerate(argv) if a == "--argstr"}
    assert args == {
        "url": "https://x/y.git",
        "rev": "a" * 40,
        "attribute": "hosts.all",
        "name": "n1",
        "labels": json.dumps(labels, sort_keys=True),
        "system": "aarch64-linux",
    }
    assert argv[argv.index("--store") + 1] == "ssh-ng://nix@pynixd"
    assert argv[argv.index("--eval-store") + 1] == "auto"
    assert "--impure" in argv


def test_git_env_basic_auth():
    env = git_env(
        "kubernetes.io/basic-auth", {"username": b"u", "password": b"p"}, Path("/x")
    )
    assert env["GIT_CONFIG_KEY_0"] == "http.extraHeader"
    assert env["GIT_CONFIG_VALUE_0"] == "Authorization: Basic dTpw"


def test_git_env_ssh_needs_known_hosts(tmp_path):
    with pytest.raises(ValueError, match="known_hosts"):
        git_env("kubernetes.io/ssh-auth", {"ssh-privatekey": b"k"}, tmp_path)
    env = git_env(
        "kubernetes.io/ssh-auth",
        {"ssh-privatekey": b"k", "known_hosts": b"h"},
        tmp_path,
    )
    assert (tmp_path / "key").stat().st_mode & 0o777 == 0o600
    assert "StrictHostKeyChecking=yes" in env["GIT_SSH_COMMAND"]


def test_git_env_refuses_other_types(tmp_path):
    with pytest.raises(ValueError, match="unsupported"):
        git_env("Opaque", {}, tmp_path)


def test_a_restart_keeps_a_reverted_node_while_it_builds():
    reverted = {"phase": NodePhase.REVERTED, "target": A, "generation": 2}
    old = {"a": reverted}
    entries = plan(old, {}, ["a"], set(), 2)
    entries |= build_entries(entries, old, {"a": Build()}, 2)
    assert entries["a"] == reverted
    # The build returns the system that failed: still Reverted.
    assert plan(entries, {"a": A}, ["a"], set(), 2)["a"] == reverted


def test_a_restart_keeps_a_done_node_while_it_builds():
    done = {"phase": NodePhase.DONE, "target": A, "current": A, "generation": 2}
    assert build_entries({}, {"a": done}, {"a": Build()}, 2)["a"] == done


def test_a_new_generation_builds_and_a_failed_build_halts():
    old = {"a": {"phase": NodePhase.REVERTED, "target": A, "generation": 1}}
    building = build_entries({}, old, {"a": Build()}, 2)
    assert building["a"]["phase"] == NodePhase.BUILDING
    done = {"a": {"phase": NodePhase.DONE, "target": A, "current": A, "generation": 1}}
    building = build_entries({}, done, {"a": Build()}, 2)
    assert building["a"] == {"phase": NodePhase.BUILDING, "current": A}
    failed = build_entries({}, old, {"a": Build(error="boom")}, 2)
    assert failed["a"]["phase"] == NodePhase.BUILD_FAILED
    assert rollout_phase(failed) == Phase.HALTED
