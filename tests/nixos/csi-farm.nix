# SPDX-License-Identifier: Apache-2.0

# A CSI volume that asks for a farm (`presentation: farm`), through nixkube's
# own functions, as root. nixidae/nixidae#1560: pynixd's build pods take their
# inputs this way, as Nix's sandbox binds each input read-only.
#
#   - `prepare_volume` keeps the farm, and `mount_farm` binds each closure
#     path on a tmpfs of its own
#   - the published target is the volume's `nix` directory: the store and the
#     database, each path a mount with its files
#   - the target, its store and each bind refuse writes; the host can write
#   - unpublish, then `detach_farm`, leaves no mount under the volume root,
#     and deleting the volume root leaves the node's store whole
{
  pkgs,
  lib ? pkgs.lib,
}:

let
  primary = pkgs.hello;
  venv = pkgs.pythonSet.mkVirtualEnv "csi-farm-env" { nixkube = [ ]; };

  publish = pkgs.writeText "csi_farm_publish.py" ''
    import sys
    from pathlib import Path

    import anyio

    from src.volume import mount_farm, mount_volume, prepare_volume


    async def main() -> None:
        volume_root, target, primary = (Path(a) for a in sys.argv[1:4])
        prepared = await prepare_volume(volume_root, {primary}, primary, bind_farm=True)
        assert prepared.bind_farm, "prepare_volume fell back to a hardlink tree"
        bound = mount_farm(volume_root, prepared.paths)
        print(f"[farm] bound {bound} of {len(prepared.paths)} paths")
        await mount_volume(volume_root / "nix", target, True)


    anyio.run(main)
  '';

  teardown = pkgs.writeText "csi_farm_teardown.py" ''
    import shutil
    import sys
    from pathlib import Path

    import anyio

    from src.errors import UnmountError
    from src.volume import detach_farm, unmount

    volume_root, target = (Path(a) for a in sys.argv[1:3])
    # The control: a plain umount2 of a target that carries the binds.
    try:
        anyio.run(unmount, target)
    except UnmountError as error:
        print(f"[control] a plain unmount of the farm's target fails: {error}")
    else:
        raise SystemExit("a plain unmount took the farm's target: the detach proves nothing")


    async def detached() -> None:
        await unmount(target, detach=True)


    anyio.run(detached)
    detach_farm(volume_root)
    shutil.rmtree(volume_root)
  '';
in
{
  name = "nixkube-csi-farm";

  nodes.machine = {
    virtualisation.memorySize = 1024;
    environment.systemPackages = [ venv ];
    virtualisation.additionalPaths = [ primary ];
  };

  testScript = ''
    machine.wait_for_unit("multi-user.target")

    # What nixkube's node environment sets: its Nix, and the commands it runs.
    run = (
        "NIX_CONFIG='experimental-features = nix-command'"
        " NIXKUBE_PACKAGE=${pkgs.nixkube} NIXKUBE_NIX=${pkgs.nix}"
        " ${venv}/bin/python"
    )
    closure = machine.succeed("nix-store --query --requisites ${primary}").split()
    print(machine.succeed(f"{run} ${publish} /var/vol/root /var/target ${primary}"))

    def mounts_under(path):
        return [
            line.split()[4]
            for line in machine.succeed("cat /proc/self/mountinfo").splitlines()
            if line.split()[4].startswith(path + "/")
        ]

    store_mounts = [m for m in mounts_under("/var/target/store") if m.count("/") == 4]
    assert len(store_mounts) == len(closure), f"{len(store_mounts)} binds for {len(closure)} paths"
    machine.succeed("test -x /var/target/store/${baseNameOf primary}/bin/hello")
    machine.succeed("test -e /var/target/var/nix/db/db.sqlite")
    print(f"[publish] the target holds {len(closure)} binds and the database")

    # Read-only on the farm's own mounts, not only on the target: kubelet's namespace gets copies of these at the
    # publish, and a mount_setattr after it never reaches the copies.
    machine.succeed("findmnt --noheadings --output OPTIONS /var/vol/root/nix/store | grep -qw ro")
    machine.succeed("findmnt --noheadings --output OPTIONS /var/vol/root/nix/store/${baseNameOf primary} | grep -qw ro")
    machine.fail("touch /var/target/WRITTEN")
    machine.fail("touch /var/target/store/WRITTEN")
    machine.fail("touch /var/target/store/${baseNameOf primary}/WRITTEN")
    print("[publish] the target, its store and a bind refuse writes")

    # Whether the refusals prove the bind flags: they do when root can write the store itself.
    source_writable = machine.execute("touch /nix/store/.csi-farm && rm /nix/store/.csi-farm")[0] == 0
    print(f"[publish] the node's own /nix/store is writable to root: {source_writable}")

    print(machine.succeed(f"{run} ${teardown} /var/vol/root /var/target"))
    left = mounts_under("/var/vol/root") + mounts_under("/var/target")
    assert left == [], f"mounts left after teardown: {left}"
    machine.succeed("test ! -e /var/vol/root")
    machine.succeed("nix-store --verify-path ${primary}")
    machine.succeed("${primary}/bin/hello")
    print("[teardown] no mount left, and the node's store is whole")
  '';
}
