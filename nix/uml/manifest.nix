# SPDX-License-Identifier: Apache-2.0

# What has to be true of a nixkube deployment for it to come up inside a Nix
# build sandbox, on User-Mode Linux.
#
# An easykubenix module rather than a patch to `kubenix/`: none of this is how
# nixkube should behave on a real cluster, and a default that only suits a
# test is a default that misleads everybody else.
#
# Two things are different in a sandbox, and both come from the same fact --
# there is no network:
#
#   images   Nothing can be pulled. Every image is imported into containerd
#            before kubelet starts (see ./images.nix and
#            `services.vivarium-k8s.extraImages`), so every container has to say
#            `Never` -- `Always`, which two of them do say, fails on an image
#            that is already there.
#
#   store    The node's Nix store is a directory of its own that the init
#            container fills by *substituting* into it. The one binary
#            cache is the run's cache guest, and every role names it and
#            nothing else.
{
  lib,
  pkgs,
  sources,
  ...
}:
let
  cache = (import sources.vivarium { inherit pkgs sources; }).lib.cacheFacts;

  # Every container in an object, whether it runs first or not.
  pullNever =
    object:
    let
      never = container: container // { imagePullPolicy = "Never"; };
      fix =
        spec:
        spec
        // lib.optionalAttrs (spec ? containers) { containers = map never spec.containers; }
        // lib.optionalAttrs (spec ? initContainers) {
          initContainers = map never spec.initContainers;
        };
    in
    if object ? spec && object.spec ? template && object.spec.template ? spec then
      lib.recursiveUpdate object { spec.template.spec = fix object.spec.template.spec; }
    # A PodTemplate holds its pod spec one level up.
    else if object ? template && object.template ? spec then
      lib.recursiveUpdate object { template.spec = fix object.template.spec; }
    else
      object;

  # The chaos phase waits for a restart after it deletes the CSI socket:
  # 30 s initial delay plus 5 x 10 s by default. 3 x 2 s here.
  fastProbes =
    object:
    let
      fast =
        container:
        container
        // lib.optionalAttrs (container ? livenessProbe) {
          livenessProbe = container.livenessProbe // {
            periodSeconds = 2;
            timeoutSeconds = 2;
            failureThreshold = 3;
          };
        }
        // lib.optionalAttrs (container ? readinessProbe) {
          readinessProbe = container.readinessProbe // {
            initialDelaySeconds = 2;
            periodSeconds = 2;
            timeoutSeconds = 2;
          };
        };
    in
    if (object.kind or "") == "DaemonSet" then
      lib.recursiveUpdate object {
        spec.template.spec.containers = map fast object.spec.template.spec.containers;
      }
    else
      object;
