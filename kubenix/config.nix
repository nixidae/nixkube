# SPDX-License-Identifier: Apache-2.0

{
  config,
  lib,
  ekn,
  ...
}:
let
  cfg = config.nixkube;
  readConfigurationFile = import ./readConfigurationFile.nix;

in
{
  config = lib.mkIf cfg.enable {
    nixkube = {
      nixConfig.settings = {
        allowed-users = [ "*" ];
        # A node substitutes and cannot build, so this list is the whole of
        # what a node can get. A store path on no cache here is a mount that
        # never succeeds, and the failure surfaces on a node rather than
        # wherever forgot to push it. `assert-cached` asks this exact list.
        trusted-public-keys = [
          "nixkube.cachix.org-1:H8UE0jlI9pxHexK/NhDmEoLDarJXp1WTymQrsajlh7M="
        ];
        substituters = [
          "https://nixkube.cachix.org"
        ];
        experimental-features = [
          "nix-command"
          "flakes"
          "read-only-local-store"
          "ca-derivations"
          "dynamic-derivations"
          "recursive-nix"
        ];
        builders-use-substitutes = true;
        narinfo-cache-negative-ttl = 0;
        narinfo-cache-positive-ttl = 0;
        warn-dirty = false;
        system-features = [
          "nixos-test"
          "benchmark"
          "big-parallel"
        ];
      };
      # Only the node runs a daemon at the default socket. A pynixd pod has
      # none, and pynixd builds there with no chroot: a `store` in the pod's
      # nix.conf wins over the `NIX_REMOTE` of a recursive-nix build, which
      # then dials a socket that does not exist (`onlyTheNodeNamesAStore`).
      node.nixConfig.settings = lib.mkMerge [
        cfg.nixConfig.settings
        { store = lib.mkDefault "daemon"; }
      ];
      pynixd.builder.nixConfig.settings = cfg.nixConfig.settings;
      pynixd.controller.nixConfig.settings = cfg.nixConfig.settings;
    };
    kubernetes.resources.${cfg.namespace} = {
      ConfigMap.nix-node = {
        metadata.labels = cfg.labels;
        data = {
          "nix.conf" = readConfigurationFile ekn.lib.packageRef.enabled cfg.node.nixConfig.nixConf;
          "logging.json" = builtins.toJSON cfg.loggingConfig;
        };
      };
      ConfigMap.pynixd = {
        metadata.labels = cfg.labels;
        data = {
          "nix.conf" =
            readConfigurationFile ekn.lib.packageRef.enabled cfg.pynixd.controller.nixConfig.nixConf;
          "logging.json" = builtins.toJSON cfg.loggingConfig;
        };
      };
    };
  };
}
