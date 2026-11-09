# SPDX-License-Identifier: Apache-2.0

# The ttRPC server `tests/test_server_integration.py` talks to. Real containerd
# code, so the suite tests the protocol rather than a Python model of it.
{
  lib,
  buildGoModule,
}:

buildGoModule {
  pname = "ttrpc-test-server";
  version = "0.1.0";

  src = lib.fileset.toSource {
    root = ./go;
    fileset = lib.fileset.unions [
      ./go/go.mod
      ./go/go.sum
      ./go/main.go
    ];
  };
  proxyVendor = true;
  vendorHash = "sha256-voE9iZ0rUp/iCNROLiKjuQdQS9rLVqPK0SlSGp0kPuU=";
  doCheck = false;

  ldflags = [
    "-s"
    "-w"
  ];

  meta.mainProgram = "grpclib-ttrpc-test-server";
}