in
{
  kubernetes.transformers = [
    pullNever
    fastProbes
  ];

  nixkube = {
    # One architecture. Nothing here cross-compiles, and the sandbox has no
    # builder to send the other one to.
    systems = {
      x86_64-linux = true;
      aarch64-linux = false;
    };

    # The tag ./images.nix imports. The default pins scratch's index by
    # digest, and a docker-archive tarball carries a tag, not an index.
    images.scratch = "ghcr.io/nixidae/nixkube/scratch:1.0.1";

    /*
      The cache StatefulSet, for how it starts rather than for what it does.

      Nothing is built here -- every path a workload asks for is already in
      the sandbox because the test derivation named it -- so pynixd
      coordinates no distributed build in this test. What it does do is boot:
      an `appstarter-init` initContainer fills its PVC, and the pod runs
      `appstarter run` out of it. That path has no other test. `test-kind-cache`
      needs a cluster and a published image; this needs neither. Issue #49.

      The claim binds against `services.vivarium-k8s.persistentVolumes` in
      ./default.nix. `storageClassName` stays null, which means the cluster
      default, which is the `standard` class that option creates.
    */
    pynixd.enable = true;

    # The host phase switches the guest's own system. Issue #24.
    nixosHost.enable = true;
    # The broken switch waits this out. A good switch needs it to cover the
    # switch, its confirmation and the disarm Job: 7.5 and 9.7 s measured on
    # a quiet host, and 12 s at a load of 13, when 12 lost the race.
    nixosHost.revertDelay = 30;
    nixosHost.resyncSeconds = 1;

    # The chaos phase waits for the node's state to settle after a runtime
    # restart, and only this sweep sees the containers from before it go.
    nri.sweepInterval = 1;

    # The guest's whole disk is 4 GiB (`vivarium.diskSize`), and the node's
    # own store is already on it. `cacheEnv` is about 512 MiB, so this is what
    # pynixd actually writes here plus room to see it grow. The 10Gi default
    # is for a cluster; against this disk it would only look satisfied,
    # because a hostPath volume enforces no size at all.
    pynixd.storageSize = "1Gi";

    # No builders. `builder-min = 1` is the default, and pynixd created two
    # builder Jobs here that sat in ContainerCreating for the whole run --
    # every path a workload asks for is already in the sandbox, so there is
    # nothing for a builder to build and nowhere for it to pull an image from.
    pynixd.controller.settings.builder-min = 0;

    # Keeps the Nix string context on the DaemonSet's store paths, so the node
    # environment is part of the manifest's closure. That is what carries it
    # into the sandbox at all, and `vivarium.nixDatabase.extraRoots` in
    # ./default.nix registers that same closure inside the guest.
    discardStringContext = false;

    /*
      A directory of the node's own, on the node's own filesystem.

      `/` was tried first, and it is very nearly right: the guest's
      /nix/store is the build sandbox's over hostfs, so pointing the node
      at `/` gives it a store that already holds everything the manifest
      names, with nothing copied and nothing fetched. The whole DaemonSet
      came up in 34 seconds that way.

      It breaks one thing, and the thing it breaks is not small. The
      driver's own state lives at `/nix/var/nix-csi` *inside the node
      container*, which is this path plus `nix/var/nix-csi` on the node.
      With `/` that is the guest's /nix -- an overlay, because a guest's
      whole /nix is one overlay mount over hostfs. NRI's read-write mode
      then asks the kernel for an overlay whose upperdir is on that
      overlay, and the kernel refuses:

          fsconfig('upperdir'=.../containers/<id>/upper): Invalid argument

      overlayfs does not accept an overlayfs as an upper layer. A real node
      has an ordinary directory here and never meets this, so the test was
      the only thing that did -- and it was hiding half of NRI.

      This directory is not the guest's store, so the init container fills
      it from the cache guest, as on a real node.
    */
    hostMountPath = "/nixkube";

    /*
      The run's cache guest and nothing else, for every role
      (`vivarium.recipes.cache` in ./default.nix). Each role's list has its
      own default of cache.nixos.org and a list concatenates, so the
      top-level `nixkube.nixConfig` cannot replace it; only `mkForce` on the
      role does.

      An unreachable substituter is worse than none: each miss against
      cachix or cache.nixos.org costs four HTTP retries and a DNS timeout
      against a resolver a build sandbox cannot reach, which reads as a
      hang. Measured, pynixd-0 spent 90s in Init that way. With the cache
      guest alone, a path the run did not list fails at once and names it:

          error: path '...-nodeEnv' is required, but there is no
          substituter that can build it
    */
    node.nixConfig.settings = {
      substituters = lib.mkForce [ cache.url ];
      trusted-public-keys = [ cache.trustedPublicKey ];
    };
    pynixd.controller.nixConfig.settings = {
      substituters = lib.mkForce [ cache.url ];
      trusted-public-keys = [ cache.trustedPublicKey ];
    };
    pynixd.builder.nixConfig.settings = {
      substituters = lib.mkForce [ cache.url ];
      trusted-public-keys = [ cache.trustedPublicKey ];
    };
  };
}
