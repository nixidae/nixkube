# SPDX-License-Identifier: Apache-2.0

import kr8s
import structlog
from kr8s.asyncio.objects import Pod

from .constants import BUILDERS_ENABLED, BUILDERS_SERVICE, NAMESPACE
from .kube_docs import pod_doc

logger = structlog.get_logger("nixkube.builders")


def is_usable(pod: Pod) -> bool:
    """Can this builder take work right now?

    **Ready, not Running.** A pod is Running from the moment its containers
    start, and the builder's sshd answers some time after that. A builder in
    that window still contributes a URI, and one URI is enough for
    `build_builder_args` to add `--max-jobs 0` -- which turns off local
    building. So a single not-yet-ready builder makes every build on this
    node fail with "no substituter that can build it", while the thing meant
    to serve it is not listening.

    An empty list is the safe answer: no `--max-jobs 0`, and the node builds
    for itself.

    **Ready only beats Running when the pod has a readiness probe.** With no
    probe the kubelet marks a container ready as soon as it starts, and this
    rule collapses back into the old one. This repository ships no builder
    manifest -- `BUILDERS_ENABLED` is false by default -- so whoever deploys
    builder pods has to give the container a probe on the SSH port for any of
    this to mean anything.
    """
    status = pod_doc(pod).get("status") or {}
    if status.get("phase") != "Running":
        return False
    return any(
        condition.get("type") == "Ready" and condition.get("status") == "True"
        for condition in status.get("conditions") or []
    )


async def get_builder_uris() -> list[str]:
    """Query k8s API for builder pods, return list of SSH URIs for --builders flag."""
    if not BUILDERS_ENABLED:
        return []

    try:
        # Use kr8s to query pods with label selector
        # kr8s.asyncio.get() returns an async generator, iterate with async for
        uris = []
        async for pod in kr8s.asyncio.get("pods", namespace=NAMESPACE, label_selector="app.kubernetes.io/name=builder"):
            try:
                if is_usable(pod):
                    pod_name = pod["metadata"]["name"]
                    uri = f"ssh-ng://nix@{pod_name}.{BUILDERS_SERVICE}.{NAMESPACE}.svc.cluster.local"
                    uris.append(uri)
            except (KeyError, TypeError, AttributeError):
                # Skip pods with missing or malformed metadata
                continue

        logger.debug("discovered_builders", count=len(uris), uris=uris)
        return uris
    except Exception:
        logger.warning("builder_discovery_failed", exc_info=True)
        return []


def build_builder_args(uris: list[str]) -> list[str]:
    """Build nix command arguments for using builder pods."""
    if not uris:
        return []

    args = ["--max-jobs", "0"]
    for uri in uris:
        args.extend(["--builders", uri])
    args.append("--builders-use-substitutes")
    return args
