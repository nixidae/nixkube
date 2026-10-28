# SPDX-License-Identifier: Apache-2.0

let
  system = builtins.currentSystem;
  inherit ((import ../. { })) sources;

  sysMap = {
    "x86_64-linux" = "aarch64-linux";
    "aarch64-linux" = "x86_64-linux";
  };
  pkgs = import sources.nixpkgs {
    inherit system;
    config = {
      allowUnfree = true;
    };
  };
  pkgsCross = import pkgs.path {
    system = sysMap.${system};
    config = {
      allowUnfree = true;
    };
  };

  package =
    pkgs:
    pkgs.buildEnv {
      name = "demoEnv";
      paths = [
        pkgs.bash
        pkgs.fishMinimal
        pkgs.coreutils
        pkgs.moreutils
        pkgs.nix
      ];
    };

  # You can use flakes, npins, niv, fetchTree, fetchFromGitHub or whatever.
  ekn = (import sources.easykubenix { }).lib.easykubenix {
    inherit pkgs;
    modules = [
      (
        { config, lib, ... }:
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
          # Will go into the default namespace
          kubernetes.resources.none.Pod.hello.spec = {
            containers = lib.mkNamedList {
              hello = {
                image = "ghcr.io/nixidae/nixkube/scratch:1.0.1"; # 1.0.1 sets PATH to /nix/var/result/bin
                command = [
                  "bash"
                  "-c"
                  "hello;sleep infinity"
                ];
                volumeMounts = lib.mkNamedList {
                  nix = {
                    mountPath = "/nix";
                    subPath = "nix";
                  };
                };
              };
            };
            # lib.mkNamedList adds metadata that tells easykubenix to convert the atrributeset
            # into a list of attrset with name attribute set
            volumes = lib.mkNamedList {
              nix.csi = {
                driver = "nixkube";
                # these are stringified into storePaths now the manifest depends
                # on pkgs.hello so when we push it we bring the package environment and nix-csi
                # can fetch it.
                volumeAttributes.${pkgs.stdenv.hostPlatform.system} = package pkgs;
                volumeAttributes.${pkgsCross.stdenv.hostPlatform.system} = package pkgsCross;
              };
            };
          };
        }
      )
    ];
  };
in
ekn
