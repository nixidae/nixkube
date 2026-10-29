# SPDX-License-Identifier: Apache-2.0

# A CSI volume as one composefs mount, through nixkube's own functions, as
# root. Issues #68 and #25.
#
#   - the node writes the same derivation CI evaluates, so it finds CI's
#     image: built here, the image's path is the one `composefsImage` names
#   - the kernel probe accepts this kernel
#   - read-only and read-write volumes at a target: one mount, store files,
#     the primary package's tree as real files, what kubelet's subPath (a
#     non-recursive bind) carries, and a database Nix can read
#   - unpublish leaves no mount
{
  pkgs,
  lib ? pkgs.lib,
  composefsImage,
}:

let
  primary = pkgs.hello;
  expected = composefsImage {
    roots = [ primary ];
    inherit primary;
  };
  venv = pkgs.pythonSet.mkVirtualEnv "csi-composefs-env" { nixkube = [ ]; };
  # A volume that holds Nix, as the NixOSRollout controller's does.
  withNix = composefsImage {
    roots = [ pkgs.nix ];
    primary = pkgs.nix;
    opaque = false;
  };

  probe = pkgs.writeText "csi_composefs_probe.py" ''
    import json
    import os
    import sys
    from pathlib import Path

    import anyio

    from src.csi import composefs
    from src.volume import unmount

    primary = Path(sys.argv[1])


    def mounts() -> list[str]:
        return [
            line.split()[4]
            for line in Path("/proc/self/mountinfo").read_text().splitlines()
        ]


    def writable(path: Path) -> bool:
        try:
            path.write_text("a write")
            return True
        except OSError:
            return False


    async def main() -> None:
        results: dict = {"available": await composefs.available()}
        image = await composefs.build_image(
            {primary}, primary, Path("/var/csi/gcroots/vol/composefs"), []
        )
        results["image"] = str(image)
        for readonly in (True, False):
            name = "ro" if readonly else "rw"
            root = Path(f"/var/csi/volumes/{name}")
            target = Path(f"/var/kubelet/pods/{name}/volumes/nix")
            before = len(mounts())
            await composefs.mount(image, target, readonly, root)
            store_hello = target / "nix/store" / primary.name / "bin/hello"
            root_hello = target / "bin/hello"
            c = {
                "added": len(mounts()) - before,
                "store_file_same": store_hello.read_bytes()
                == (primary / "bin/hello").read_bytes(),
                "root_is_a_file": root_hello.is_file() and not root_hello.is_symlink(),
                "result": os.readlink(target / "nix/var/result"),
                "store_path_writable": writable(target / "nix/store" / primary.name / "W"),
                # Read-write copies up into the volume's upper directory, as
                # the overlay over a hardlink tree always did; the store itself
                # must not change.
                "store_changed": (primary / "W").exists(),
            }
            if not readonly:
                gap = target / "nix/store/0000000000000000000000000000000-built"
                gap.mkdir()
                c["gap_writable"] = writable(gap / "out")
                c["upper_holds_it"] = (root / "upper/nix/store" / gap.name / "out").exists()
                c["reached_the_store"] = Path("/nix/store", gap.name).exists()
            await unmount(target)
            c["left"] = len(mounts()) - before
            results[name] = c
        print("PROBE " + json.dumps(results))


    anyio.run(main)
  '';
