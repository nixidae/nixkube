# SPDX-License-Identifier: Apache-2.0

# Node environment: nixkube is the sole entrypoint — no init system needed.
# All supervision (the store server, GC, CSI, NRI) is handled by nixkube itself.
# Only debug/runtime tools are included alongside nixkube.
{
  pkgs,
  # pynixd, for a node whose `nixkube.node.storeServer` is one. Off, the
  # environment leaves pynixd's closure out.
  withPynixd ? false,
  ...
}:
pkgs.buildEnv {
  name = "nodeEnv";
  paths =
    with pkgs;
    [
      nixkube
      # `nix-node` starts as `appstarter run nixkube`, out of this environment.
      # The image's copy is shadowed: the node's store covers /nix, so nothing
      # in the image can exec once the pod runs. Issue #49.
      appstarter
      tini
      bash
      coreutils
      fishMinimal
      nix
      openssh
      util-linuxMinimal
      gnugrep
      getent
      doggo
      iputils
      curl
    ]
    ++ lib.optional withPynixd pynixd-cli;
}
