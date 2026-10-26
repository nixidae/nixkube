# SPDX-License-Identifier: Apache-2.0

from src.nixos_rollout import (
    Action,
    NodePhase,
    Phase,
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
