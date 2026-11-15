# SPDX-License-Identifier: Apache-2.0

{
  callPackage,
  nixvenv,
  graphviz,
  optionsReference,
}:
callPackage ./shared/sphinx-site.nix { inherit nixvenv; } {
  name = "nixkube";
  root = ../.;
  tools = [ graphviz ];
  pages."options.md" = optionsReference;
}
