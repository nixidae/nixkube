# SPDX-License-Identifier: Apache-2.0

{
  config,
  lib,
  ...
}:
let
  cfg = config.nixkube;
in
{
  # The CSIDriver objects are the node DaemonSet's registration. A driver
  # object with no node plugin behind it is a registration nothing serves.
  config = lib.mkIf (cfg.enable && cfg.node.enable) {
    kubernetes.resources.none.CSIDriver =
      let
        spec = {
          attachRequired = false;
          podInfoOnMount = true;
          volumeLifecycleModes = [ "Ephemeral" ];
          # The kubelet never applies a pod's fsGroup to the volume. With `File` it walked the whole
          # read-only closure of every pod that sets one, and logged a failed chown and chmod per file:
          # 34,482 lines and about 3.4 s for a build pod (run nkrunsplit1). The node sets the owner of a
          # writable store itself (`csi/storedisk.py`).
          fsGroupPolicy = "None";
          requiresRepublish = false;
          storageCapacity = false;
        };
      in
      {
        "nixkube" = {
          metadata.labels = cfg.labels;
          inherit spec;
        };
        "nix.csi.store" = lib.mkIf cfg.node.compat {
          metadata.labels = cfg.labels;
          inherit spec;
        };
      };
  };
}
