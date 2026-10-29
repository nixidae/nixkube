# SPDX-License-Identifier: Apache-2.0

# What a guest test changes about the CI deployment, and nothing else.
#
# ../../default.nix adds this to the module list `kubenixCI1` and
# `kubenixCI2` are built from, so the guest deploys the same thing CI does
# plus what is here. Each setting names the guest fact that forces it.
{
  lib,
  pkgs,
  sources,
  ...
}:
let
  cache = (import sources.vivarium { inherit pkgs sources; }).lib.cacheFacts;
  fromCache = {
    substituters = lib.mkForce [ cache.url ];
    trusted-public-keys = [ cache.trustedPublicKey ];
  };
in
{
  /*
    Run the image this checkout built, not the one published last.

    `nix run --file . internal.ciTestCache.driver` is the only end-to-end test of the
    node boot path, and it deployed `imagePullPolicy = "Always"`. So kubelet
    went to ghcr.io however the guest was prepared, and the test ran an
    `appstarter` this repository did not build -- a change under `pkgs/`
    could not be tried here at all.

    That is not hypothetical. `store.copy`, the fallback for a `nodeEnv` the
    node cannot fetch, called `nix copy` with no `--from`, so it read the
    default store through a daemon socket no appstarter container has. It
    could never have worked, and no test here could see it: the uml test
    seeds the guest store first, so the fallback never runs, and this test
    ran somebody else's binary.

    `IfNotPresent` and not `Never`. The CSI sidecars come from
    registry.k8s.io and still have to be pulled; only nixkube's own
    containers take this option. ./ci.nix imports the tarball carrying the
    matching tag.
  */
  nixkube.imagePullPolicy = "IfNotPresent";

  /*
    A claim the guest's disk can honour.

    The guest offers one hostPath volume whose `capacity.storage` is
    `vivarium.diskSize`, because the volume is a directory on that disk.
    That is 8192Mi, and the default claim is 10Gi, so nothing binds and
    `pynixd-0` never schedules: `0/1 nodes are available: 1 node(s) didn't
    find available persistent volumes to bind`.

    1Gi, against the 8.3M that run measured pynixd's volume using. The
    figure is a claim and not a reservation -- nothing enforces a hostPath
    volume's size -- so it buys nothing to ask for more.
  */
  nixkube.pynixd.storageSize = "1Gi";

  /*
    The run's cache guest and nothing else, for every role
    (`vivarium.recipes.cache` in ./ci.nix), as in ./manifest.nix.

    Keeping string context on the DaemonSet's store paths makes the node
    and cache environments part of the manifest's closure, which is what
    the cache guest serves.
  */
  nixkube.discardStringContext = false;
  nixkube.node.nixConfig.settings = fromCache;
  nixkube.pynixd.controller.nixConfig.settings = fromCache;
  nixkube.pynixd.builder.nixConfig.settings = fromCache;
}
