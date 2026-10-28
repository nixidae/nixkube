# SPDX-License-Identifier: Apache-2.0

"""How a pod sees its closure: the decisions, and the chart drawn from them.

CSI's publish, NRI's mount and `prepare_volume` call these functions, and
`docs/presentation.dot` is rendered from them by enumerating every input:

    python -m src.presentation > docs/presentation.dot

The `presentationChartIsCurrent` check fails when the committed chart and
these functions disagree. `docs/store-presentation.md` says what each
presentation costs.

Pure, with no imports from the daemon, so the chart renders without a node.
"""

from __future__ import annotations

import itertools
import sys
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Final


class Presentation(StrEnum):
    COMPOSEFS = "composefs"
    FARM = "farm"
    HARDLINKS = "hardlinks"
    REFUSED = "refused"


def csi(*, enabled: bool, kernel: bool, user_namespace: bool, host_store: bool) -> Presentation:
    """A CSI volume.

    A pod with `hostUsers: false` gets an idmapped mount of every volume,
    and the kernel refuses MOUNT_ATTR_IDMAP on an overlay: measured on 7.2,
    CRI-O's runc failed "failed to set MOUNT_ATTR_IDMAP ... invalid
    argument". A host-store node has no hardlink tree: a hardlink out of the
    host's store fails with EXDEV. No farm: kubelet's `subPath` bind does not
    carry submounts."""
    if enabled and kernel and not user_namespace:
        return Presentation.COMPOSEFS
    if host_store:
        return Presentation.REFUSED
    return Presentation.HARDLINKS


def nri(*, vm: bool, enabled: bool, kernel: bool, bind_farm: bool, host_store: bool) -> Presentation:
    """An NRI container's /nix, before `farm_fallback`.

    A VM runtime reads /nix through virtio-fs, which serves files, so it gets
    a hardlink tree, and a host-store node has none to give. No user
    namespace rule, unlike `csi`: nobody idmaps this mount, because it
    reaches the container after the runtime made it."""
    if vm:
        return Presentation.REFUSED if host_store else Presentation.HARDLINKS
    if enabled and kernel:
        return Presentation.COMPOSEFS
    if bind_farm or host_store:
        return Presentation.FARM
    return Presentation.HARDLINKS


def farm_fallback(*, fits: bool, host_store: bool) -> Presentation:
    """A farm that would not leave headroom under `fs.mount-max` becomes a
    hardlink tree, where the node has one."""
    if fits:
        return Presentation.FARM
    return Presentation.REFUSED if host_store else Presentation.HARDLINKS


def nri_chart(
    *,
    vm: bool,
    enabled: bool,
    kernel: bool,
    bind_farm: bool,
    host_store: bool,
    farm_fits: bool,
) -> Presentation:
    """`nri` then `farm_fallback`, as `_build_and_mount` and
    `prepare_volume` apply them one after the other."""
    shown = nri(
        vm=vm,
        enabled=enabled,
        kernel=kernel,
        bind_farm=bind_farm,
        host_store=host_store,
    )
    if shown is Presentation.FARM:
        return farm_fallback(fits=farm_fits, host_store=host_store)
    return shown


# Each input as the chart asks it, in the order the chart asks.
QUESTIONS: Final = {
    "vm": "VM runtime\n(Kata)?",
    "host_store": "host-store\nnode?",
    "enabled": "composefs\noption on?",
    "kernel": "kernel passes the\ncomposefs probe?",
    "user_namespace": "hostUsers: false?",
    "bind_farm": "NRI_BIND_FARM?",
    "farm_fits": "farm fits under\nfs.mount-max?",
}

COLOURS: Final = {
    Presentation.COMPOSEFS: "palegreen",
    Presentation.FARM: "lightblue",
    Presentation.HARDLINKS: "khaki",
    Presentation.REFUSED: "lightpink",
}


@dataclass(frozen=True)
class Leaf:
    shown: Presentation


@dataclass(frozen=True)
class Question:
    name: str
    yes: Question | Leaf
    no: Question | Leaf


def tree(decide: Callable[..., Presentation], names: list[str], fixed: dict[str, bool]) -> Question | Leaf:
    """The decision tree of `decide`, asking `names` in order and skipping
    a question whose answer changes nothing."""
    rest = [n for n in names if n not in fixed]
    if not rest:
        return Leaf(decide(**fixed))
    seen = {
        decide(**fixed, **dict(zip(rest, values, strict=True)))
        for values in itertools.product((False, True), repeat=len(rest))
    }
    if len(seen) == 1:
        return Leaf(seen.pop())
    first, *_ = rest
    yes = tree(decide, names, {**fixed, first: True})
    no = tree(decide, names, {**fixed, first: False})
    if yes == no:
        return yes
    return Question(first, yes, no)


def dot_lines(prefix: str, title: str, root: Question | Leaf) -> list[str]:
    lines = [
        f"  subgraph cluster_{prefix} {{",
        f'    label="{title}";',
        "    fontname=sans;",
    ]
    # One node for each distinct subtree, so the tree draws as a DAG.
    names: dict[Question | Leaf, str] = {}

    def node(at: Question | Leaf) -> str:
        if at in names:
            return names[at]
        if isinstance(at, Leaf):
            name = f"{prefix}_{at.shown}"
            names[at] = name
            lines.append(
                f'    {name} [label="{at.shown}", shape=box, style="rounded,filled", fillcolor={COLOURS[at.shown]}];'
            )
            return name
        name = f"{prefix}_q{len(names)}"
        names[at] = name
        label = QUESTIONS[at.name].replace("\n", "\\n")
        lines.append(f'    {name} [label="{label}", shape=diamond];')
        lines.append(f'    {name} -> {node(at.yes)} [label="yes"];')
        lines.append(f'    {name} -> {node(at.no)} [label="no"];')
        return name

    node(root)
    lines.append("  }")
    return lines


def chart() -> str:
    csi_names = ["enabled", "kernel", "user_namespace", "host_store"]
    nri_names = ["vm", "enabled", "kernel", "bind_farm", "host_store", "farm_fits"]
    lines = [
        "// Rendered by `python -m src.presentation`. Do not edit.",
        "digraph presentation {",
        "  fontname=sans;",
        "  node [fontname=sans];",
        "  edge [fontname=sans];",
        *dot_lines("csi", "CSI volume", tree(csi, csi_names, {})),
        *dot_lines("nri", "NRI /nix", tree(nri_chart, nri_names, {})),
        "}",
    ]
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    sys.stdout.write(chart())
