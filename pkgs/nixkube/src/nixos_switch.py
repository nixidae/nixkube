# SPDX-License-Identifier: Apache-2.0

"""Switch a NixOS host from a pod: the switch Job's program (#359).

One process, two halves. In the pod it runs from the store the CSI driver
mounts, and copies its own closure and the toplevel into the host's store.
Then it enters PID 1's namespaces and acts with the host's own nix, systemd
and switch-to-configuration. A store path names the same files on both sides,
so the interpreter keeps running from the host's copy of its closure.

Standard library only, and run by python3Minimal: that closure lands on every
host switched. One thread throughout: the kernel refuses to move a process
with more than one into another mount namespace.
"""

from __future__ import annotations

import json
import os
import selectors
import shutil
import subprocess
import sys
from enum import StrEnum
from pathlib import Path
from typing import Final


class Action(StrEnum):
    SWITCH = "switch"
    BOOT = "boot"
    DISARM = "disarm"
    REVERT = "revert"


# `switch`'s result when only a boot picks the system up. The Job reports it
# as its termination message, `REBOOT_MESSAGE` in nixos_rollout.py, and exits
# 0: an exit code would make a Job that did what it should a Failed one.
REBOOT_NEEDED: Final = 100
REBOOT_MESSAGE: Final = "reboot-needed"
# Opened before the program enters the host's mount namespace, where the
# container's copy is out of reach.
TERMINATION_LOG: Final = Path("/dev/termination-log")
REVERT_UNIT: Final = "nixkube-revert"
PROFILE: Final = "/nix/var/nix/profiles/system"
# Roots the target until the profile holds it, and through a revert that
# leaves the profile elsewhere. /run goes with the next boot.
TARGET_ROOT: Final = "/run/nixkube-target"
# Roots this program's closure on the host while it runs from there.
SELF_ROOT: Final = "/run/nixkube-switch"
# The host's /nix is mounted at /host/nix, and `root` says so, for the reads a
# unix:// store makes from the local filesystem.
HOST_STORE: Final = "unix:///host/nix/var/nix/daemon-socket/socket?root=/host"
HOST_PATH: Final = "/run/current-system/sw/bin"
# The CSI driver's environment: this program, nix, ssh, bash, the fake NSS.
POD_ENV: Final = Path("/nix/var/result")
# The mount namespace last: entering it replaces /proc, and the others are
# opened through /proc/1.
NAMESPACES: Final = ("cgroup", "ipc", "uts", "net", "pid", "mnt")
BOOT_PARTS: Final = ("kernel", "initrd", "kernel-modules")


class SwitchError(Exception):
    """A refusal or failure the Job reports as its exit code."""

    def __init__(self, message: str, code: int = 1) -> None:
        super().__init__(message)
        self.code = code


def store_path(path: Path) -> Path:
    """The store path holding `path`, which is under /nix/store."""
    if path.parts[:3] != ("/", "nix", "store") or len(path.parts) < 4:
        raise ValueError(f"{path} is not in /nix/store")
    return Path(*path.parts[:4])


def copy_argv(source: str, paths: list[str]) -> list[str]:
    # Root on the host's daemon socket is a trusted user: no signature needed.
    return [
        "nix",
        "--extra-experimental-features",
        "nix-command",
        "copy",
        "--no-check-sigs",
        "--from",
        source,
        "--to",
        HOST_STORE,
        *paths,
    ]


def readlink(path: Path) -> str | None:
    try:
        return os.readlink(path)
    except FileNotFoundError:
        return None


def boot_parts_changed(booted: Path, target: Path) -> list[str]:
    """What only a boot picks up: the parts that differ from the booted system's."""
    return [part for part in BOOT_PARTS if readlink(booted / part) != readlink(target / part)]


def journal_lines(buffer: bytes) -> tuple[bytes, list[tuple[str, str]]]:
    """Complete `journalctl --output=json` lines as (cursor, message), and the rest."""
    *lines, rest = buffer.split(b"\n")
    out: list[tuple[str, str]] = []
    for line in lines:
        if not line.strip():
            continue
        entry = json.loads(line)
        message = entry.get("MESSAGE")
        # journald sends a message that is not UTF-8 as a list of bytes.
        if isinstance(message, list):
            message = bytes(message).decode(errors="replace")
        out.append((entry["__CURSOR"], message or ""))
    return rest, out


