# SPDX-License-Identifier: Apache-2.0

import platform

from src.nix import get_current_system


def test_the_current_system_is_this_machine() -> None:
    # The test environment's nix.conf sets no `system` or `eval-system`, so
    # Nix answers the machine it runs on, as `builtins.currentSystem` does.
    assert get_current_system() == f"{platform.machine()}-linux"
