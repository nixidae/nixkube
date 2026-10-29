# The option reference of a module tree, as a Markdown page for Sphinx.
# nixidae/nixidae#1105.
{ lib, pkgs }:
{
  # The page heading.
  title,
  # The `options` of an evaluated module tree.
  options,
  # Only options whose name starts with one of these appear.
  prefixes,
  # The source tree. A declaration under it links to its file on GitHub.
  root,
  # The GitHub repository that holds the exported source, as `owner/name`.
  repository,
}:
let
  rootPrefix = "${toString root}/";

  link =
    decl:
    let
      file = lib.removePrefix rootPrefix (toString decl);
    in
    if lib.hasPrefix rootPrefix (toString decl) then
      {
        name = file;
        url = "https://github.com/${repository}/blob/main/${file}";
      }
    else
      decl;

  doc = pkgs.nixosOptionsDoc {
    inherit options;
    warningsAreErrors = false;
    transformOptions =
      opt:
      opt
      // {
        visible = opt.visible or true && lib.any (prefix: lib.hasPrefix prefix opt.name) prefixes;
        declarations = map link opt.declarations;
      };
  };
in
pkgs.runCommand "options.md" { inherit title; } ''
  {
    printf '# %s\n\n' "$title"
    cat ${doc.optionsCommonMark}
  } > $out
''
