# SPDX-License-Identifier: Apache-2.0

# A writable CSI farm (`storeVolume`), whose store is the pod's own emptyDir,
# through nixkube's own functions, as root. nixidae/nixidae#1560: a build pod's
# outputs count against the pod's disk, and its inputs stay the node's.
#
#   - kubelet's directory is a shared mount here, as on a node. The control:
#     a bind of the emptyDir that is not made private carries each input bind
#     back to the emptyDir's own path
#   - nixkube's bind is private: the emptyDir's own path holds stubs, the
#     database and what the pod wrote, and nothing of the closure
#   - the pod's user writes the store and the database; each input is read-only
#   - deleting the emptyDir while published leaves the node's store whole, and
#     the teardown leaves no mount
{
  pkgs,
  lib ? pkgs.lib,
}:

let
  primary = pkgs.hello;
  venv = pkgs.pythonSet.mkVirtualEnv "csi-farm-disk-env" { nixkube = [ ]; };
  owner = "30000";

  publish = pkgs.writeText "csi_farm_disk_publish.py" ''
    import sys
    from pathlib import Path

    import anyio

    from src.volume import bind_store_disk, bind_writable, mount_farm, own_store, prepare_volume


    async def main() -> None:
        volume_root, target, disk, primary = (Path(a) for a in sys.argv[1:5])
        bind_store_disk(volume_root, disk)
        prepared = await prepare_volume(volume_root, {primary}, primary, bind_farm=True, allow_hardlinks=False)
        assert prepared.bind_farm, "prepare_volume fell back to a hardlink tree"
        own_store(volume_root, ${owner}, ${owner})
        bound = mount_farm(volume_root, prepared.paths, writable=True)
        print(f"[farm] bound {bound} of {len(prepared.paths)} paths")
        bind_writable(volume_root / "nix", target)


    anyio.run(main)
  '';

  # The bind `bind_store_disk` makes, without its private step.
  control = pkgs.writeText "csi_farm_disk_control.py" ''
    import ctypes
    import os
    import sys
    from pathlib import Path

    from src.constants import MS_BIND
    from src.nri.farm import build_farm

    volume_root, disk, primary = (Path(a) for a in sys.argv[1:4])
    libc = ctypes.CDLL(None, use_errno=True)
    disk.mkdir(parents=True)
    (volume_root / "nix").mkdir(parents=True)
    if libc.mount(os.fsencode(disk), os.fsencode(volume_root / "nix"), None, MS_BIND, None) != 0:
        raise OSError(ctypes.get_errno(), "bind")
    print(f"[control] bound {build_farm(volume_root / 'nix/store', [primary])} path without the private step")
  '';

  teardown = pkgs.writeText "csi_farm_disk_teardown.py" ''
    import shutil
    import sys
    from pathlib import Path

    import anyio

    from src.volume import detach_farm, is_farm, unmount

    volume_root, target = (Path(a) for a in sys.argv[1:3])


    async def detached() -> None:
        await unmount(target, detach=True)


    anyio.run(detached)
    assert is_farm(volume_root), "the store disk is not a farm to is_farm: the sweeps would delete through it"
    detach_farm(volume_root)
    shutil.rmtree(volume_root)
  '';