in
{
  name = "nixkube-csi-composefs";

  nodes.machine = {
    virtualisation.memorySize = 2048;
    # What the image is built from, but not the image: the guest builds it.
    virtualisation.additionalPaths = [
      primary
      pkgs.nixkube
      pkgs.composefs
      pkgs.nix
      withNix
    ];
    environment.systemPackages = [
      venv
      pkgs.composefs
    ];
    nix.settings.substituters = lib.mkForce [ ];
  };

  testScript = ''
    import json

    machine.wait_for_unit("multi-user.target")
    machine.fail("test -e ${builtins.unsafeDiscardStringContext (toString expected)}")
    print("[setup] the image is not in the guest's store before the node builds it")

    # The client's setting, as nixkube's own nix.conf sets it in the pod, and
    # the tools as the nixkube wrapper names them.
    client = "NIX_CONFIG='experimental-features = nix-command read-only-local-store'"
    wrapper = (
        "NIXKUBE_PACKAGE=${pkgs.nixkube} NIXKUBE_COMPOSEFS=${pkgs.composefs}"
        " NIXKUBE_NIX=${pkgs.nix}"
    )
    out = machine.succeed(f"{client} {wrapper} ${venv}/bin/python ${probe} ${primary}")
    results = json.loads([l for l in out.splitlines() if l.startswith("PROBE ")][0][6:])
    print(f"[results] {results}")

    assert results["available"] is True, "the kernel probe refused this kernel"
    assert results["image"] == "${builtins.unsafeDiscardStringContext (toString expected)}", (
        f"the node built {results['image']}, CI evaluates"
        " ${builtins.unsafeDiscardStringContext (toString expected)}: a node would never"
        " find CI's image"
    )
    print("[image] the node's derivation is CI's: same output path")

    for mode in ("ro", "rw"):
        c = results[mode]
        assert c["added"] == 1, f"[{mode}] one volume added {c['added']} mounts"
        assert c["store_file_same"], f"[{mode}] a store file reads differently"
        assert c["root_is_a_file"], f"[{mode}] /bin/hello is not a regular file"
        assert c["result"] == "${primary}", f"[{mode}] result link {c['result']}"
        assert c["store_changed"] is False, f"[{mode}] a write reached {primary}"
        assert c["left"] == 0, f"[{mode}] unpublish left {c['left']} mounts"
    assert results["ro"]["store_path_writable"] is False, "a read-only volume took a write"
    assert results["rw"]["gap_writable"] and results["rw"]["upper_holds_it"], (
        "a read-write volume cannot be written, or not into its upper directory"
    )
    assert not results["rw"]["reached_the_store"], "a volume's write reached /nix/store"
    print("[mount] one mount each, store files, real root files, writes kept out of the store")

    # What kubelet's subPath does, against a fresh read-only mount, and the
    # database through the volume, as a pod's Nix would read it.
    machine.succeed("mkdir -p /var/t /var/sub")
    machine.succeed(
        "${venv}/bin/python -c 'import sys; from pathlib import Path;"
        " from src.csi.composefs import compose;"
        " compose(Path(sys.argv[1]), Path(\"/var/t\"), \"/nix/store\", Path(\"/var/tscratch\"), None)'"
        " ${builtins.unsafeDiscardStringContext (toString expected)}/image.cfs"
    )
    machine.succeed("mount --bind /var/t/nix /var/sub")
    machine.succeed("/var/sub/store/$(basename ${primary})/bin/hello | grep -q 'Hello, world'")
    machine.succeed("umount /var/sub")
    print("[subPath] a non-recursive bind of <volume>/nix carries the store")
    # Read as SQLite, not through `nix --store local?root=...&read-only=true`:
    # that still creates /nix/store/.links, which any read-only volume
    # refuses, a hardlink tree's as much as this one.
    paths = machine.succeed(
        "${venv}/bin/python -c 'import sqlite3;"
        " db = sqlite3.connect(\"file:/var/t/nix/var/nix/db/db.sqlite?immutable=1\", uri=True);"
        " print(\"\\n\".join(p for (p,) in db.execute(\"select path from ValidPaths\")))'"
    )
    assert "${primary}" in paths.split(), f"the volume's database does not know hello: {paths}"
    print(f"[db] the volume's database lists {len(paths.split())} paths, hello among them")
    machine.succeed("umount /var/t")

    # A read-write volume as a local store, as the NixOSRollout controller
    # uses its own: `--eval-store auto` writes .drv files into the volume, so
    # Nix opens the database for writing and creates /nix/store/.links.
    machine.succeed(
        "${venv}/bin/python -c 'import sys, anyio; from pathlib import Path;"
        " from src.csi import composefs;"
        " anyio.run(composefs.mount, Path(sys.argv[1]), Path(\"/var/w\"), False, Path(\"/var/wroot\"))'"
        " ${builtins.unsafeDiscardStringContext (toString expected)}"
    )
    machine.succeed("echo a-drv-stand-in > /tmp/added")
    rc, out = machine.execute(
        "${pkgs.nix}/bin/nix-store --store 'local?root=/var/w' --add /tmp/added 2>&1"
    )
    print(f"[rw-store] rc={rc}: {out.strip()}")
    assert rc == 0, f"a read-write volume is not a writable local store: {out.strip()}"
    print("[rw-store] Nix writes a local store on a read-write volume")

    # The controller's own view: kubelet binds <volume>/nix over /nix, and
    # Nix runs from there with no daemon, so `auto` picks the local store.
    machine.succeed(
        "${venv}/bin/python -c 'import sys, anyio; from pathlib import Path;"
        " from src.csi import composefs;"
        " anyio.run(composefs.mount, Path(sys.argv[1]), Path(\"/var/n\"), False, Path(\"/var/nroot\"))'"
        " ${builtins.unsafeDiscardStringContext (toString withNix)}"
    )
    rc, out = machine.execute(
        "unshare --mount sh -c 'mount --bind /var/n/nix /nix &&"
        " NIX_REMOTE= ${pkgs.nix}/bin/nix-store --add /tmp/added' 2>&1"
    )
    print(f"[rw-auto] rc={rc}: {out.strip()}")
    assert rc == 0, f"Nix on a read-write volume bound at /nix cannot write its store: {out.strip()}"
    print("[rw-auto] Nix writes its own store at /nix on a read-write volume")
  '';
}
