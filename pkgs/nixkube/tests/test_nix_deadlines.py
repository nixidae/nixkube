# SPDX-License-Identifier: Apache-2.0

"""No nix call on a request path may run without a deadline.

Issue #38 measured the failure in the GC loop: a call that never returns
ends that work until the pod restarts, and the restart re-enters the same
call. The calls in `src/nix/` are worse placed than those -- they run
inside `NodePublishVolume` and inside the NRI build task, and neither has
anything above it that gives up.

A structural check, because the rule is about calls that do not exist yet.
A reviewer catches the missing `timeout=` on the line they are reading; this
catches the one nobody read.
"""

import ast
from pathlib import Path

import pytest

import src.nix

# Everything here that starts a process on behalf of a caller. The four from
# `subprocessing`, plus `pipe_commands` (which runs two of its own) and
# `subprocess.run` (synchronous, so it blocks the whole loop).
#
# Not `open_process`: `pipe_commands` calls it twice inside a scope that
# already carries the deadline its own caller passed, and flagging those two
# would say the boundary is one level lower than it is.
RUNNERS = {
    "try_captured",
    "try_console",
    "run_captured",
    "run_console",
    "pipe_commands",
    "run",  # subprocess.run
}

MODULES = sorted(path for path in Path(src.nix.__file__).parent.glob("*.py") if path.name != "__init__.py")


def _calls_without_timeout(source: str) -> list[tuple[str, int]]:
    tree = ast.parse(source)
    missing = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
        if name not in RUNNERS:
            continue
        if not any(keyword.arg == "timeout" for keyword in node.keywords):
            missing.append((name, node.lineno))
    return missing


def test_the_modules_were_actually_found() -> None:
    """A glob that matches nothing passes every check below it."""
    assert MODULES, "found no modules to check under src/nix"
    assert {path.name for path in MODULES} >= {"build.py", "verify.py", "closure.py"}


@pytest.mark.parametrize("module", MODULES, ids=lambda p: p.name)
def test_every_nix_call_carries_a_deadline(module: Path) -> None:
    missing = _calls_without_timeout(module.read_text())
    assert not missing, f"{module.name} runs nix with no timeout: " + ", ".join(
        f"{name} at line {line}" for name, line in missing
    )


def test_the_check_can_fail() -> None:
    """The negative control for the parametrised check above.

    Without it, a broken matcher -- a renamed runner, an AST change -- reports
    a clean sweep over calls it never recognised.
    """
    assert _calls_without_timeout("await try_captured('nix', 'build')") == [("try_captured", 1)]
    assert _calls_without_timeout("await try_captured('nix', timeout=1)") == []
