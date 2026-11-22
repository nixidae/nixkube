# SPDX-License-Identifier: Apache-2.0

# What a pynixd build pod over a CSI farm runs, and so what its farm volume holds
# (phases/buildpods.py, ./farm_build_pod.py). nixidae/nixidae#1560, spike.
#
# Its closure is the volume: pynixd, the Nix it reads its settings with, and busybox,
# the input of the pod's build.
{
  pkgs,
  pynixd,
}:
let
  config = pkgs.writeText "farm-build-pod-pynixd.json" (
    builtins.toJSON {
      stores.local = {
        type = "local-socket";
        store_path = "/build";
        managed = true;
        # A Nix daemon would not know the farm: pynixd answers every operation.
        daemon = false;
        pynixd_writes = true;
        lower_store = "/";
        op_routes.BuildDerivation = "pynixd";
        sibling_sandbox = "/build/sibling";
      };
      unix_path = "/build/pynixd.sock";
      ssh_port = null;
      gc_enabled = false;
      log_level = "INFO";
      ranking.min_schedule_score = -1.0e9;
      ranking.min_free_bytes = 0;
    }
  );
in
pkgs.runCommand "farm-build-pod" { } ''
  mkdir $out
  cp ${./farm_build_pod.py} $out/run.py
  ln -s ${config} $out/pynixd.json
  ln -s ${pynixd} $out/pynixd
  ln -s ${pynixd.venv} $out/venv
  ln -s ${pkgs.nix} $out/nix
  ln -s ${pkgs.busybox} $out/busybox
''
