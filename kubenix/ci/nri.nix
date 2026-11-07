# SPDX-License-Identifier: Apache-2.0

# NRI test workloads — validate /nix mounting via the new mount API.
#   RO (nri-hello-ro): open_tree(2) + move_mount(2), Linux 5.2+
#   RW (nri-hello-rw): fsopen("overlay") + fsconfig + fsmount, Linux 6.5+
#
# No CSI volume is involved. The nri-nri plugin detects the hello store path
# in the container command, fetches the closure, and mounts /nix via
# setns + the appropriate new-API mechanism before the container starts.
# A successful job completion proves the mount and binary execution worked.

{
  config,
  curPkgs,
  lib,
  ...
}:
let
  cfg = config.nixkube;
in
{
  config.kubernetes.resources.${cfg.namespace} = {
    # RO: /nix mounted via open_tree(2) clone + move_mount(2) + remount RO.
    Job.nri-hello-ro = {
      metadata.labels = cfg.labels // {
        "app.kubernetes.io/component" = "ci-test-nri";
      };
      spec.template.spec = {
        restartPolicy = "Never";
        containers = lib.mkNamedList {
          hello = {
            image = "ghcr.io/nixidae/nixkube/scratch:1.0.2";
            command = [ "${curPkgs.hello}/bin/hello" ];
          };
        };
      };
    };

    # RW: /nix mounted via fsopen("overlay") + fsconfig + fsmount.
    # Requires Linux 6.5+ — CI uses ubuntu-24.04 runner (kernel 6.8).
    Job.nri-hello-rw = {
      metadata.labels = cfg.labels // {
        "app.kubernetes.io/component" = "ci-test-nri";
      };
      spec.template = {
        metadata.annotations."nixkube/pod-rw" = "true";
        spec = {
          restartPolicy = "Never";
          containers = lib.mkNamedList {
            hello = {
              image = "ghcr.io/nixidae/nixkube/scratch:1.0.2";
              command = [ "${curPkgs.hello}/bin/hello" ];
            };
          };
        };
      };
    };
  };
}
