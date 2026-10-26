# SPDX-License-Identifier: Apache-2.0

# What a workload asks nixkube for, and how the test knows it got it.
#
# kubenix/ci carries workloads already, and most of them cannot run here:
# `flake-hello` fetches from GitHub, `expr-hello` builds from nixpkgs, and a
# build sandbox has no network. These are the offline half, written out
# rather than filtered, so each one says what it proves.
#
# Every job here reaches its binary through the path the pod asked the driver
# for, and nowhere else. That is not decoration: the node hands out no /nix at
# all of its own accord -- kubeadm patches give the control plane its store
# and nothing else gets one -- so a container whose command is a store path
# cannot start unless nixkube put the store there.
{
  config,
  curPkgs,
  lib,
  ...
}:
let
  cfg = config.nixkube;

  # Not /nix, which is where the NRI plugin puts its own mount. Keeping the
  # volume somewhere else is what lets the test say which of the two put a
  # file where it found it.
  #
  # ./probe.nix and ./default.nix's `settings` repeat this literal. Three
  # places, because the first two are separate evaluations and the third is
  # what the test says to `kubectl exec`.
  mountPath = "/mnt/csi";

  system = curPkgs.stdenv.hostPlatform.system;

  labels = cfg.labels // {
    "app.kubernetes.io/component" = "uml-test";
  };

  # Its own component, so the wait for the jobs above does not also wait for
  # a Deployment that never finishes.
  residentLabels = cfg.labels // {
    "app.kubernetes.io/component" = "uml-resident";
  };

  # A CSI ephemeral volume holding one store path, and nothing else.
  storeVolume = lib.mkNamedList {
    store.csi = {
      driver = "nixkube";
      readOnly = true;
      volumeAttributes.${system} = "${curPkgs.hello}";
    };
  };

  # `hello`, reached through the volume: /mnt/csi/nix/store/...-hello/bin/hello.
  # Only the driver can put a file at that path.
  helloThroughVolume = lib.mkNamedList {
    hello = {
      image = "ghcr.io/nixidae/nixkube/scratch:1.0.1";
      command = [ "${mountPath}${lib.getExe curPkgs.hello}" ];
      volumeMounts = lib.mkNamedList {
        store.mountPath = mountPath;
      };
    };
  };

  job = spec: {
    metadata.labels = labels;
    spec = {
      # No retries. A workload that needs a second attempt here has found a
      # bug, and six of them only hide it behind five more minutes.
      backoffLimit = 0;
      template.spec = {
        restartPolicy = "Never";
      }
      // spec;
    };
  };
in
{
  config.kubernetes.resources.${cfg.namespace} = {
    # One pod, one store path, mounted where the pod asked.
    Job.csi-path = job {
      containers = helloThroughVolume;
      volumes = storeVolume;
    };

    # The same store path, asked for again by a second pod at the same time.
    # A driver that mounts per volume and unmounts per volume passes this; one
    # that shares a mount and unmounts it on the first delete does not.
    Job.csi-shared = job {
      containers = helloThroughVolume;
      volumes = storeVolume;
    };

    /*
      Something for the chaos scenarios to disturb.

      Every job above starts, prints and exits, so by the time anything
      breaks the driver there is nothing left running. This stays up,
      holding a CSI volume and an NRI mount, and the question after each
      scenario is whether it still has them -- a pod that loses its /nix
      while running is a different failure from one that cannot start.

      `sleep` is a store path, which is also what makes the NRI plugin act
      on this pod. So one pod covers both.
    */
    Deployment.resident = {
      metadata.labels = residentLabels;
      spec = {
        replicas = 1;
        selector.matchLabels = residentLabels;
        template = {
          metadata.labels = residentLabels;
          spec = {
            # `sleep` as PID 1 ignores SIGTERM, so a delete waited out the
            # whole default grace period: 31 s of the chaos phase.
            terminationGracePeriodSeconds = 1;
            containers = lib.mkNamedList {
              resident = {
                image = "ghcr.io/nixidae/nixkube/scratch:1.0.1";
                command = [
                  "${curPkgs.coreutils}/bin/sleep"
                  "infinity"
                ];
                volumeMounts = lib.mkNamedList {
                  store.mountPath = mountPath;
                };
              };
            };
            volumes = storeVolume;
          };
        };
      };
    };
  };
}
