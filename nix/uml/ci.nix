# SPDX-License-Identifier: Apache-2.0

# What the kind jobs do, on a guest instead of on a container.
#
#     nix run --file . internal.ciTest.driver -- --out ./o        # test-kind-nocache
#     nix run --file . internal.ciTestCache.driver -- --out ./o   # test-kind-cache
#
# `nix/uml/default.nix` is the other cluster test and asks a different
# question: it renders the manifest, applies it, and then breaks the node
# nine ways to see whether the driver comes back.  It also runs on a node
# this repository's harness built specially -- every image nix-built and
# imported, the store mounted into every static pod.
#
# This one is the kind jobs, ported.  The node pulls its images from
# registry.k8s.io like any other node, nothing is patched, and the
# deployment goes through `kubenixDeploy` -- the same script CI runs, doing
# the same kluctl deploy -- rather than through a manifest this test
# rendered itself.  That last part is most of the point: the deployment
# script is a thing that can break, and nothing tested it before.
#
# **kluctl is deprecated.** It is still what CI uses, so this ports it
# faithfully rather than getting ahead of the replacement.
#
# kluctl runs on the *host*, against the guest's API server through a passt
# forward, which is where kind runs it too -- on the runner, not inside a
# node.
#
# **Outside the build sandbox only.** The node pulls its images from
# registry.k8s.io, so there is nothing to run here without a network. Store
# paths are the exception: the run's cache guest serves them.
{
  pkgs,
  lib,
  sources,
  instance,
  workloads,
  # Which jobs the deployment creates, and which of them are asserted.
  # Both lists come from ci/workflows/ci.nix so the two cannot drift.
  deployedJobs,
  assertedJobs,
  rejectedJobs,
  # nixkube's own image, as a docker-archive tarball under the tag a pod
  # asks for. ./images.nix builds it for the other cluster test.
  umlImages,
  name,
  # The guest's own segment.  Two of these tests can run at once, so each
  # needs a network of its own -- see ./default.nix.
  lan,
  # Remove /etc/NIXOS before the deploy, so that a host-store deployment
  # takes this NixOS guest for any other distribution.
  foreignHost ? false,
}:
let
  vivariumSet = import sources.vivarium { inherit pkgs sources; };
  vivarium = vivariumSet.lib;

  # Whether this deployment brings the pynixd cache with it, which is the
  # difference between the two kind jobs.  Asked of the instance rather
  # than passed in, so the two cannot disagree.
  pynixd = instance.config.nixkube.pynixd.enable;
  hostStore = instance.config.nixkube.hostStore.enable;
