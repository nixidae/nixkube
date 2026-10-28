# SPDX-License-Identifier: Apache-2.0

{
  config,
  lib,
  ...
}:
let
  cfg = config.nixkube;
  inherit (cfg) namespace;
in
{
  config = lib.mkIf cfg.enable {
    kubernetes.resources.none.Namespace.${namespace} = {
      metadata.labels = cfg.labels;
    };

    /*
      Half the prune scope for a nixkube deploy.

      `ekn.environment` has no default: an apply deletes objects carrying
      this label that the apply did not produce, so two projects sharing a
      value on one cluster delete each other's work. `mkDefault`, because a
      deployment that installs nixkube beside its own objects wants one
      scope for all of them and says so itself.

      The other half is `ekn.dev/deployment-unit`, which easykubenix renders
      onto every object in a deployment unit. nixkube declares no units, so
      its objects carry no unit label and a whole-instance prune owns them.
    */
    ekn.environment = lib.mkDefault namespace;
  };
}
