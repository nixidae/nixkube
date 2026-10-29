# The option reference of a module tree, as `nixosOptionsDoc` makes it.
# nixidae/nixidae#1105.
{ lib, pkgs }:
{
  # The `options` of an evaluated module tree.
  options,
  # Only options whose name starts with one of these appear.
  prefixes,
  # A declaration under this path is shown relative to it.
  declarationsRoot,
}:
pkgs.nixosOptionsDoc {
  inherit options;
  warningsAreErrors = false;
  transformOptions =
    opt:
    opt
    // {
      visible = opt.visible or true && lib.any (prefix: lib.hasPrefix prefix opt.name) prefixes;
      declarations = map (
        decl:
        let
          prefix = toString declarationsRoot;
        in
        if lib.hasPrefix prefix (toString decl) then lib.removePrefix prefix (toString decl) else decl
      ) opt.declarations;
    };
}