in
vivarium.mkTest {
  inherit name;

  /*
    Every store path the node is asked for, from a guest of the run and
    from nowhere else: what was pushed to cachix before this test could
    run, and what a fork could never push.

    Two manifests, and each closure carries what its pods name. The CI
    deployment's, because ./ci-guest.nix keeps the string context on it.
    The workloads', because `kubenixCITest` sets `nixkube.enable = false`,
    so the discard transformer never runs on it -- `env-ssl`'s `printer`,
    built on whatever machine runs this, is in there.
  */
  vivarium.recipes.cache = {
    enable = true;
    roots =
      let
        pin = import ../../kubenix/ci/nixpkgs-pin.nix;
        pinned = pin.import' { system = pkgs.stdenv.hostPlatform.system; };
        unfree = pin.import' {
          system = pkgs.stdenv.hostPlatform.system;
          config.allowUnfree = true;
        };
      in
      map toString [
        instance.config.internal.manifestJSONFile
        workloads.config.internal.manifestJSONFile
        # What `flake-hello` substitutes, and what `expr-hello` builds from.
        pinned.hello
        unfree.hello-unfree.inputDerivation
      ];
  };
  phases.test = {
    script = ./ci.py;
    after = [ "boot" ];
  };
  # The node test's helpers, and kr8s for them: they reach the API server
  # from the host, through `<out>/access/kubeconfig`. The fork: it honours
  # that file's `tls-server-name`, which names the API server behind the
  # host's forward.
  pythonPath = [
    ./helpers
  ]
  ++ map (module: "${module}/${pkgs.python3.sitePackages}") (
    pkgs.python3.pkgs.requiredPythonModules [
      (pkgs.python3Packages.callPackage "${sources.nanopynix}/nix/kr8s.nix" { src = sources.kr8s; })
    ]
  );
  backend = "qemu";
  vivarium.recipes.kubernetes.enable = true;

  nodes.cp =
    { config, ... }:
    {
      services.vivarium-k8s = {
        enable = true;
        role = "control-plane";

        /*
          A node like any other node.

          `images = "nix"`, which is the default and what the other
          cluster tests use, builds every image from nixpkgs and mounts
          `/nix/store` into the static pods to make them resolve. That is
          what lets a cluster run inside a build sandbox -- and it is
          exactly wrong here, because nixkube's whole job is to put a
          store into a pod. On such a node it cannot be told apart from
          the node doing it, and this test would pass with its subject
          switched off.
        */
        images = "pull";

        /*
          Except nixkube's own, which is the thing under test.

          Everything else a node needs still comes off the network, which is
          the point of `pull` above. This one image does not: a pod names
          the content tag of this checkout's image, and pulling it would
          need CI to have published that image first. A change to
          `pkgs/appstarter` could not be tried
          here at all -- and one that could never work shipped because of
          it, `store.copy` calling `nix copy` with no `--from`.

          The tarball carries that exact tag, so containerd already has the
          name kubelet looks up. `nixkube.imagePullPolicy` is what stops
          kubelet fetching the published one over the top of it; see
          ./ci-guest.nix.
        */
        extraImages = [ umlImages.nix ];

        # The DaemonSet asks containerd for an NRI connection whether or
        # not containerd is listening, and gets no error when it is not.
        nri = true;

        /*
          Somewhere for pynixd's claim to land.

          `pynixd` is a StatefulSet with a `volumeClaimTemplate` and no
          `storageClassName`, so it binds against the cluster's default
          StorageClass or it stays Pending forever. kind has one and a
          kubeadm node has none, which is the one thing this test needs
          that the kind job got for free.

          One volume, because there is one claim.
        */
        persistentVolumes = if pynixd then 1 else 0;
      };

      /*
        With the host store, the guest's own daemon substitutes from
        nowhere, so every path a pod gets must come through nixkube's
        daemon and nixkube's substituters. On sg1 (2026-11-19) the host's
        daemon fetched for nixkube, could not reach nixkube's cache, and no
        new pod started; here both named the same cache, which hid it.
        Above `mkForce`, which the cache recipe uses.
      */
      nix.settings.substituters = lib.mkIf (hostStore && !foreignHost) (lib.mkOverride 40 [ ]);

      vivarium = {
        memory = "14336M";
        cpus = 4;
        # Measured at the end of a `ciTestCache` run, which is the heavier
        # of the two: 3.8G of 7.8G used, and pynixd's volume 8.3M of that.
        # The test prints `df` for this reason -- raise it from a
        # measurement, not from a guess.
        #
        # **This number is also the PV's capacity**, because a hostPath
        # volume is a directory on this disk and nothing else bounds it. So
        # a claim larger than it can never bind -- see the storage size in
        # ./ci-guest.nix, which is the end that gives.
        diskSize = 8192;
        /*
          The API server, reachable from the host, because kluctl runs
          there. On any free host port, which `cp.reachable(6443)` names:
          a developer machine that runs Kubernetes itself has `*:6443`
          bound already.
        */
        inbound = [
          { guest = config.vivarium.sshPort; }
          { guest = 6443; }
        ];
        inherit lan;
      };

    };

  settings = {
    deploy = "${instance.deploymentScript}";
    deployWorkloads = "${workloads.deploymentScript}";
    inherit
      deployedJobs
      assertedJobs
      rejectedJobs
      pynixd
      hostStore
      foreignHost
      ;
  };
}
