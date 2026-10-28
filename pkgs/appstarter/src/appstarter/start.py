# SPDX-License-Identifier: Apache-2.0
"""`appstarter run`: say which version this is, then become the application."""

import logging
import os
from pathlib import Path

from .config import RESULT_PATH, State

log = logging.getLogger("appstarter.run")


def run(program: str, argv: list[str], store_root: Path) -> int:
    """`exec` `program` out of the store, with the skew in its environment.

    **`exec`, not a child process.** tini is pid 1 and its child has to be the
    application, or signals and reaping stop working and this becomes a second
    init inside a container that already has one.

    The two variables are the whole interface to the application. One bit
    saying "the fetch failed" is not enough for it to report anything useful:
    the event text needs both versions, and so does anything that decides
    whether to take the new one.
    """
    entry = store_root / RESULT_PATH / "bin" / program
    if not os.path.lexists(entry):
        log.error("%s does not exist; `appstarter init` did not run, or failed", entry)
        return 1

    state = State.read(store_root)
    if state is None:
        log.warning("no state from `appstarter init`; starting %s without a version", entry)
    else:
        os.environ["APPSTARTER_WANTED_STORE_PATH"] = state.wanted
        os.environ["APPSTARTER_RUNNING_STORE_PATH"] = state.running
        if state.degraded:
            log.warning(
                "starting %s, which is %s; the deployment asks for %s",
                program,
                state.running,
                state.wanted,
            )

    os.execv(str(entry), [program, *argv])
    return 0
