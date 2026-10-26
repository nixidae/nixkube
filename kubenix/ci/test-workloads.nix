# SPDX-License-Identifier: Apache-2.0

{
  pkgs,
  lib,
  sources,
  ...
}:
let
  system = pkgs.stdenv.hostPlatform.system;
  pin = import ./nixpkgs-pin.nix;
  namespace = "nixkube";
  labels = {
    "app.kubernetes.io/managed-by" = "nix";
    "app.kubernetes.io/name" = "nixkube";
    "app.kubernetes.io/part-of" = "nixkube";
  };

  subPath = spath: lib.removePrefix "/" (toString spath);

  containers = lib.mkNamedList {
    hello = {
      image = "ghcr.io/nixidae/nixkube/scratch:1.0.1";
      command = [ "hello" ];
      volumeMounts = lib.mkNamedList {
        nixkube = {
          mountPath = "/nix";
          subPath = "nix";
        };
      };
    };
  };
in
{
  kubernetes.resources.${namespace} = {
    ConfigMap.sources = {
      metadata.labels = labels;
      data = sources;
    };

    Job.flake-hello = {
      metadata.labels = labels // {
        "app.kubernetes.io/component" = "ci-test";
      };
      spec.template.spec = {
        restartPolicy = "Never";
        inherit containers;
        volumes = lib.mkNamedList {
          nixkube.csi = {
            driver = "nixkube";
            volumeAttributes.flakeRef = "${pin.flakeRef}#hello";
          };
        };
      };
    };

    Job.expr-hello = {
      metadata.labels = labels // {
        "app.kubernetes.io/component" = "ci-test";
      };
      spec.template.spec = {
        restartPolicy = "Never";
        containers = lib.mkNamedList {
          hello = {
            image = "ghcr.io/nixidae/nixkube/scratch:1.0.1";
            command = [ "hello-unfree" ];
            volumeMounts = lib.mkNamedList {
              nixkube = {
                mountPath = "/nix";
                subPath = "nix";
              };
            };
          };
        };
        volumes = lib.mkNamedList {
          nixkube.csi = {
            driver = "nixkube";
            readOnly = true;
            # The revision of ./nixpkgs-pin.nix. The guest test's cache guest
            # holds what building this needs, and not the result, so the node
            # still builds it.
            volumeAttributes.nixExpr = # nix
              ''
                let
                  nixpkgs = builtins.fetchTree {
                    type = "github";
                    owner = "${pin.owner}";
                    repo = "${pin.repo}";
                    rev = "${pin.rev}";
                  };
                  pkgs = import nixpkgs { config = { allowUnfree = true; }; };
                in
                pkgs.hello-unfree # test that building works
              '';
          };
        };
      };
    };

    Job.path-hello = {
      metadata.labels = labels // {
        "app.kubernetes.io/component" = "ci-test";
      };
      spec.template.spec = {
        restartPolicy = "Never";
        inherit containers;
        volumes = lib.mkNamedList {
          nixkube.csi = {
            driver = "nixkube";
            readOnly = true;
            volumeAttributes.${system} = pkgs.hello;
          };
        };
      };
    };

    Job.commandpath-hello = {
      metadata.labels = labels // {
        "app.kubernetes.io/component" = "ci-test";
      };
      spec.template.spec = {
        restartPolicy = "Never";
        containers = lib.recursiveUpdate containers {
          hello.command = [ (lib.getExe pkgs.hello) ];
        };
        volumes = lib.mkNamedList {
          nixkube.csi = {
            driver = "nixkube";
            readOnly = true;
          };
        };
      };
    };

    Job.env-ssl = {
      metadata.labels = labels // {
        "app.kubernetes.io/component" = "ci-test";
      };
      spec.template.spec = {
        restartPolicy = "Never";
        containers = lib.recursiveUpdate containers {
          hello = {
            command = [
              (lib.getExe (
                pkgs.writeShellApplication {
                  name = "printer";
                  runtimeInputs = [
                    pkgs.coreutils
                    pkgs.hello
                  ];
                  text = # bash
                    ''
                      set -x
                      hello
                      ls -lah "$SSL_CERT_DIR"
                    '';
                }
              ))
            ];
            env = lib.mkNamedList {
              SSL_CERT_FILE.value = "${pkgs.cacert}/etc/ssl/certs/ca-bundle.crt";
              SSL_CERT_DIR.value = "${pkgs.cacert}/etc/ssl/certs";
            };
            volumeMounts = lib.mkForce [
              {
                name = "nixkube";
                mountPath = "/nix";
                subPath = "nix";
              }
            ];
          };
        };
        volumes = lib.mkNamedList {
          nixkube.csi = {
            driver = "nixkube";
            readOnly = true;
          };
        };
      };
    };

    # NRI test workloads
    Job.nri-hello-ro = {
      metadata.labels = labels // {
        "app.kubernetes.io/component" = "ci-test-nri";
      };
      spec.template.spec = {
        restartPolicy = "Never";
        containers = lib.mkNamedList {
          hello = {
            image = "ghcr.io/nixidae/nixkube/scratch:1.0.1";
            command = [ "${pkgs.hello}/bin/hello" ];
          };
        };
      };
    };

    Job.nri-hello-rw = {
      metadata.labels = labels // {
        "app.kubernetes.io/component" = "ci-test-nri";
      };
      spec.template = {
        metadata.annotations."nixkube/pod-rw" = "true";
        spec = {
          restartPolicy = "Never";
          containers = lib.mkNamedList {
            hello = {
              image = "ghcr.io/nixidae/nixkube/scratch:1.0.1";
              command = [ "${pkgs.hello}/bin/hello" ];
            };
          };
        };
      };
    };

    # Test failure scenarios for event reporting
    Job.invalid-storepath-hello = {
      metadata.labels = labels // {
        "app.kubernetes.io/component" = "ci-test";
      };
      spec.template.spec = {
        restartPolicy = "Never";
        inherit containers;
        volumes = lib.mkNamedList {
          nixkube.csi = {
            driver = "nixkube";
            readOnly = true;
            volumeAttributes.${system} = "/nix/store/0000000000000000000000000000000-nonexistent";
          };
        };
      };
    };

    Job.invalid-flake-hello = {
      metadata.labels = labels // {
        "app.kubernetes.io/component" = "ci-test";
      };
      spec.template.spec = {
        restartPolicy = "Never";
        inherit containers;
        volumes = lib.mkNamedList {
          nixkube.csi = {
            driver = "nixkube";
            volumeAttributes.flakeRef = "github:nonexistent/nonexistent-repo/nonexistent-ref#nonexistent";
          };
        };
      };
    };

    Job.invalid-expr-hello = {
      metadata.labels = labels // {
        "app.kubernetes.io/component" = "ci-test";
      };
      spec.template.spec = {
        restartPolicy = "Never";
        inherit containers;
        volumes = lib.mkNamedList {
          nixkube.csi = {
            driver = "nixkube";
            volumeAttributes.nixExpr = # nix
              ''
                let
                  broken = this_identifier_does_not_exist;
                in
                broken
              '';
          };
        };
      };
    };
  };
}
