# SPDX-License-Identifier: Apache-2.0

# Cache uses a patched Nix to keep storePaths registrationTime up2date when they're referenced
# through the Daemon protocol
{
  pkgs,
  dinix,
}:
let
  inherit (pkgs) lib;
  dinixEval = import dinix {
    inherit pkgs;
    modules = [
      ../modules/gc.nix
      ../modules/users.nix
      ../modules/nix-daemon.nix
      ../modules/setup.nix
      ../modules/logger.nix
      {
        config = {
          gc = {
            retainSeconds = 86400;
            intervalSeconds = 3600;
          };
          # Umbrella service for cache
          services.cache = {
            type = "internal";
            depends-on = [
              "nix-daemon"
              "gc"
            ];
          };
        };
      }
    ];
  };

in
pkgs.buildEnv {
  name = "cacheEnv";
  paths = with pkgs; [
    dinixEval.config.containerWrapper
    # `pynixd` starts as `appstarter run pynixd-nixkube-central`, out of this
    # environment. The image's copy is shadowed: the PVC covers /nix once the
    # pod runs, and nothing in the image can exec after that. Issue #49.
    appstarter
    bash
    coreutils
    nix
    openssh
    pynixd-nixkube
    pynixd-nixkube.fakeNss
    tini
    # dev
    fishMinimal
  ];
  # So we can peek into eval
  passthru.dinixEval = dinixEval;
}