def prepare_pod(env: Path) -> None:
    """What nix's ssh-ng store needs in a scratch image.

    ssh exits when its user has no passwd entry. nix waits for ssh's
    `LocalCommand=echo started`, which ssh runs with /bin/sh: nix's
    createSSHEnv sets SHELL=/bin/sh, so no other shell helps.
    """
    for name in ("passwd", "group", "nsswitch.conf"):
        source, target = env / "etc" / name, Path("/etc") / name
        if source.exists() and not target.exists():
            shutil.copyfile(source, target)
    Path("/bin").mkdir(exist_ok=True)
    if not Path("/bin/sh").exists():
        Path("/bin/sh").symlink_to(env / "bin" / "bash")
    Path("/tmp").mkdir(exist_ok=True)
    Path("/tmp").chmod(0o1777)


def enter_host() -> None:
    """Join PID 1's namespaces; setns into a mount namespace also moves / and the cwd."""
    threads = os.listdir("/proc/self/task")
    if len(threads) != 1:
        raise SwitchError(f"{len(threads)} threads: setns into a mount namespace needs one")
    fds = [os.open(f"/proc/1/ns/{ns}", os.O_RDONLY) for ns in NAMESPACES]
    for fd in fds:
        os.setns(fd)
        os.close(fd)
    os.chdir("/")
    os.environ["PATH"] = HOST_PATH


def run(*argv: str) -> None:
    subprocess.run(argv, check=True)


def output(*argv: str) -> str:
    return subprocess.run(argv, check=True, capture_output=True, text=True).stdout


def unit_active(unit: str) -> bool:
    return subprocess.run(["systemctl", "is-active", "--quiet", unit], check=False).returncode == 0


def realise(path: str, root: str) -> None:
    subprocess.run(["nix-store", "--realise", path, "--add-root", root], check=True, stdout=subprocess.DEVNULL)


def follow(unit: str, argv: list[str]) -> int:
    """Run `argv`, which starts `unit`, and print the unit's journal until it ends.

    The unit's output goes to the host journal, and this puts the same lines
    in the Job's log. journald writes a line some time after the unit wrote
    it, so once the unit ends, `--sync` flushes journald and a read after the
    last cursor printed collects the rest.
    """
    last = json.loads(output("journalctl", "--lines=1", "--output=json") or "{}").get("__CURSOR")
    after = [f"--after-cursor={last}"] if last else []
    query = ["journalctl", "--output=json", f"--unit={unit}"]
    follower = subprocess.Popen([*query, "--follow", *after], stdout=subprocess.PIPE)
    if follower.stdout is None:
        raise RuntimeError("journalctl started with no stdout pipe")
    child = subprocess.Popen(argv)
    pidfd = os.pidfd_open(child.pid)
    selector = selectors.DefaultSelector()
    selector.register(follower.stdout.fileno(), selectors.EVENT_READ)
    selector.register(pidfd, selectors.EVENT_READ)
    buffer = b""
    cursor = last

    def emit(chunk: bytes) -> None:
        nonlocal buffer, cursor
        buffer, lines = journal_lines(buffer + chunk)
        for _, message in lines:
            print(message, flush=True)
        if lines:
            cursor = lines[-1][0]

    running = True
    while running:
        for key, _ in selector.select():
            if key.fd == pidfd:
                running = False
            elif chunk := os.read(key.fd, 65536):
                emit(chunk)
            else:
                selector.unregister(key.fd)
    code = child.wait()
    os.close(pidfd)
    run("journalctl", "--sync")
    follower.terminate()
    emit(follower.stdout.read())
    follower.wait()
    # A follower stopped mid-line leaves half an entry; the read below starts
    # after the last whole one.
    buffer = b""
    rest = [f"--after-cursor={cursor}"] if cursor else []
    emit(output(*query, *rest).encode() + b"\n")
    return code


def switch(toplevel: str, revert_delay: str) -> int:
    previous = os.readlink("/run/current-system")
    if previous == toplevel:
        print(f"already running {toplevel}", flush=True)
        return 0
    if unit_active(f"{REVERT_UNIT}.timer"):
        raise SwitchError("a revert is armed: an earlier switch is not confirmed")
    realise(toplevel, TARGET_ROOT)
    # Nothing has changed yet: the controller drains the node and runs `boot`.
    if parts := boot_parts_changed(Path(os.readlink("/run/booted-system")), Path(toplevel)):
        print(f"{', '.join(parts)} differ from the booted system's: a reboot is needed", flush=True)
        return REBOOT_NEEDED
    # Armed before the profile moves, so a failure after this point always has
    # a way back. A switch that fails starts it at once; one the controller
    # cannot confirm is reverted by the controller, or by the timer when no
    # pod can run on the node any more.
    back = f"{previous}/sw/bin/nix-env --profile {PROFILE} --set {previous} && exec {previous}/bin/switch-to-configuration switch"
    run(
        "systemd-run",
        f"--unit={REVERT_UNIT}",
        f"--on-active={revert_delay}",
        "--timer-property=AccuracySec=1s",
        "--service-type=oneshot",
        "--property=KillMode=process",
        "--collect",
        "--quiet",
        f"{previous}/sw/bin/sh",
        "-c",
        back,
    )
    run("nix-env", "--profile", PROFILE, "--set", toplevel)
    unit = f"nixkube-switch-{Path(toplevel).name[:12]}"
    print(f"switching to {toplevel}; host journal: journalctl --unit {unit}", flush=True)
    code = follow(
        unit,
        [
            f"{toplevel}/sw/bin/systemd-run",
            f"--unit={unit}",
            "--wait",
            "--collect",
            "--quiet",
            "--service-type=exec",
            "--property=KillMode=process",
            f"{toplevel}/bin/switch-to-configuration",
            "switch",
        ],
    )
    if code != 0:
        print(f"switch-to-configuration failed; reverting to {previous}", file=sys.stderr, flush=True)
        run("systemctl", "start", f"{REVERT_UNIT}.service")
        run("systemctl", "stop", f"{REVERT_UNIT}.timer")
        return 1
    return 0


