# SPDX-License-Identifier: Apache-2.0

{ lib, ... }:
{
  # A caller that imports nixkube from an umbrella passes that umbrella's
  # sources, so pynixd and the rest move with its lock. This repository's
  # own lock only answers when nobody did (#79).
  _module.args.sources = lib.mkDefault (import ../nix/sources.nix);

  imports = [
    ./options.nix
    ./assertions.nix
    ./namespace.nix
    ./daemonset.nix
    ./csidriver.nix
    ./config.nix
    ./pynixd.nix
    ./nixos-host.nix

    ./rbac.nix
    ./undeploy.nix
    ./secret.nix
  ];
}