in
{
  name = "nixkube-csi-farm-disk";

  nodes.machine = {
    virtualisation.memorySize = 1024;
    environment.systemPackages = [
      venv
      pkgs.sqlite
    ];
    virtualisation.additionalPaths = [ primary ];
  };

  testScript = ''
    machine.wait_for_unit("multi-user.target")

    run = (
        "NIX_CONFIG='experimental-features = nix-command'"
        " NIXKUBE_PACKAGE=${pkgs.nixkube} NIXKUBE_NIX=${pkgs.nix}"
        " ${venv}/bin/python"
    )
    closure = machine.succeed("nix-store --query --requisites ${primary}").split()
    closure_bytes = int(machine.succeed(f"du -scb {' '.join(closure)} | tail -1").split()[0])
    as_owner = "setpriv --reuid=${owner} --regid=${owner} --clear-groups"

    def mounts_under(path):
        return [
            line.split()[4]
            for line in machine.succeed("cat /proc/self/mountinfo").splitlines()
            if line.split()[4].startswith(path + "/")
        ]

    # kubelet's directory, shared as on a node, in a peer group of its own: a peer of / shows each mount twice.
    machine.succeed(
        "mkdir -p /var/kubelet && mount --bind /var/kubelet /var/kubelet"
        " && mount --make-private /var/kubelet && mount --make-shared /var/kubelet"
    )
    pod = "/var/kubelet/pods/0f3c/volumes"

    print(machine.succeed(f"{run} ${control} /var/vol/control {pod}/kubernetes.io~empty-dir/control ${primary}"))
    leaked = mounts_under(f"{pod}/kubernetes.io~empty-dir/control")
    assert leaked, "the control bind did not propagate: this test cannot see a leak"
    print(f"[control] without the private step the emptyDir's own path holds {leaked}")
    machine.succeed("umount --recursive --lazy /var/vol/control/nix")

    disk = f"{pod}/kubernetes.io~empty-dir/store"
    target = f"{pod}/kubernetes.io~csi/nix/mount"
    print(machine.succeed(f"{run} ${publish} /var/vol/root {target} {disk} ${primary}"))

    assert mounts_under(disk) == [], f"input binds at the emptyDir's own path: {mounts_under(disk)}"
    store_mounts = [m for m in mounts_under(f"{target}/store") if m.count("/") == 10]
    assert len(store_mounts) == len(closure), f"{len(store_mounts)} binds for {len(closure)} paths"
    machine.succeed(f"test -x {target}/store/${baseNameOf primary}/bin/hello")
    machine.succeed(f"findmnt --noheadings --output OPTIONS {target}/store/${baseNameOf primary} | grep -qw ro")
    print(f"[publish] the target holds {len(closure)} read-only binds, none at the emptyDir's path")

    machine.succeed(f"{as_owner} sqlite3 {target}/var/nix/db/db.sqlite 'pragma user_version = 1'")
    machine.succeed(f"{as_owner} dd if=/dev/zero of={target}/store/output bs=1M count=1")
    machine.fail(f"{as_owner} touch {target}/store/${baseNameOf primary}/WRITTEN")
    machine.fail(f"touch {target}/store/${baseNameOf primary}/WRITTEN")
    machine.succeed(f"test -s {disk}/store/output")
    print("[publish] the pod's user writes the store and the database, and no input")

    # What kubelet's walk counts: it crosses no other st_dev, and a bind keeps its source's.
    # The store holds the output and empty stubs. Measured: the database holds Nix's 8 MiB `reserved` file.
    store_used = int(machine.succeed(f"du -sb {disk}/store").split()[0])
    assert store_used == 1 << 20, f"the emptyDir's store counts {store_used} bytes for a 1 MiB output"
    state_used = int(machine.succeed(f"du -sb {disk}/var").split()[0])
    print(f"[publish] the emptyDir counts {store_used} bytes of store and {state_used} of state; the closure is {closure_bytes}")

    machine.execute(f"rm -rf {disk}")
    machine.succeed("nix-store --verify-path ${primary}")
    machine.succeed("${primary}/bin/hello")
    print("[publish] deleting the emptyDir under a live farm leaves the node's store whole")

    print(machine.succeed(f"{run} ${teardown} /var/vol/root {target}"))
    left = mounts_under("/var/vol/root") + mounts_under(target) + mounts_under(disk)
    assert left == [], f"mounts left after teardown: {left}"
    machine.succeed("test ! -e /var/vol/root")
    machine.succeed("nix-store --verify-path ${primary} && ${primary}/bin/hello")
    print("[teardown] no mount left, and the node's store is whole")
  '';
}
