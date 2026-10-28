# SPDX-License-Identifier: Apache-2.0
"""`appstarter init` and `appstarter run`."""

import argparse
import logging
import os
import sys
from pathlib import Path

from . import config, seed, setup, start, store

# Both modes take the prefix a `/nix/...` path resolves under, and the two
# containers of a pod see the same store under different ones.
#
# The initContainer mounts the whole node directory at /nix-volume, because
# the image already has its own store at /nix and a container cannot have two.
# It is therefore a chroot store, and `/nix/var/result` is really
# /nix-volume/nix/var/result.
#
# The main container mounts only the store, at /nix, so the same path needs no
# prefix at all. Setting this to "/nix" reads that store at /nix/nix and finds
# nothing.
_INIT_STORE = Path("/nix-volume")
_RUN_STORE = Path("/")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="appstarter", description=__doc__)
    modes = parser.add_subparsers(dest="mode", required=True)

    init = modes.add_parser("init", help="fetch the wanted version into the node's store")
    init.add_argument(
        "--store",
        type=Path,
        default=Path(os.environ.get("APPSTARTER_STORE", _INIT_STORE)),
        help="the prefix the node's store paths resolve under",
    )

    run = modes.add_parser("run", help="start the application the store holds")
    run.add_argument("program", help="the program to exec out of the store")
    run.add_argument("argv", nargs=argparse.REMAINDER, help="its arguments")
    run.add_argument(
        "--store",
        type=Path,
        default=Path(os.environ.get("APPSTARTER_STORE", _RUN_STORE)),
        help="the prefix the store paths resolve under",
    )

    return parser


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=os.environ.get("APPSTARTER_LOG_LEVEL", "INFO"),
        format="%(name)s %(levelname)s %(message)s",
    )
    args = _parser().parse_args(argv)

    if args.mode == "run":
        return start.run(args.program, args.argv, args.store)

    wanted = os.environ.get("APPSTARTER_WANTED")
    if not wanted:
        print("APPSTARTER_WANTED is unset; nothing to fetch", file=sys.stderr)
        return 2
    try:
        setup.prepare()
        return seed.run(wanted, config.fallback(), args.store)
    except (store.StoreError, KeyError, ValueError, OSError) as error:
        print(f"appstarter init: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
