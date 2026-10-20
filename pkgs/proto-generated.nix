# SPDX-License-Identifier: Apache-2.0

# The source tree of one `*-proto-python` project.
#
# Not a fragment to splice into a build: this produces the complete project --
# `pyproject.toml` and `README.md` beside the `src/<package>` that protoc
# writes -- so the Python builder takes it as a plain `src`. Nothing in the
# checkout is `src/`; every module comes out of a `.proto`.
#
# Generating here rather than in the package's own build is what lets
# pyproject.nix build these at all. protoc runs each plugin as a separate
# process that has to import its own closure, and pyproject.nix's builders
# deliberately propagate nothing, so a codegen step inside the package would
# have to hand-resolve three plugin closures. `python.withPackages` arranges
# exactly that, and it belongs out here.
{
  lib,
  runCommand,
  protobuf,
  python3,
}:

{
  # The project directory in this repository. Its `pyproject.toml` is copied
  # into the result, so the metadata Nix reads at evaluation time and the
  # metadata hatchling reads inside the build are the same file.
  projectRoot,
  # The Python package protoc writes into, at `src/<package>`. This is the
  # project's distribution name as well.
  package,
  version,
  # Writes the `.proto` files into `$protoDir`. Their layout there is the
  # proto path, so a file that another one imports has to land where the
  # `import` line says.
  prepareProtos,
  # Passed to protoc, relative to `$protoDir`.
  protoFiles,
  # Runs after protoc, against `$out`. protoc emits absolute imports between
  # the modules it writes, which are wrong once they are inside a package.
  fixImports ? "",
}:

let
  # The three plugins protoc runs, plus the runtime protobuf they import.
  # `withPackages` and not `nativeBuildInputs`: a plugin is a separate
  # process, and this is what puts its own closure on its `sys.path`.
  pythonEnv = python3.withPackages (ps: [
    ps.grpclib
    ps.mypy-protobuf
    ps.grpcio-tools
    ps.protobuf
  ]);
in
runCommand "${package}-proto-source"
  {
    inherit version;
    nativeBuildInputs = [
      protobuf
      pythonEnv
    ];
    passthru = { inherit pythonEnv; };
  }
  ''
    protoDir="$NIX_BUILD_TOP/proto"
    mkdir -p "$protoDir" "$out/src/${package}"

    # `projectRoot + "/..."` and not `''${projectRoot}/...`: the second copies
    # the whole directory, `generated.nix` included, into the build's inputs.
    cp ${projectRoot + "/pyproject.toml"} "$out/pyproject.toml"
    ${lib.optionalString (builtins.pathExists (projectRoot + "/README.md")) ''
      cp ${projectRoot + "/README.md"} "$out/README.md"
    ''}

    # protoc writes neither of these, and both are part of the package:
    # `__init__.py` makes it importable, `py.typed` publishes the annotations
    # the `--mypy_out` plugin generates beside each module.
    touch "$out/src/${package}/__init__.py"
    touch "$out/src/${package}/py.typed"

    ${prepareProtos}

    protoc \
      --proto_path="$protoDir" \
      --python_out="$out/src/${package}" \
      --grpclib_python_out="$out/src/${package}" \
      --mypy_out="$out/src/${package}" \
      ${lib.concatStringsSep " " protoFiles}

    ${fixImports}
  ''
