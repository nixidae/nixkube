# SPDX-License-Identifier: Apache-2.0

"""Fail when an attribute's build closure needs a foreign architecture.

GitHub runners have no binfmt. A workstation with
`extra-platforms = aarch64-linux` emulates instead, so this fault is invisible
in development and appears first on a runner -- as it did:
`kubenixApply.manifestJSONFile` needed 3120 aarch64 derivations on x86_64.

Asks the derivations, not the store. `nix build` passes whenever the foreign
paths happen to be present already.
"""

import argparse
import json
import subprocess
import sys
from collections import Counter

# Nix runs these itself (fetchurl and friends). They have no builder and are
# not a foreign build.
PORTABLE = {"builtin"}


def run(*args: str) -> str:
    result = subprocess.run(args, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise RuntimeError(f"{' '.join(args)} failed:\n{result.stderr.strip()}")
    return result.stdout


def systems_of(attr: str, file: str) -> Counter[str]:
    drv = run("nix", "path-info", "--derivation", "--file", file, attr).strip()
    raw = json.loads(run("nix", "derivation", "show", "--recursive", drv))
    drvs = raw.get("derivations", raw)
    return Counter(v.get("system", "?") for v in drvs.values() if isinstance(v, dict))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("attributes", nargs="+")
    parser.add_argument("--file", default=".")
    parser.add_argument(
        "--native",
        help="System to require. Defaults to this machine's.",
    )
    args = parser.parse_args()

    native = args.native or run("nix", "eval", "--impure", "--raw", "--expr", "builtins.currentSystem")
    print(f"native system: {native}")

    failed = False
    for attr in args.attributes:
        try:
            counts = systems_of(attr, args.file)
        except RuntimeError as exc:
            print(f"  ERROR   {attr}: {exc}", file=sys.stderr)
            failed = True
            continue
        foreign = {system: n for system, n in counts.items() if system != native and system not in PORTABLE}
        if foreign:
            detail = ", ".join(f"{s}={n}" for s, n in sorted(foreign.items()))
            print(f"  FOREIGN {attr} -> {detail}", file=sys.stderr)
            failed = True
        else:
            print(f"  ok      {attr}")

    if failed:
        print(
            "\nThese attributes need a machine this runner is not.\n"
            "A runner has no emulation, so the build cannot succeed there.",
            file=sys.stderr,
        )
        return 1

    print(f"all attributes build natively on {native}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
