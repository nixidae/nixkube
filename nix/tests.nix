# SPDX-License-Identifier: Apache-2.0

# The Python suites, one derivation each, each behind zuban in strict mode.
#
# pyproject.nix's builders have no `pytestCheckHook` equivalent: a package
# propagates nothing, so its own build cannot import what it depends on. A
# suite therefore runs against a venv, which is also closer to what ships.
#
# **Each suite is its own `checks` member.** Before this they rode along inside
# the packages, and `checkMembers.pynixd-nixkube-tests` named a package rather
# than a test result -- so a suite that stopped running would have looked
# exactly like a suite that passed.
{
  lib,
  pkgs,
  pythonSet,
}:

let
  # Only `tests/` and the `pyproject.toml` that configures pytest. The project
  # itself comes from the venv, and `tests/__init__.py` makes pytest put this
  # directory on `sys.path` rather than the package's own source.
  suiteSource =
    projectRoot:
    lib.fileset.toSource {
      root = projectRoot;
      fileset = lib.fileset.unions [
        (lib.fileset.fileFilter (file: file.hasExt "py") (projectRoot + "/tests"))
        (projectRoot + "/pyproject.toml")
      ];
    };

  # The project's Python and its `mypy.ini`, for zuban.
  typeSource =
    projectRoot:
    lib.fileset.toSource {
      root = projectRoot;
      fileset = lib.fileset.unions [
        (lib.fileset.fileFilter (file: file.hasExt "py" || file.hasExt "pyi") projectRoot)
        (projectRoot + "/mypy.ini")
      ];
    };

  mkSuite =
    {
      name,
      projectRoot,
      # `pythonSet.mkVirtualEnv`'s spec: which projects, with which extras.
      spec,
      # Packages whose every module is imported before the suite runs.
      #
      # **A walk, not a list.** The builders have no `pythonImportsCheck`, and
      # a module no test imports is a module nothing loads until a cluster
      # does: `pynixd_nixkube` shipped four modules naming `pynixd.types.ids`
      # after pynixd renamed it, so the package built and then died at
      # start-up with `ModuleNotFoundError`. A hand-written list is the same
      # check with a way to fall behind -- a module added and not listed is
      # unchecked and says nothing.
      importPackages ? [ ],
      env ? { },
      nativeBuildInputs ? [ ],
    }:
    let
      venv = pythonSet.mkVirtualEnv "${name}-test-env" spec;
    in
    pkgs.runCommand "${name}-tests"
      (
        env
        // {
          nativeBuildInputs = [
            venv
            pkgs.zuban
          ]
          ++ nativeBuildInputs;
        }
      )
      ''
        # A directory of its own: beside `tests/`, the source would shadow
        # the venv's copy, which is the one pytest has to import.
        cp -r ${typeSource projectRoot} typecheck
        (cd typecheck && zuban mypy --python-executable ${venv}/bin/python)
        cp -r ${suiteSource projectRoot}/. .
        chmod -R +w .
        ${lib.optionalString (importPackages != [ ]) ''
          python ${./import-walk.py} ${lib.escapeShellArgs importPackages}
        ''}
        pytest -p no:cacheprovider tests
        touch "$out"
      '';
in
{
  appstarter-tests = mkSuite {
    name = "appstarter";
    projectRoot = ../pkgs/appstarter;
    spec.appstarter = [ "test" ];
    importPackages = [ "appstarter" ];
  };

  nixkube-tests = mkSuite {
    name = "nixkube";
    projectRoot = ../pkgs/nixkube;
    spec.nixkube = [ "test" ];
    importPackages = [ "src" ];
    # The waiter in tests/test_vm.py runs under it, as it does in a VM.
    env.NRI_VM_BUSYBOX = "${pkgs.pkgsStatic.busybox}/bin/busybox";
  };

  pynixd-nixkube-tests = mkSuite {
    name = "pynixd-nixkube";
    projectRoot = ../pkgs/pynixd-nixkube;
    spec.pynixd-nixkube = [ "test" ];
    importPackages = [ "pynixd_nixkube" ];
    # `pynixd_nixkube/setup.py` reads both at import time, so a suite that
    # does not set them fails on collection rather than on an assertion.
    env = {
      FAKE_NSS = "${pkgs.pynixd-nixkube.fakeNss}";
      CA_CERTS = "${pkgs.dockerTools.caCertificates}";
    };
  };

  nri-wait-tests = mkSuite {
    name = "nri-wait";
    projectRoot = ../pkgs/nri-wait;
    spec.nri-wait = [ "test" ];
    importPackages = [ "nri_wait" ];
  };

  grpclib-ttrpc-tests = mkSuite {
    name = "grpclib-ttrpc";
    projectRoot = ../pkgs/grpclib-ttrpc;
    spec.grpclib-ttrpc = [ "test" ];
    importPackages = [ "grpclib_ttrpc" ];
    env.TTRPC_TEST_SERVER = lib.getExe (pkgs.callPackage ../pkgs/grpclib-ttrpc/test-server.nix { });
  };

  grpclib-nri-tests = mkSuite {
    name = "grpclib-nri";
    projectRoot = ../pkgs/grpclib-nri;
    spec.grpclib-nri = [ "test" ];
    importPackages = [ "grpclib_nri" ];
    env.NRI_TEST_SERVER = lib.getExe (pkgs.callPackage ../pkgs/grpclib-nri/test-server.nix { });
  };
}
