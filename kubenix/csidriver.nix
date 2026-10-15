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
    kubernetes.resources.none.CSIDriver = {
      "nixkube" = {
        metadata.labels = cfg.labels;
        spec = {
          attachRequired = false;
          podInfoOnMount = true;
          volumeLifecycleModes = [ "Ephemeral" ];
          fsGroupPolicy = "File";
          requiresRepublish = false;
          storageCapacity = false;
        };
      };
      "nix.csi.store" = lib.mkIf cfg.node.compat {
        metadata.labels = cfg.labels;
        spec = {
          attachRequired = false;
          podInfoOnMount = true;
          volumeLifecycleModes = [ "Ephemeral" ];
          fsGroupPolicy = "File";
          requiresRepublish = false;
          storageCapacity = false;
        };
      };
    };
  };
}
