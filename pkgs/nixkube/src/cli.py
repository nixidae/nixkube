# SPDX-License-Identifier: Apache-2.0

import json
import logging
import sys
from pathlib import Path

import anyio
import structlog

from . import metrics
from .cache import cache_probe_loop
from .constants import (
    BUILDERS_ENABLED,
    ENABLE_COMPAT_DRIVER,
    HOST_MOUNT_PATH,
    HOST_STORE,
    KUBE_NODE_NAME,
    METRICS_ADDR,
    METRICS_ENABLED,
    METRICS_PORT,
    NAMESPACE,
    NIX_BUILD_TIMEOUT,
    NRI_ENABLED,
    NRI_PLUGIN_IDX,
    NRI_PLUGIN_NAME,
    PYNIXD_ENABLED,
    VERIFY_STORE_PATHS,
)
from .csi.server import csi_serve
from .gc_task import gc_loop
from .nix_daemon import supervise_nix_daemon
from .nri.server import nri_serve
from .retire import retire_loop
from .startup import run_setup
from .supervision import supervised


def configure_structlog(renderer: str = "json") -> None:
    """Configure structlog with stdlib integration and chosen renderer.

    Sets up a shared processor chain for both structlog and stdlib loggers
    (third-party libraries like kr8s, grpclib). The ProcessorFormatter routes
    all log records through structlog processors so output format is uniform
    regardless of source.

    Args:
        renderer: "json" (default, for production/Kubernetes) or "console" (for dev)
    """
    shared_processors: list[structlog.types.Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_logger_name,
        structlog.stdlib.add_log_level,
        structlog.stdlib.PositionalArgumentsFormatter(),
        structlog.processors.StackInfoRenderer(),
    ]

    if renderer == "console":
        # Auto-detect colors: use ANSI only when stdout is a real terminal.
        # In a Kubernetes container stdout is a pipe (isatty=False), so colors are
        # disabled and the output stays plain text — safe for log aggregators and
        # copy-paste. Colors are enabled automatically when running in a local terminal.
        final_renderer: structlog.types.Processor = structlog.dev.ConsoleRenderer(colors=sys.stdout.isatty())
    elif renderer == "logfmt":
        # Logfmt: key=value pairs on a single line, human-readable and machine-parseable.
        final_renderer = structlog.processors.LogfmtRenderer()
    else:
        # JSON (default). No asctime — Kubernetes captures stdout with its own timestamps.
        # default=str converts non-serializable types (e.g. Path values) to strings.
        final_renderer = structlog.processors.JSONRenderer(default=str)

    structlog.configure(
        processors=[*shared_processors, structlog.stdlib.ProcessorFormatter.wrap_for_formatter],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    # ConsoleRenderer formats exceptions natively; ExceptionRenderer is only
    # needed for machine-readable formats (json/logfmt) to serialize exc_info.
    pre_render: list[structlog.types.Processor] = (
        [] if renderer == "console" else [structlog.processors.ExceptionRenderer()]
    )

    formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=shared_processors,
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            *pre_render,
            final_renderer,
        ],
    )

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)
    root_logger = logging.getLogger()
    root_logger.handlers.clear()
    root_logger.addHandler(handler)


def log_effective_log_config(renderer: str) -> None:
    """Print the effective logging configuration for debugging.

    Uses print() instead of logger to ensure output is always visible
    regardless of configured log levels.
    """
    root = logging.getLogger()
    lines = [
        "Effective logging configuration:",
        f"  structlog renderer: {renderer}",
        f"  Root logger: level={logging.getLevelName(root.level)}",
    ]
    for name in sorted(logging.Logger.manager.loggerDict.keys()):
        log = logging.getLogger(name)
        if log.level != logging.NOTSET:
            lines.append(f"  Logger '{name}': level={logging.getLevelName(log.level)}")
    print("\n".join(lines))


def log_effective_app_config() -> None:
    """Print the effective application configuration for debugging.

    Uses print() instead of logger to ensure output is always visible
    regardless of configured log levels.
    """
    print(
        "Effective application configuration:\n"
        f"  {NIX_BUILD_TIMEOUT=}\n"
        f"  {PYNIXD_ENABLED=}\n"
        f"  {BUILDERS_ENABLED=}\n"
        f"  {VERIFY_STORE_PATHS=}\n"
        f"  {HOST_MOUNT_PATH=}\n"
        f"  {NRI_PLUGIN_NAME=}\n"
        f"  {NRI_PLUGIN_IDX=}\n"
        f"  {NAMESPACE=}\n"
        f"  {KUBE_NODE_NAME=}"
    )


