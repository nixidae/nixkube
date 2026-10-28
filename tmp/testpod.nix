# SPDX-License-Identifier: Apache-2.0

let
  system = builtins.currentSystem;
  inherit ((import ../. { })) sources;

  pkgs = import sources.nixpkgs {
    inherit system;
    config = {
      allowUnfree = true;
    };
  };

  # You can use flakes, npins, niv, fetchTree, fetchFromGitHub or whatever.
  ekn = import sources.easykubenix {
    inherit pkgs;
    modules = [
      (
        { config, lib, ... }:
        let
          PATH = lib.makeBinPath [
            pkgs.coreutils
            pkgs.fishMinimal
            pkgs.bash
          ];
        in
        {
          ekn.environment = "nixtest";
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
          kubernetes.resources.nixkube.Pod.csitest = {
            spec = {
              containers = lib.mkNamedList {
                ${toString builtins.currentTime} = {
                  image = "ghcr.io/nixidae/nixkube/scratch:1.0.1";
                  command = [
                    (lib.getExe pkgs.tini)
                    "--"
                    (lib.getExe' pkgs.coreutils "sleep")
                    "infinity"
                  ];
                  env = lib.mkNamedList {
                    PATH.value = PATH;
                  };
                  volumeMounts = lib.mkNamedList {
                    nix-store = {
                      mountPath = "/nix";
                      subPath = "nix";
                    };
                  };
                };
              };
              volumes = lib.mkNamedList {
                nix-store.csi = {
                  driver = "nixkube";
                  readOnly = true;
                };
              };
            };
          };

          kubernetes.resources.nixkube.Pod.nritest = {
            metadata.annotations = {
              "nixkube/pod-rw" = "true";
              "nixkube/pod-ssl" = "/etc/ssl/certs=${pkgs.dockerTools.caCertificates}/etc/ssl/certs";
              "nixkube/pod-group" = "/etc/group=${pkgs.dockerTools.fakeNss}/etc/group";
              "nixkube/pod-passwd" = "/etc/passwd=${pkgs.dockerTools.fakeNss}/etc/passwd";
              "nixkube/pod-nsswitch" = "/etc/nsswitch.conf=${pkgs.dockerTools.fakeNss}/etc/nsswitch.conf";
              "nixkube/pod-binsh" = "/bin/sh=${pkgs.dockerTools.binSh}/bin/sh";
              "nixkube/pod-usrbinenv" = "/usr/bin/env=${pkgs.dockerTools.usrBinEnv}/usr/bin/env";
            };
            spec = {
              containers = lib.mkNamedList {
                ${toString builtins.currentTime} = {
                  # image = "gcr.io/distroless/static:latest";
                  image = "ghcr.io/nixidae/nixkube/scratch:1.0.1";

                  env = lib.mkNamedList {
                    AFILE.value = pkgs.writeText "afile" "this is a file";
                    PATH.value = PATH;
                  };
                  command = [
                    (lib.getExe pkgs.tini)
                    "--"
                    (lib.getExe' pkgs.coreutils "sleep")
                    "infinity"
                  ];
                };
              };
            };
          };
        }
      )
    ];
  };
in
ekn
