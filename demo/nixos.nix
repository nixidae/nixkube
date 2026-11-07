# SPDX-License-Identifier: Apache-2.0
let
  default = import ../. { };
  inherit (default) pkgs easykubenix;
  inherit (pkgs) lib;

  nixos = import (pkgs.path + "/nixos/lib/eval-config.nix") {
    inherit pkgs;
    modules = [
      (
        {
          config,
          pkgs,
          lib,
          ...
        }:
        {
          boot.isContainer = true;
          boot.specialFileSystems = lib.mkForce { };
          boot.nixStoreMountOpts = lib.mkForce [ ];
          services.journald.console = "/dev/stderr";
          networking.resolvconf.enable = false;
          environment.etc.hostname.enable = lib.mkForce false;
          environment.etc.hosts.enable = lib.mkForce false;
          system.stateVersion = "25.05";
        }
      )
    ];
  };

  ekn = easykubenix {
    inherit pkgs;
    modules = [
      (
        {
          config,
          pkgs,
          lib,
          ...
        }:
        {
          # The prune scope: an apply deletes objects carrying this label that
          # it did not produce. Read by kluctl and by `ekn kubeapply` alike.
          ekn.environment = "demodeploy";
          kluctl = {
            preDeployScript = # bash
              ''
                nix copy \
                  --substitute-on-destination \
                  --no-check-sigs \
                  --to ssh-ng://nix@nixcache.lillecarl.com?port=2222 \
                  ${config.kluctl.projectDir} \
                  -v || true
              '';
          };
          kubernetes.resources.none.Pod.nixos.spec = {
            automountServiceAccountToken = false;
            containers = lib.mkNamedList {
              nixos = {
                image = "ghcr.io/nixidae/nixkube/scratch:1.0.2"; # sets PATH to /nix/var/result/bin
                command = [
                  "/nix/var/result/init"
                  "--system"
                  "--log-level=debug"
                  "--log-target=console"
                ];
                volumeMounts = lib.mkNamedList {
                  nix = {
                    mountPath = "/nix";
                    readOnly = true;
                    subPath = "nix";
                  };
                  run.mountPath = "/run";
                  tmp.mountPath = "/tmp";
                  cgroup.mountPath = "/sys/fs/cgroup";
                };
                env = lib.mkNamedList {
                  container.value = "1";
                };
              };
            };
            volumes = lib.mkNamedList {
              run.emptyDir.medium = "Memory";
              tmp.emptyDir.medium = "Memory";
              cgroup.hostPath.path = "/sys/fs/cgroup";
              nix.csi = {
                driver = "nixkube";
                volumeAttributes.${pkgs.stdenv.hostPlatform.system} = pkgs.buildEnv {
                  name = "initenv";
                  paths = [
                    pkgs.fish
                    pkgs.bash
                    pkgs.coreutils
                    nixos.config.system.build.toplevel
                  ];
                };
                readOnly = true;
              };
            };
          };
        }
      )
    ];
  };
in
ekn // { inherit nixos; }
