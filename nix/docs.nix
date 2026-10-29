# SPDX-License-Identifier: Apache-2.0

{
  lib,
  stdenvNoCC,
  python,
  sphinx,
  myst-parser,
  furo,
  graphviz,
  nixvenv,
}:
let
  pythonEnv = nixvenv {
    inherit python;
    packages = _: [
      sphinx
      myst-parser
      furo
    ];
    executables = [ "sphinx-build" ];
  };
in
stdenvNoCC.mkDerivation {
  pname = "nixkube-docs";
  version = "0";

  src = lib.fileset.toSource {
    root = ../.;
    fileset = lib.fileset.unions [
      ../docs
      ../nix/shared/sphinx_shared.py
    ];
  };

  nativeBuildInputs = [
    pythonEnv
    graphviz
  ];

  buildPhase = ''
    runHook preBuild
    NIXIDAE_DOCS_OFFLINE=1 sphinx-build -b html docs "$out" -W
    runHook postBuild
  '';

  dontInstall = true;
}
