/**
  A source's Sphinx site: `docs/` built to HTML, with warnings as errors.
  nixidae/nixidae#1347.

  `callPackage` it from `python3Packages`. The Python arguments have defaults
  because a source that passes its own `pythonEnv` may have no such scope.
*/
{
  lib,
  stdenvNoCC,
  python ? null,
  sphinx ? null,
  myst-parser ? null,
  furo ? null,
  nixvenv ? null,
}:
{
  # The source's name. The derivation is `<name>-docs`.
  name,
  # The source's root directory.
  root,
  # What Sphinx reads beyond `docs/` and `nix/shared/sphinx_shared.py`.
  files ? [ ],
  # The packages autodoc imports.
  packages ? [ ],
  # An environment with `sphinx-build`.
  pythonEnv ? nixvenv {
    inherit python;
    packages =
      _:
      packages
      ++ [
        sphinx
        myst-parser
        furo
      ];
    executables = [ "sphinx-build" ];
  },
  # Programs Sphinx runs, such as graphviz's `dot`.
  tools ? [ ],
  # Generated pages: each file goes to `docs/<page>` before the build.
  pages ? { },
}:
stdenvNoCC.mkDerivation {
  pname = "${name}-docs";
  version = "0";

  src = lib.fileset.toSource {
    inherit root;
    fileset = lib.fileset.unions (
      [
        (root + "/docs")
        (root + "/nix/shared/sphinx_shared.py")
      ]
      ++ files
    );
  };

  nativeBuildInputs = [ pythonEnv ] ++ tools;

  buildPhase = lib.concatLines (
    [ "runHook preBuild" ]
    ++ lib.mapAttrsToList (page: file: "cp ${file} docs/${page}") pages
    ++ [
      ''NIXIDAE_DOCS_OFFLINE=1 sphinx-build -b html docs "$out" -W''
      "runHook postBuild"
    ]
  );

  dontInstall = true;
}
