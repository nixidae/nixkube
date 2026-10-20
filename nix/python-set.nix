# SPDX-License-Identifier: Apache-2.0

# This repository's Python projects, as a pyproject.nix builders set.
#
# **Why not nixpkgs' Python builders.** They work by propagation, which has
# cost this repository twice. A name in `dependencies` reaches the runtime
# closure of every consumer whether or not anything imports it -- that is how
# a codegen toolchain and 310 MiB got into `nodeEnv`. And `withPackages` drops
# an application together with everything it propagates, so an environment
# assembled that way is missing what the application carried. pyproject.nix's
# builders put runtime dependencies in `passthru` and assemble real
# virtualenvs, so neither failure has a place to happen.
#
# The other reason is #49: one application `exec`s another out of a different
# store, so whatever the first one exports reaches the second. Measured on
# `pkgs.nixkube`, `buildPythonApplication` produces a bash wrapper that
# prepends 90 store `bin` directories to `PATH` and exports
# `PYTHONNOUSERSITE` and three `SETUP_*` variables. The exec'd program
# inherits all of it and resolves `nix`, `git` and `ssh` out of the first
# program's store.
#
# Imports do not leak: nixpkgs bakes a 43-entry `site.addsitedir` list into
# the entry-point script itself, not into the environment. A venv entry point
# is a plain script with a shebang into the venv, so it exports nothing and
# reads one prefix.
#
# **The machinery is nanopynix', and this repository only wires it.**
# `ps.mkPythonSet` and not `nanopynix.pythonSetWith`: nothing here imports a
# nanopynix project, and `pythonSetWith` composes all of them into the set.
{
  lib,
  pkgs,
  sources,
  python ? pkgs.python3,
}:

let
  inherit (import sources.nanopynix { inherit pkgs sources; }) ps;

  protoGenerated = pkgs.callPackage ../pkgs/proto-generated.nix { python3 = python; };

  # Keyed by distribution name, which is what `pyproject.toml` declares and
  # what the set resolves. It is not the directory name: `pkgs/csi-proto-python`
  # ships a distribution called `csi`.
  projects = {
    appstarter = ../pkgs/appstarter;
    csi = ../pkgs/csi-proto-python;
    cri = ../pkgs/cri-proto-python;
    nri = ../pkgs/nri-proto-python;
    ttrpc = ../pkgs/ttrpc-proto-python;
    grpclib-ttrpc = ../pkgs/grpclib-ttrpc;
    grpclib-nri = ../pkgs/grpclib-nri;
    nri-wait = ../pkgs/nri-wait;
    pynixd-nixkube = ../pkgs/pynixd-nixkube;
    nixkube = ../pkgs/nixkube;
  };

  # The four projects whose `src/` comes out of protoc. Each `generated.nix`
  # emits a complete project, `pyproject.toml` included, so the set builds it
  # as an ordinary source.
  generatedSources =
    lib.mapAttrs (_: root: pkgs.callPackage (root + "/generated.nix") { inherit protoGenerated; })
      (
        lib.getAttrs [
          "csi"
          "cri"
          "nri"
          "ttrpc"
        ] projects
      );

  # **The files `pyproject.toml` names, not the project directory.**
  # `renderers.mkDerivation` takes `src = projectRoot` whole, and these
  # directories hold `tests/`, a README nothing installs and whatever a local
  # `pytest` writes. Each of those would change the package's hash and rebuild
  # `cacheEnv` and the init image behind it. A project without
  # `tool.hatch.build.targets.wheel.packages` builds from its
  # `pyproject.toml` alone.
  sourceOf =
    name: root:
    generatedSources.${name} or (ps.projectSource {
      src = root;
      name = "${name}-source";
    });

  project =
    name: root:
    ps.mkProject {
      projectRoot = root;
      inherit python;
      extra = rendered: {
        src = sourceOf name root;
        meta = rendered.meta // {
          platforms = lib.platforms.linux;
        };
      };
    };
in
ps.mkPythonSet {
  inherit python;

  # `kr8s` and `pynixd` are nixpkgs Python packages, but this repository's own
  # rather than the tree's, so they are lifted in as roots instead of being
  # looked up by name.
  nixpkgsRoots = [
    pkgs.kr8s
    pkgs.pynixd
  ]
  ++ ps.nixpkgsRootsFor {
    inherit python;
    projectRoots = lib.attrValues projects;
    # `nixpkgsRootsFor` excludes the directory names on its own, and these
    # projects are not named after their directories.
    exclude = lib.attrNames projects ++ [
      "kr8s"
      "pynixd"
    ];
  };

  overlay =
    pySelf: pyPrev:
    {
      # **nixpkgs under-declares `grpclib`.** Its `dependencies` are `h2` and
      # `multidict`, and `grpclib/_typing.py` imports `typing_extensions` at
      # the top with no guard. Lifting derives the metadata from those
      # declarations, so a venv holding `grpclib` without
      # `typing-extensions` fails on the first import of `grpclib.server`.
      #
      # It hides in nixpkgs because `pythonImportsCheck` names `grpclib`, and
      # `grpclib/__init__.py` does not reach `_typing`.
      grpclib = pyPrev.grpclib.overrideAttrs (old: {
        passthru = old.passthru // {
          dependencies = old.passthru.dependencies // {
            typing-extensions = [ ];
          };
        };
      });
    }
    // lib.mapAttrs (name: root: pySelf.callPackage (project name root) { }) projects;
}
