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
      #
      # The node's store server substitutes from pynixd itself: a huggorm
      # client sends no per-call substituters, as `nix build
      # --extra-substituters` did. `ssh_config` bounds the connect.
      # Not `trusted=1`: the server serves untrusted clients too, so pynixd's
      # paths need a signature; the server trusts the cluster's key at start
      # (`store_server.server_environment`).
      #
      # A path pynixd substitutes keeps only its cache's signature, so the node
      # trusts every key pynixd does. sg1, 2026-11-28: pynixd trusts flox for
      # the GX10s' GPU stack, and the nodes did not.
      node.nixConfig.settings =
        let
          pynixd = cfg.pynixd.controller.nixConfig.settings;
          pynixdKeys = lib.subtractLists (cfg.nixConfig.settings.trusted-public-keys or [ ]) (
            lib.unique ((pynixd.trusted-public-keys or [ ]) ++ (pynixd.extra-trusted-public-keys or [ ]))
          );
        in
        lib.mkMerge [
          cfg.nixConfig.settings
          { store = lib.mkDefault "daemon"; }
          (lib.mkIf cfg.pynixd.enable {
            extra-substituters = [ "ssh-ng://nix@pynixd?priority=20" ];
          })
          (lib.mkIf (cfg.pynixd.enable && pynixdKeys != [ ]) {
            extra-trusted-public-keys = pynixdKeys;
          })
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
