# SPDX-License-Identifier: Apache-2.0

{
  fetchFromGitHub,
  protoGenerated,
}:

let
  ttrpc = fetchFromGitHub {
    owner = "containerd";
    repo = "ttrpc";
    rev = "v1.2.7";
    sha256 = "sha256-oQamR59cQrcuw9tervKrf+2vYnweRRNgST8GObFNjTk=";
  };
in
protoGenerated {
  version = "0.11.0";
  projectRoot = ./.;
  package = "ttrpc";
  # `request.proto` imports `proto/status.proto` by that path, so the layout
  # under `$protoDir` is what makes the import resolve.
  prepareProtos = ''
    mkdir -p "$protoDir/proto"
    cp ${ttrpc}/request.proto "$protoDir/ttrpc.proto"
    cp ${./status.proto} "$protoDir/proto/status.proto"
    cp ${ttrpc}/integration/streaming/test.proto "$protoDir/streaming.proto"
  '';
  protoFiles = [
    "ttrpc.proto"
    "proto/status.proto"
    "streaming.proto"
  ];
  fixImports = ''
    touch "$out/src/ttrpc/proto/__init__.py"
    substituteInPlace "$out/src/ttrpc/ttrpc_pb2.py" \
      --replace-fail "from proto import status_pb2" "from .proto import status_pb2"
  '';
}
