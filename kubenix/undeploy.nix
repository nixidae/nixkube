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
  config = lib.mkIf cfg.undeploy {
    assertions = [
      {
        assertion = !(cfg.enable && cfg.undeploy);
        message = "nixkube.undeploy cannot be true when nixkube.enable is also true.";
      }
      {
        assertion = !(cfg.undeploy && lib.hasPrefix "/nix" cfg.hostMountPath);
        message = "nixkube.undeploy will not undeploy /nix based mount locations like ${cfg.hostMountPath}";
      }
    ];

    kubernetes.resources.${cfg.namespace}.DaemonSet.nixkube-cleanup =
      let
        labels = cfg.labels // {
          "app.kubernetes.io/component" = "cleanup";
        };
        matchLabels = cfg.matchLabels // {
          "app.kubernetes.io/component" = "cleanup";
        };
      in
      {
        metadata.labels = labels;
        spec.selector.matchLabels = matchLabels;
        spec.template = {
          metadata.labels = labels;
          spec = {
            containers = lib.mkNamedList {
              cleanup = {
                name = "cleanup";
                image = "busybox:latest";
                command = [
                  "find"
                  "/nix"
                  "-mindepth"
                  "1"
                  "-delete"
                ];

                volumeMounts = lib.mkNamedList {
                  nix-store.mountPath = "/nix";
                };
                securityContext.privileged = true;
              };
              # The build pods' seccomp profile, which nix-node writes. `build_pods.py`.
              seccomp-cleanup = {
                name = "seccomp-cleanup";
                image = "busybox:latest";
                command = [
                  "rm"
                  "-rf"
                  "/kubelet/seccomp/nixkube"
                ];
                volumeMounts = lib.mkNamedList {
                  kubelet.mountPath = "/kubelet";
                };
                securityContext.privileged = true;
              };
            };

            volumes = lib.mkNamedList {
              nix-store.hostPath.path = cfg.hostMountPath;
              kubelet.hostPath = {
                path = "/var/lib/kubelet";
                type = "Directory";
              };
            };
          };
        };
      };
  };
}
