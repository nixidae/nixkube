# SPDX-License-Identifier: Apache-2.0

# The public surface of this repository, for a consumer who uses flakes.
#
# **This is not how the repository builds.** `default.nix` is, and nixidae
# (https://github.com/nixidae/nixidae), which this repository is exported
# from, hands it every source. This file exists because flakes have the
# market share: it lets somebody write an input for this repository and get a
# curated set of outputs, rather than nothing.
#
# So it holds no logic. It names what is public and calls `default.nix`, and
# a change to how anything is built happens there.
#
# Two inputs, and neither duplicates a pin nixidae keeps.
#
#   nixpkgs   The consumer's, and the point of the exercise. It is handed to
#             nixidae in place of the revision its flake.lock names, so
#             `inputs.<this>.inputs.nixpkgs.follows = "nixpkgs"` does what a
#             flake user expects it to.
#
#   nixidae   Which nixidae commit, and nothing else. `flake.lock` pins it,
#             and lock-subtrees writes that lock. Every other source comes
#             from that commit's own flake.lock.
{
  description = "A CSI driver for Nixxing Kubernetes";

  inputs = {
    nixpkgs.url = "github:nixos/nixpkgs/nixpkgs-unstable";
    nixidae = {
      url = "github:nixidae/nixidae";
      flake = false;
    };
  };

  outputs =
    {
      self,
      nixpkgs,
      nixidae,
    }:
    let
      inherit (nixpkgs) lib;
      # Linux only. This is a CSI driver and a container runtime plugin.
      forEachSystem = lib.genAttrs [
        "aarch64-linux"
        "x86_64-linux"
      ];

      # nixidae's own set, with the consumer's nixpkgs in place of the one it
      # names.
      sources = import "${nixidae}/nix/wire.nix" {
        overrides.nixpkgs = nixpkgs.outPath;
      };

      # No `pkgs` argument: this repository builds its own package set,
      # because it applies an overlay of its own.
      each = forEachSystem (system: import ./. { inherit sources system; });
    in
    {
      # `kubenixApply` and its siblings are deliberately absent. Each one is
      # an easykubenix evaluation rather than a derivation, so `nix build`
      # cannot realise it and `nix flake show` throws on it. A consumer who
      # wants one takes the module below and evaluates it themselves.
      packages = forEachSystem (system: {
        nixkube-docs = each.${system}.docs.default;
        default = each.${system}.docs.default;
      });

      # The easykubenix module that installs the driver, which is what a
      # consumer actually wants from this repository.
      easykubenixModules.default = ./kubenix;

      formatter = forEachSystem (system: each.${system}.treefmt);
    };
}
