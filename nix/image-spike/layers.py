#!/usr/bin/env python3
"""Print the layer table of a nix2container image, or of an OCI manifest.

    nix build --file . spike.n2c.image --print-out-paths
    ./nix/image-spike/layers.py <that path> [name ...]

A nix2container image is a JSON file that names store paths per layer, so the
table needs nothing built and no registry. Pass the group names in policy
order to label the rows; the layers after them are the remainder.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

MIB = 1024 * 1024


def paths_of(layer: dict) -> list[str]:
    return [p["path"] if isinstance(p, dict) else p for p in (layer.get("paths") or [])]


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__, file=sys.stderr)
        return 2

    image = json.loads(Path(sys.argv[1]).read_text())
    names = sys.argv[2:]
    layers = image["layers"]

    seen: dict[str, int] = {}
    total = 0
    duplicated = 0
    print(f"{'#':>2}  {'group':<14} {'MiB':>8}  {'paths':>6}  repeated")
    for index, layer in enumerate(layers):
        paths = paths_of(layer)
        repeated = sum(1 for p in paths if p in seen)
        duplicated += repeated
        total += layer["size"]
        label = names[index] if index < len(names) else f"rest-{index - len(names) + 1}"
        print(f"{index:>2}  {label:<14} {layer['size'] / MIB:>8.1f}  {len(paths):>6}  {repeated:>8}")
        for p in paths:
            seen.setdefault(p, index)

    print(f"{'':>2}  {'TOTAL':<14} {total / MIB:>8.1f}  {len(seen):>6}  {duplicated:>8}")
    print(f"\n{len(layers)} layers, {len(seen)} distinct store paths.")
    if duplicated:
        print(f"{duplicated} path(s) appear in more than one layer: the dedupe chain has a gap.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
