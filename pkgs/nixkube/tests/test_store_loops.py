# SPDX-License-Identifier: Apache-2.0

from src.cli import store_loops


def test_a_separate_store_runs_its_server_and_its_sweep() -> None:
    assert store_loops(host_store=False) == {"store-server", "gc"}


def test_a_host_store_runs_its_own_server_leaves_gc_to_the_host_and_retires_its_old_store() -> None:
    assert store_loops(host_store=True) == {"store-server", "retire"}
