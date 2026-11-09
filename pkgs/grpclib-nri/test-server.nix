# SPDX-License-Identifier: Apache-2.0

# The NRI server `tests/test_nri_server.py` talks to. Real containerd code, so
# the suite tests the protocol rather than a Python model of it.
{
  lib,
  buildGoModule,
}:

buildGoModule {
  pname = "nri-test-server";
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
  vendorHash = "sha256-bpKT8mHSlA5eP67C3B2ws+BF2S/B+dMH5GYqV2edcXg=";
  doCheck = false;

  ldflags = [
    "-s"
    "-w"
  ];

  meta.mainProgram = "grpclib-nri-test-server";
}
