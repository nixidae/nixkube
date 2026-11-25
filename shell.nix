# SPDX-License-Identifier: Apache-2.0

let
  default = (import ./. { }).internal;
  inherit (default) pkgs;
  inherit (pkgs) lib;
  suites = builtins.attrValues default.pythonTests;

  # One venv holding every project a suite in `nix/tests.nix` tests, with the
  # extras that suite asks for.
  #
  # **Not `python3.withPackages`.** That keeps only importable modules, so it
  # drops an application together with everything the application propagates,
  # and the list then has to name each project's `.dependencies` beside the
  # project itself. A venv has no such rule, so nothing here restates the
  # dependency graph: a name missing from a pyproject.toml fails resolution
  # rather than falling back to a store copy.
  #
  # What zuban checks against, by hand: in a package directory,
  # `zuban mypy --python-executable "$(command -v python)"`.
  python = pkgs.pythonSet.mkVirtualEnv "nixkube-dev-env" (
    lib.mergeAttrsList (map (s: s.spec) suites)
  );

  # Sphinx and what it imports, as their own environment. They are a tool this
  # shell runs, not something any project here declares, so they have no place
  # in the venv above.
  docsPython = default.nixvenv {
    python = pkgs.python3;
    packages = pp: [
      pp.sphinx
      pp.myst-parser
      pp.furo
    ];
    executables = [ "sphinx-build" ];
  };

  # `pylsp-mypy` fails its own test suite on python3.14 in the pinned
  # nixpkgs, so the whole shell refused to build: an editor integration
  # was able to break the shell.
  #
  #   FAILED test/test_plugin.py::test_plugin - assert 2 == 1
  #   FAILED test/test_plugin.py::test_handling_of_line_endings[...] - KeyError: 'code'
  #   FAILED test/test_plugin.py::test_multiple_workspaces - assert 1 == 0
  #   ... 7 failed, 32 passed
  #
  # Broken upstream, not here: `cache.nixos.org` returns 404 for it, so
  # nixpkgs' own builders did not get it either. Every other tool in this
  # shell returns 200.
  #
  # Skipping its tests is safe in a way that skipping Nix's was not (see
  # `pkgs/default.nix`). There is no cache entry to lose, because there is no
  # successful build to cache, and nothing outside this shell holds it -- it
  # ships in no image and no environment.
  pylsp-mypy = pkgs.python3Packages.pylsp-mypy.overridePythonAttrs (_: {
    doCheck = false;
    doInstallCheck = false;
  });

  # A venv of its own: a Python package in `packages` makes nixpkgs' setup
  # hook put it on PYTHONPATH, for every Python in the shell.
  pylsp = default.nixvenv {
    python = pkgs.python3;
    packages = pp: [
      pp.python-lsp-server
      pylsp-mypy
      pp.pylsp-rope
      pp.python-lsp-ruff
    ];
    executables = [ "pylsp" ];
  };

  # One pytest per project, as `nix/tests.nix` runs them. Every project's
  # `tests/` is a package named `tests`, so one pytest over two refuses the
  # second with ImportPathMismatchError.
  unitTests = pkgs.writeShellApplication {
    name = "nixkube-unit-tests";
    text = ''
      if [ ! -d pkgs/nixkube ]; then
        echo "nixkube-unit-tests: run it from nixkube/" >&2
        exit 2
      fi
      failed=()
      for project in ${lib.escapeShellArgs (map (s: s.project) suites)}; do
        (cd "pkgs/$project" && python -m pytest "$@") || failed+=("$project")
      done
      if [ ''${#failed[@]} -gt 0 ]; then
        echo "nixkube-unit-tests: failed: ''${failed[*]}" >&2
        exit 1
      fi
    '';
  };
in
pkgs.mkShell {
  # What each suite needs at run time, as `nix/tests.nix` sets it.
  env = lib.mergeAttrsList (map (s: s.suiteEnv) suites);
  packages = [
    unitTests
    python
    pkgs.xonsh
    docsPython
    pkgs.cachix
    pkgs.cargo
    pkgs.go
    pkgs.kluctl
    pkgs.kubectx
    pkgs.zuban
    pylsp
    pkgs.regctl
    pkgs.ruff
    pkgs.rustc
    pkgs.skopeo
    pkgs.stern
    pkgs.dive
    default.treefmt
    # The `vivarium` CLI, for the guest tests: `ctl`, `monitor`, `run`.
    (import default.sources.vivarium {
      inherit pkgs;
      inherit (default) sources;
    }).packages.vivarium
  ];
  # What `activate` sets. pylsp runs on its own venv, and jedi finds the
  # project's packages through this.
  shellHook = ''
    export VIRTUAL_ENV=${python}
  '';
}