def store_loops(*, host_store: bool) -> frozenset[str]:
    """The loops that look after the store.

    A host-store node runs no nix-daemon and no sweep: the socket is the
    host's nix-daemon, and a second daemon would take it, and the sweep
    deletes every unrooted path older than an hour and copies the whole
    store to the cache -- the host's users' paths and the host's whole store.
    It retires the separate store it used before instead. Issue #25.
    """
    if host_store:
        return frozenset({"retire"})
    return frozenset({"nix-daemon", "gc"})


async def async_main():
    """Run one-shot setup, then supervise nix-daemon, GC, CSI, and NRI concurrently.

    Loads logging configuration from /etc/nix/logging.json (ConfigMap-mounted).
    Runs run_setup() synchronously first, then spawns supervised tasks for
    nix-daemon, GC loop, CSI server(s), and optionally the NRI plugin.

    One task group holds all of them, so a `CrashLoopError` out of any one
    cancels the rest and exits the process, and Kubernetes restarts the pod
    with backoff. That is what the group buys over `asyncio.gather`, which
    propagates the first exception and leaves its siblings running.
    """
    config: dict = {}
    # `anyio.Path`, not `pathlib.Path`: `open()` in an async function blocks
    # the event loop. It costs nothing here, since nothing else is running
    # yet, but the rule is worth keeping whole -- this is the only place in
    # the service that reads a file from async code.
    logging_config_path = anyio.Path("/etc/nix/logging.json")
    config_present = await logging_config_path.exists()

    if config_present:
        config = json.loads(await logging_config_path.read_text())

    renderer = config.pop("renderer", "json")
    configure_structlog(renderer)

    # Apply per-logger levels from config
    for name, settings in config.get("loggers", {}).items():
        if isinstance(settings, dict) and "level" in settings:
            logging.getLogger(name).setLevel(settings["level"])

    root_level = config.get("root", {}).get("level", "WARNING")
    logging.getLogger().setLevel(root_level)

    logger = structlog.get_logger("nixkube")
    if config_present:
        logger.info("logging_config_loaded", path=str(logging_config_path))
    else:
        logger.info("logging_config_fallback", path=str(logging_config_path))

    log_effective_log_config(renderer)
    log_effective_app_config()

    logger.info("nri_plugin", enabled=NRI_ENABLED)
    logger.info(
        "csi_drivers",
        drivers=["nixkube"] + (["nix.csi.store"] if ENABLE_COMPAT_DRIVER else []),
    )

    if METRICS_ENABLED:
        metrics.serve(port=METRICS_PORT, addr=METRICS_ADDR)

    await run_setup()

    async with anyio.create_task_group() as tg:
        loops = store_loops(host_store=HOST_STORE)
        logger.info("store_loops", host_store=HOST_STORE, loops=sorted(loops))
        if "nix-daemon" in loops:
            tg.start_soon(supervise_nix_daemon, name="nix-daemon")
        if "gc" in loops:
            tg.start_soon(gc_loop, name="gc")
        if "retire" in loops:
            tg.start_soon(retire_loop, name="retire")
        tg.start_soon(cache_probe_loop, name="cache-probe")
        # Not supervised. It has nothing to crash on, and a restart loop
        # around a sampler would report on itself.
        tg.start_soon(metrics.LoopLagMonitor().run, name="loop-lag")
        tg.start_soon(
            supervised,
            lambda: csi_serve(plugin_name="nixkube", socket_path=Path("/csi/nixkube/csi.sock")),
            "csi",
            name="csi",
        )
        if ENABLE_COMPAT_DRIVER:
            tg.start_soon(
                supervised,
                lambda: csi_serve(
                    plugin_name="nix.csi.store",
                    socket_path=Path("/csi/nix.csi.store/csi.sock"),
                ),
                "csi-compat",
                name="csi-compat",
            )
        if NRI_ENABLED:
            tg.start_soon(supervised, nri_serve, "nri", name="nri")


def main():
    """Entry point for the nixkube daemon."""
    anyio.run(async_main, backend="asyncio")


if __name__ == "__main__":
    main()
