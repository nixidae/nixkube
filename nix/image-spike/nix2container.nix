# SPDX-License-Identifier: Apache-2.0
#
# Spike A: the layering policy through nix2container.
#
# `buildLayer` takes the store paths of one layer by name. `layers` is the
# dedupe: a path already held by a layer listed there is not repeated, so the
# chain has to be built base first and each link has to list every link before
# it.
#
# nix2container writes no tarball. The image is a JSON manifest that names
# store paths, and `skopeo-nix2container` sends them straight out of the
# store. That is a different push path from `niximage.nix`, which pipes a
# tarball through gzip.
{
  pkgs,
  sources,

  app ? null,
}:
let
  n2c = (import sources.nix2container { inherit pkgs; }).nix2container;
  policy = import ./policy.nix (
    { inherit pkgs; } // pkgs.lib.optionalAttrs (app != null) { inherit app; }
  );

  # Fold the groups into a chain, each link listing the ones before it.
  chain = builtins.foldl' (
    built: group:
    built
    ++ [
      (n2c.buildLayer {
        inherit (group) deps;
        layers = built;
      })
    ]
  ) [ ] policy.groups;

  named = builtins.length chain;
in
{
  image = n2c.buildImage {
    name = "nix-spike-n2c";
    tag = "spike";

    # The remainder. `maxLayers` applies to this layer alone, and everything
    # the named layers already hold is deduped away by `layers`.
    layers = chain;
    maxLayers = policy.restLayers;

    copyToRoot = policy.contents;

    config = {
      Env = [ "PATH=${pkgs.lib.makeBinPath policy.runtimeInputs}" ];
      Entrypoint = [ "${pkgs.bashInteractive}/bin/bash" ];
    };
  };

  inherit (policy) groups;
  namedLayers = named;
  skopeo = (import sources.nix2container { inherit pkgs; }).skopeo-nix2container;
}
