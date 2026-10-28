# SPDX-License-Identifier: Apache-2.0
"""`appstarter init`: put a usable store on the node."""

import logging
import os
import time
from pathlib import Path

from . import store
from .config import RESULT_PATH, ROOT_DIR, State, store_path_for

log = logging.getLogger("appstarter.init")

# Only a pynixd that did not answer is worth asking twice. A pynixd that
# answered and does not have the path will not have it a minute later, and a
# binary cache that 404s is not a race. Issue #27 is the case this exists for:
# pynixd may be starting elsewhere and waiting on this very node.
_MAX_ATTEMPTS = 5
_BACKOFF_STEP = 10.0

_PYNIXD_STORE = "ssh-ng://nix@pynixd"

# The same store as `store.LOCAL_STORE`, asked for as a *substituter*.
# `trusted=true` belongs only here: it says a path may be taken without a
# signature, which is a question about substitution and not about reading.
_LOCAL = f"{store.LOCAL_STORE}?trusted=true"


def _substituters() -> tuple[list[str], str]:
    """The extra substituters to pass, and what pynixd had to say.

    **The three states are the point.** `nix build` reports "no substituter
    that can build it" whether pynixd was off, unreachable, or answered and
    did not have the path. Those are three different problems with one
    sentence between them, and two of them happened on nixlab2 in one day.
    """
    if os.environ.get("PYNIXD_ENABLED", "false") != "true":
        return [_LOCAL], "disabled"
    if store.ping(_PYNIXD_STORE):
        return [_LOCAL, f"{_PYNIXD_STORE}?trusted=true"], "answered"
    return [_LOCAL], "unreachable"


def _fetch(wanted: str, into: Path, out_link: Path) -> None:
    substituters, pynixd = _substituters()
    log.info("fetching %s (pynixd: %s)", wanted, pynixd)

    for attempt in range(1, _MAX_ATTEMPTS + 1):
        try:
            store.build(wanted, into, substituters, out_link)
            return
        except store.StoreError as error:
            retryable = pynixd == "unreachable" and attempt < _MAX_ATTEMPTS
            if not retryable:
                raise
            delay = attempt * _BACKOFF_STEP
            log.warning(
                "%s; pynixd did not answer, retry %d of %d in %.0fs",
                error,
                attempt,
                _MAX_ATTEMPTS,
                delay,
            )
            time.sleep(delay)


def run(wanted_spec: str, fallback: str | None, store_root: Path) -> int:
    """Fetch `wanted_spec` into `store_root`, or seed it from the image.

    `config.fallback` says where the fallback comes from and why.
    """
    wanted = store_path_for(wanted_spec)
    out_link = store_root / RESULT_PATH

    try:
        _fetch(wanted, store_root, out_link)
        running = wanted
    except store.StoreError as error:
        if fallback is None:
            log.error("%s, and this image names no fallback", error)
            return 1
        log.error("%s", error)
        log.warning("seeding %s from the image instead; this node will be behind", fallback)
        store.copy(fallback, store_root, out_link)
        running = fallback

    store.verify(running, store_root)
    role = os.environ.get("APPSTARTER_ROLE", "app")
    store.link(running, store_root / ROOT_DIR / role)
    State(wanted=wanted, running=running).write(store_root)

    if wanted != running:
        log.warning("store holds %s, and the deployment asks for %s", running, wanted)
    else:
        log.info("store holds %s", running)
    return 0
