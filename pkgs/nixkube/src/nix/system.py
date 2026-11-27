# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from functools import cache

import huggorm


@cache
def get_current_system() -> str:
    """The system this node's Nix builds for: `builtins.currentSystem`.

    Nix's own setting, read in process through huggorm. It opens no store and
    runs no evaluation, so it cannot block on either.
    """
    return huggorm.current_system()
