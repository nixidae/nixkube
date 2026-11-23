# SPDX-License-Identifier: Apache-2.0

/*
  Central's build pod store, whose every pod reads its inputs from a nixkube farm
  (phases/buildpods.py). nixidae/nixidae#1560, spike.

  For each build, central adds one text path that refers to the build's inputs and to
  each store path this template names. The pod's `/nix` is the farm of that path's
  closure: pynixd, Nix, and the inputs. The pod's pynixd reads the farm as its lower
  store and copies nothing in from central.

  Test packaging: the pod runs from the test's busybox image, and the store takes only
  derivations that ask for `feature`.
*/
{
  config,
  pkgs,
  sources,
  ...
}:
let
  cfg = config.nixkube;
  pynixd = (import sources.pynixd { inherit pkgs sources; }).packages.pynixd;
  inherit (pkgs.stdenv.hostPlatform) system;
  feature = "nixkube-podfarm";
  uid = 1000;

  podConfig = pkgs.writeText "nixkube-build-pod-pynixd.json" (
    builtins.toJSON {
      stores.local = {
        type = "local-socket";
        store_path = "/build";
        managed = true;
        # A Nix daemon would not know the farm: pynixd answers every operation.
        daemon = false;
        pynixd_writes = true;
        lower_store = "/";
        op_routes.BuildDerivation = "pynixd";
        sibling_sandbox = "/build/sibling";
      };
      unix_path = "/build/pynixd.sock";
      ssh_port = null;
      gc_enabled = false;
      # Central relays the pod's log, and the phase reads `build_one_inputs` there, an info event.
      log_level = "INFO";
      ranking.min_schedule_score = -1.0e9;
      ranking.min_free_bytes = 0;
    }
  );

  mounts = [
    {
      name = "nix";
      mountPath = "/nix";
      readOnly = true;
    }
    {
      name = "build";
      mountPath = "/build";
    }
  ];
  unprivileged = {
    allowPrivilegeEscalation = false;
    capabilities.drop = [ "ALL" ];
  };

  template.pod.spec = {
    terminationGracePeriodSeconds = 5;
    # A build pod whose central went away ends here; its sandbox container serves for ever.
    activeDeadlineSeconds = 600;
    inherit (cfg.buildPods) priorityClassName;
    securityContext = {
      runAsUser = uid;
      runAsGroup = uid;
      runAsNonRoot = true;
      seccompProfile.type = "RuntimeDefault";
      # The kubelet gives this group read on the job Secret, which pynixd mounts 0400.
      fsGroup = uid;
    };
    containers = [
      {
        name = "pynixd";
        image = "vivarium.test/busybox:1";
        imagePullPolicy = "Never";
        command = [
          "${pynixd}/bin/pynixd"
          "build-one"
          "/run/pynixd-job"
        ];
        env = [
          {
            name = "PYNIXD_CONFIG";
            value = "${podConfig}";
          }
          {
            name = "NIX_CONFIG";
            value = ''
              experimental-features = nix-command
              build-users-group =
              substituters =
              require-drop-supplementary-groups = false
              system-features = ${feature}
            '';
          }
          {
            name = "PATH";
            value = "${pkgs.nix}/bin:${pkgs.coreutils}/bin";
          }
        ];
        securityContext = unprivileged;
        volumeMounts = mounts;
      }
      {
        name = "sandbox";
        image = "vivarium.test/busybox:1";
        imagePullPolicy = "Never";
        command = [
          "${pynixd.venv}/lib/venv/bin/python"
          "-I"
          "-m"
          "pynixd_store.builder.sibling"
          "run"
          "/build/sibling"
        ];
        # The build pods' profile allows the namespaces the sandbox makes; RuntimeDefault refuses them.
        securityContext = unprivileged // {
          seccompProfile = {
            type = "Localhost";
            localhostProfile = cfg.buildPods.seccompProfile;
          };
        };
        volumeMounts = mounts;
      }
    ];
    volumes = [
      # pynixd fills `volumeAttributes` for each build.
      {
        name = "nix";
        csi = {
          driver = "nixkube";
          readOnly = true;
        };
      }
      {
        name = "build";
        emptyDir = { };
      }
    ];
  };
in
{
  nixkube.pynixd.controller.settings = {
    stores.buildpods = {
      type = "kubernetes-build-pods";
      template = "farm";
      central_host = "pynixd.${cfg.namespace}.svc.cluster.local";
      central_port = 22;
      systems = [ system ];
      system_features = [ feature ];
      mandatory_features = [ feature ];
      farm.volume = "nix";
      priority_classes = [
        cfg.buildPods.priorityClassName
        cfg.buildPods.nestedPriorityClassName
      ];
    };
    # The pod's service account: no kubeconfig.
    provisioning.kubernetes = {
      inherit (cfg) namespace;
      templates.farm = template;
    };
  };
}
