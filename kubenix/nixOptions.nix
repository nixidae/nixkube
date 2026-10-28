# SPDX-License-Identifier: Apache-2.0

{
  pkgs,
  nix ? pkgs.nix,
}:
let
  inherit (pkgs) lib;
  defaultSystemFeatures = [
    "nixos-test"
    "benchmark"
    "big-parallel"
    # "kvm"
  ];

  deduplicatedListOf =
    type:
    let
      listType = lib.types.listOf type;
      baseMerge = listType.merge;
    in
    listType
    // {
      merge = loc: defs: lib.unique (baseMerge loc defs);
    };

  semanticConfType =
    with lib.types;
    let
      confAtom =
        nullOr (oneOf [
          bool
          int
          float
          str
          path
          package
        ])
        // {
          description = "Nix config atom (null, bool, int, float, str, path or package)";
        };
    in
    attrsOf (either confAtom (deduplicatedListOf confAtom));
in
with lib;
types.submodule (
  { config, ... }:
  {
    options = {
      nixConf = mkOption {
        type = import ./configurationFileType.nix { inherit lib; };
        internal = true;
      };

      checkConfig = mkOption {
        type = types.bool;
        default = true;
        internal = true;
      };

      checkAllErrors = mkOption {
        type = types.bool;
        default = true;
        internal = true;
      };

      extraOptions = mkOption {
        description = "Extra lines to add to nix.conf";
        type = types.lines;
        default = "";
      };

      settings = mkOption {
        description = "Settings rendered to nix.conf";
        type = types.submodule {
          freeformType = semanticConfType;
          options = { };
        };
        default = { };
      };
    };
    config = {
      settings = {
        trusted-public-keys = [
          "cache.nixos.org-1:6NCHdD59X431o0gWypbMrAURkbJ16ZPMQFGspcDShjY="
        ];
        substituters = [
          "https://cache.nixos.org"
        ];
        trusted-users = [ "root" ];
        system-features = defaultSystemFeatures;
      };
      # A file override must discard the default preview text with its file.
      nixConf = mkDefault (
        let
          file =
            (pkgs.formats.nixConf {
              inherit (config)
                checkAllErrors
                checkConfig
                extraOptions
                ;
              package = nix.out;
              inherit (nix) version;
            }).generate
              "nix.conf"
              config.settings;
        in
        {
          inherit file;
          inherit (file) text;
        }
      );
    };
  }
)