def boot(toplevel: str) -> int:
    # No revert is armed: a timer does not live through the reboot.
    if os.readlink("/run/booted-system") == toplevel:
        print(f"already booted {toplevel}", flush=True)
        return 0
    realise(toplevel, TARGET_ROOT)
    run("nix-env", "--profile", PROFILE, "--set", toplevel)
    run(f"{toplevel}/bin/switch-to-configuration", "boot")
    # Later, so this Job completes first; the controller then waits for a new
    # boot ID.
    run(
        "systemd-run",
        "--unit=nixkube-reboot",
        "--on-active=10",
        "--timer-property=AccuracySec=1s",
        "--collect",
        "--quiet",
        "systemctl",
        "reboot",
    )
    print(f"rebooting into {toplevel} in 10s", flush=True)
    return 0


def disarm(toplevel: str) -> int:
    # The timer first: stopping it does not stop a revert it already started,
    # so a check before the stop can pass while the host reverts. A switch to
    # the running system arms nothing, and stop on a unit that is not loaded
    # fails.
    if unit_active(f"{REVERT_UNIT}.timer"):
        run("systemctl", "stop", f"{REVERT_UNIT}.timer")
    state = output("systemctl", "show", "--property=ActiveState", "--value", f"{REVERT_UNIT}.service").strip()
    if state in ("activating", "active", "deactivating"):
        raise SwitchError("the revert is running: the switch was not confirmed in time")
    # The timer may have reverted the host before the switch Job could
    # report, and then a confirmed kubelet runs the old system.
    if (running := os.readlink("/run/current-system")) != toplevel:
        raise SwitchError(f"running {running}, not {toplevel}: the switch was reverted")
    return 0


def revert() -> int:
    if not unit_active(f"{REVERT_UNIT}.timer"):
        raise SwitchError("no revert is armed")
    run("systemctl", "start", f"{REVERT_UNIT}.service")
    run("systemctl", "stop", f"{REVERT_UNIT}.timer")
    return 0


def act(action: Action, toplevel: str, revert_delay: str) -> int:
    match action:
        case Action.SWITCH:
            return switch(toplevel, revert_delay)
        case Action.BOOT:
            return boot(toplevel)
        case Action.DISARM:
            return disarm(toplevel)
        case Action.REVERT:
            return revert()


def main() -> None:
    try:
        action = Action(os.environ["ACTION"])
    except ValueError:
        print(f"unknown ACTION: {os.environ['ACTION']}", file=sys.stderr)
        sys.exit(2)
    toplevel = os.environ["TOPLEVEL"]
    # Set when the controller built the toplevel, so pynixd holds it. A
    # prebuilt one reaches the host through the host's own substituters.
    fetch_from = os.environ.get("FETCH_FROM", "")
    me = str(store_path(Path(__file__).resolve()))
    prepare_pod(POD_ENV)
    os.environ["PATH"] = str(POD_ENV / "bin")
    try:
        # From the pod's own store: the CSI driver put it there to run it.
        run(*copy_argv("local", [me]))
        if fetch_from and action in (Action.SWITCH, Action.BOOT):
            run(*copy_argv(fetch_from, [toplevel]))
        termination = TERMINATION_LOG.open("w")
        enter_host()
        realise(me, SELF_ROOT)
        code = act(action, toplevel, os.environ.get("REVERT_DELAY", ""))
        if code == REBOOT_NEEDED:
            termination.write(REBOOT_MESSAGE)
            code = 0
        termination.close()
        sys.exit(code)
    except SwitchError as e:
        print(e, file=sys.stderr, flush=True)
        sys.exit(e.code)
    except subprocess.CalledProcessError as e:
        print(f"{' '.join(map(str, e.cmd))} exited {e.returncode}", file=sys.stderr, flush=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
