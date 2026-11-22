"""The node runs from the guest's own /nix, as on a NixOS host. Issue #25.

The guest is NixOS, so `nixkube-host-check` labels it, the separate-store
pod steps aside, and `nix-node-host` serves it with the guest's store and a
nix-daemon of nixkube's own. This asks what that has to mean:

  - the node carries `nixkube/host=nixos`, and the separate DaemonSet
    schedules nothing on it
  - nixkube's own nix-daemon serves the guest's store on nixkube's socket,
    and the guest's daemon keeps the default socket
  - what the node runs, and what a volume reads, are rooted for the host's
    GC: Nix refuses to delete them, and deletes an unrooted path at once
  - the separate store at `hostMountPath`, which the step-aside pod's
    hostPath volumes created, is retired once nothing mounts from it
"""

from kr8s.asyncio.objects import DaemonSet, Node
from nixkube_uml import NAMESPACE, READY_TIMEOUT, check_resident, cluster, probe, raw
from vivarium_k8s import until
from vivarium_runner import Machine, MachineError, Machines

STILL_ALIVE = "since it is still alive"
# kubenix/daemonset.nix, NIX_DAEMON_SOCKET_PATH of the host DaemonSet.
OWN_SOCKET = "/nix/var/nix-csi/daemon-socket/socket"


async def refuses_delete(cp: Machine, path: str, why: str) -> None:
    done = await cp.execute(f"nix-store --delete {path} 2>&1", timeout=300)
    if done.returncode == 0 or STILL_ALIVE not in done.stdout:
        raise MachineError(f"[cp] the host's GC could take {why}, {path}: rc={done.returncode} {done.stdout}")
    print(f"[nixkube] the host's GC keeps {why}", flush=True)


async def test(vms: Machines) -> None:
    cp, settings = vms.cp, vms.settings
    api = await cluster(vms)

    node = raw(await Node.get("cp", api=api))
    label = node["metadata"].get("labels", {}).get("nixkube/host")
    if label != "nixos":
        raise MachineError(f"[cp] the node's nixkube/host label is {label!r}, not nixos")
    separate = raw(await DaemonSet.get("nix-node-separate", namespace=NAMESPACE, api=api))
    scheduled = separate["status"].get("desiredNumberScheduled", 0)
    if scheduled != 0:
        raise MachineError(f"[cp] the separate DaemonSet still schedules {scheduled} pods")
    print("[nixkube] labelled nixos; the separate DaemonSet schedules nothing", flush=True)

    # Read through the agent, not a shell: a shell's own command line holds
    # the pattern, and a grep for it finds the shell.
    # pynixd's own daemon runs `--store /data`, and is not this.
    own = [p for p in await cp.processes() if p["cmdline"].startswith("nix daemon --store local")]
    if len(own) != 1:
        raise MachineError(f"[cp] nixkube should run one nix-daemon of its own, not {own}")
    await cp.succeed(f"test -S {OWN_SOCKET}")
    await cp.succeed(f"nix --extra-experimental-features nix-command store info --store unix://{OWN_SOCKET}")
    await cp.succeed("systemctl is-active nix-daemon.socket")
    print(
        f"[nixkube] nixkube's daemon serves {OWN_SOCKET}; the guest's keeps the default socket",
        flush=True,
    )

    # The negative control: an unrooted path goes at once, so a refusal
    # below is a root and not a delete that cannot work here.
    loose = (await cp.succeed("echo loose > /tmp/loose && nix-store --add /tmp/loose")).stdout.strip()
    await cp.succeed(f"nix-store --delete {loose}")
    print(f"[nixkube] an unrooted path is deleted: {loose}", flush=True)
    running = (await cp.succeed("readlink /nix/var/nix/gcroots/appstarter/node")).stdout.strip()
    await refuses_delete(cp, running, "the node's running environment")
    await refuses_delete(cp, settings["workloadStorePath"], "the resident pod's volume")

    async def retired() -> tuple[bool, str]:
        rc = (await cp.execute(f"test -e {settings['hostMountPath']}")).returncode
        return rc != 0, f"{settings['hostMountPath']} is still there"

    await until("the separate store to be retired", retired, READY_TIMEOUT, cp)
    print(f"[nixkube] {settings['hostMountPath']} is retired", flush=True)

    await check_resident(api, cp, settings, "the host store checks")
    await probe(api, cp, settings, "the host store checks")
