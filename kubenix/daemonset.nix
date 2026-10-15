# SPDX-License-Identifier: Apache-2.0

{
  config,
  lib,
  csiPkgs,
  curPkgs,
  ekn,
  ...
}:
let
  packageRef = ekn.lib.packageRef;
  cfg = config.nixkube;
  nsRes = config.kubernetes.resources.${cfg.namespace};
in
{
  options.nixkube.node = {
    enable = (lib.mkEnableOption "node DaemonSet (CSI driver and NRI plugin)") // {
      default = true;
    };
    compat = (lib.mkEnableOption "nix.csi.store CSI driver (for backwards compatibility)") // {
      default = true;
      apply =
        value:
        if value then
          lib.warn "nixkube.node.compat: CSI compatibility driver (nix.csi.store) is enabled. This is deprecated and will be removed in a future release. Please migrate to the nixkube driver name." value
        else
          value;
    };
    tolerations = lib.mkOption {
      description = ''
        Taints the node DaemonSet tolerates, as a Kubernetes `tolerations`
        list. Empty by default, so the DaemonSet lands only where an ordinary
        workload would.

        This used to be an unconditional toleration of
        `node-role.kubernetes.io/control-plane:NoSchedule`, which is right on
        a cluster that runs workloads on its control plane -- where nixkube
        was developed -- and wrong as a default. Shipped that way it assumes
        every cluster does that, and on one that respects the taint it puts a
        nix-node pod where an ordinary workload would not go.

        Set it to restore the old behaviour where that is what you want:

            nixkube.node.tolerations = [
              {
                key = "node-role.kubernetes.io/control-plane";
                operator = "Exists";
                effect = "NoSchedule";
              }
            ];
      '';
      type = lib.types.listOf lib.types.attrs;
      default = [ ];
      example = lib.literalExpression ''
        [
          {
            key = "node-role.kubernetes.io/control-plane";
            operator = "Exists";
            effect = "NoSchedule";
          }
        ]
      '';
    };
    nixConfig = lib.mkOption {
      description = "nix.conf for CSI/mounter/DaemonSet pods";
      type = (import ./nixOptions.nix) {
        pkgs = curPkgs;
        nix = config.nixkube.nix.package;
      };
    };
  };
  config =
    let
      labels = cfg.labels // {
        "app.kubernetes.io/component" = "node";
      };
      matchLabels = cfg.matchLabels // {
        "app.kubernetes.io/component" = "node";
      };

      # Set by `nixkube-host-check` on a node whose host runs NixOS.
      hostLabel = "nixkube/host";

      # First in both pods when `hostStore` is on: on the wrong host it moves
      # the node to the other DaemonSet by its label, and waits to be deleted.
      hostCheck = store: {
        name = "host-check";
        image = "ghcr.io/nixidae/nixkube/nix:${cfg.version}-${curPkgs.nix.version}";
        inherit (cfg) imagePullPolicy;
        command = [
          "nixkube-host-check"
          "--mode"
          store
        ];
        env = lib.mkNamedList {
          KUBE_NODE_NAME.valueFrom.fieldRef.fieldPath = "spec.nodeName";
          HOST_ROOT.value = "/host";
        };
        volumeMounts = lib.mkNamedList {
          host-root = {
            mountPath = "/host";
            readOnly = true;
          };
        };
        resources.requests = {
          memory = "64Mi";
          cpu = "10m";
        };
      };

      /**
        One node DaemonSet. `store` is null for the single DaemonSet of a
        cluster without `hostStore`, whose render it leaves unchanged, and
        "separate" or "host" for the two with it: the one with nixkube's own
        store at `hostMountPath`, and the one that mounts the host's /nix.
        Each selects its own pods by `nixkube/store`, and its nodes by the
        `nixkube/host` label. Issue #25.
      */
      nodeDaemonSet =
        store:
        let
          storeLabel = lib.optionalAttrs (store != null) { "nixkube/store" = store; };
          labels' = labels // storeLabel;
          matchLabels' = matchLabels // storeLabel;
        in
        {
          metadata.labels = labels';
          metadata.annotations."nixkube/discard" = "true";
          spec = {
            updateStrategy = {
              type = "RollingUpdate";
              rollingUpdate.maxUnavailable = 1;
            };
            selector.matchLabels = matchLabels';
            template = {
              metadata.labels = labels';
              metadata.annotations = {
                "kubectl.kubernetes.io/default-container" = "nix-node";
                # `appstarter-init` fetches into `/nix-volume`, which is this
                # node's own store by hostPath -- the same store NRI's
                # `fetch_packages` targets. So injection here does
                # `appstarter init`'s work before it runs, without its retry
                # or its image fallback, and that fallback is the only thing
                # that rescues a node whose pynixd is down. Issues #27, #55.
                "nixkube/appstarter-init-exclude" = "true";
              }
              // lib.optionalAttrs (cfg.metrics.enable && cfg.metrics.annotations) {
                "prometheus.io/scrape" = "true";
                "prometheus.io/port" = toString cfg.metrics.port;
                "prometheus.io/path" = "/metrics";
              }
              // {
                configHash = lib.hashAttrs (
                  { } // nsRes.ConfigMap.nix-node or { } // nsRes.configMap.ssh-config or { }
                );
              };
              spec =
                lib.optionalAttrs (cfg.node.tolerations != [ ]) {
                  inherit (cfg.node) tolerations;
                }
                // lib.optionalAttrs (store != null) {
                  affinity.nodeAffinity.requiredDuringSchedulingIgnoredDuringExecution.nodeSelectorTerms = [
                    {
                      matchExpressions = [
                        {
                          key = hostLabel;
                          operator = if store == "host" then "In" else "NotIn";
                          values = [ "nixos" ];
                        }
                      ];
                    }
                  ];
                }
                // {
                  serviceAccountName = "nixkube";
                  priorityClassName = "system-node-critical";
                  initContainers = lib.mkNumberedList (
                    lib.optionalAttrs (store != null) { "0" = hostCheck store; }
                    // {
                      "1" = {
                        name = "appstarter-init";
                        image = "ghcr.io/nixidae/nixkube/nix:${cfg.version}-${curPkgs.nix.version}";
                        inherit (cfg) imagePullPolicy;
                        securityContext.privileged = true; # chroot store
                        command = [
                          "appstarter"
                          "init"
                        ];
                        env = lib.mkNamedList {
                          # One environment per enabled architecture, because
                          # one DaemonSet runs on all of them. `appstarter`
                          # picks its own.
                          APPSTARTER_WANTED.value = builtins.toJSON (
                            lib.mapAttrs (
                              system: sysPkgs: "${packageRef "nixkube-node-env-${system}" sysPkgs.nixkube-node-env}"
                            ) csiPkgs
                          );
                          # Which of the image's fallbacks to take when the
                          # fetch fails. The image carries one per role and
                          # names them itself -- a fallback named here comes out
                          # of this same evaluation, so it would be the path
                          # above and would fall back to nothing.
                          APPSTARTER_ROLE.value = "node";
                          # Without this `appstarter` cannot tell a pynixd that
                          # is off by choice from one that is down, and reports
                          # the same failure for both. See issue #27.
                          PYNIXD_ENABLED.value = lib.boolToString cfg.pynixd.enable;
                        };
                        volumeMounts = lib.mkNamedList {
                          nix-store.mountPath = "/nix-volume";
                          nix-config.mountPath = "/etc/nix";

                          ssh-config.mountPath = "/etc/ssh";
                          ssh-key.mountPath = "/etc/ssh-key";
                          ssh-dynauth.mountPath = "/etc/ssh-dynauth";
                        };
                        resources = {
                          requests = {
                            memory = "128Mi";
                            cpu = "100m";
                          };
                        };
                      };
                    }
                  );
                  containers = lib.mkNamedList {
                    nix-node = {
                      image = "ghcr.io/nixidae/nixkube/scratch:1.0.1";
                      # `appstarter run` execs nixkube, so tini's child stays
                      # the application and signals and reaping keep working.
                      # It also puts the two store paths in nixkube's
                      # environment, which is how a node that is behind says
                      # so. Issue #49.
                      command = [
                        "tini"
                        "--"
                        "appstarter"
                        "run"
                        "nixkube"
                      ];
                      securityContext.privileged = true;
                      # The port `cli.py` serves `/metrics` on. Declared so
                      # that `metrics.podMonitor` can select it by name, and
                      # named so the two move together. Issue #40.
                      ports = lib.mkIf cfg.metrics.enable (
                        lib.mkNamedList {
                          metrics.containerPort = cfg.metrics.port;
                        }
                      );

                      /*
                        Ask the livenessprobe sidecar whether the driver still
                        answers, and restart this container when it does not.

                        The sidecar was already here, calling Probe over
                        /csi/nixkube/csi.sock and serving the answer on 9808.
                        Nothing read it. So a driver that had become
                        unreachable stayed that way, and the only symptom was
                        pods stuck in ContainerCreating with no event to say
                        why -- kubelet cannot report a driver it cannot talk
                        to.

                        Measured: deleting /var/lib/kubelet/plugins/nixkube/
                        csi.sock left the DaemonSet 5/5 Running with 0
                        restarts, and a pod that wanted a volume waited 298
                        seconds without one thing changing. Unlinking the
                        path does not close the listening socket the driver
                        holds, so nothing inside the driver notices either.

                        failureThreshold x periodSeconds is 50s of real
                        unreachability before a restart, and
                        initialDelaySeconds keeps a slow start from counting.
                      */
                      livenessProbe = {
                        httpGet = {
                          path = "/healthz";
                          port = 9808;
                        };
                        initialDelaySeconds = 30;
                        periodSeconds = 10;
                        timeoutSeconds = 5;
                        failureThreshold = 5;
                      };

                      /*
                        Say so when the driver does not answer.

                        The livenessProbe above restarts the container, and it
                        works. It does not report anything. A container with no
                        readinessProbe is Ready as soon as it runs, so
                        `kubectl get pods` said 3/3 Running, ready=true, for
                        every one of the six minutes its driver was dead.

                        Measured on nixlab2: a directory left where
                        /var/lib/kubelet/plugins/nixkube/csi.sock belongs makes
                        the driver die at bind, every second. restartCount went
                        3 -> 6 over that break, and nothing else moved. The
                        node stayed Ready and kept attracting builders it could
                        not serve, and a builder Pod on it sat in Pending with
                        `connect: connection refused` on the socket.

                        Same target as the livenessProbe, faster to fire:
                        2 x 10s reports NotReady about 30 seconds before the
                        5 x 10s restart. So the restart, when it comes, has a
                        reason visible ahead of it.

                        No Service selects these pods -- both Services select
                        component=pynixd, and this is component=node -- so
                        NotReady removes nothing from any endpoint list. It
                        only makes the report true.
                      */
                      readinessProbe = {
                        httpGet = {
                          path = "/healthz";
                          port = 9808;
                        };
                        initialDelaySeconds = 10;
                        periodSeconds = 10;
                        timeoutSeconds = 5;
                        failureThreshold = 2;
                      };

                      env = lib.mkNamedList (
                        lib.optionalAttrs (store != null) {
                          HOST_STORE.value = lib.boolToString (store == "host");
                        }
                        // {
                          PYNIXD_ENABLED.value = lib.boolToString cfg.pynixd.enable;
                          ENABLE_COMPAT_DRIVER.value = lib.boolToString cfg.node.compat;
                          NRI_ENABLED.value = "true";
                          NRI_VM_RUNTIME_HANDLERS.value = lib.concatStringsSep "," cfg.nri.vmRuntimeHandlers;
                          HOME.value = "/nix/var/nix-csi/root";
                          HOST_MOUNT_PATH.value = cfg.hostMountPath;
                          KUBE_NAMESPACE.valueFrom.fieldRef.fieldPath = "metadata.namespace";
                          KUBE_NODE_NAME.valueFrom.fieldRef.fieldPath = "spec.nodeName";
                          KUBE_POD_IP.valueFrom.fieldRef.fieldPath = "status.podIP";
                          KUBE_POD_NAME.valueFrom.fieldRef.fieldPath = "metadata.name";
                          KUBE_POD_UID.valueFrom.fieldRef.fieldPath = "metadata.uid";
                          NIX_BUILD_TIMEOUT.value = toString cfg.nodeBuildTimeout;
                          VERIFY_STORE_PATHS.value = lib.boolToString cfg.verifyStorePaths;
                          NIXOS_HOST_ENABLED.value = lib.boolToString cfg.nixosHost.enable;
                          CSI_COMPOSEFS.value = lib.boolToString cfg.csi.composefs;
                          NRI_COMPOSEFS.value = lib.boolToString cfg.nri.composefs;
                          METRICS_ENABLED.value = lib.boolToString cfg.metrics.enable;
                          METRICS_PORT.value = toString cfg.metrics.port;
                          NIXPKGS_ALLOW_UNFREE.value = "1";
                          USER.value = "root";
                        }
                      );
                      volumeMounts = lib.mkNamedList {
                        csi-socket.mountPath = "/csi";
                        nix-config.mountPath = "/etc/nix";
                        nri-socket.mountPath = "/var/run/nri";
                        registration.mountPath = "/registration";
                        host-root = {
                          mountPath = "/host";
                        };
                        kubelet = {
                          mountPath = "/var/lib/kubelet";
                          mountPropagation = "Bidirectional";
                        };
                        /*
                          A plain mount of <hostMountPath>/nix, and not `subPath =
                          "nix"` on the parent volume.

                          The kubelet performs a subPath bind itself, in its own
                          mount namespace. A distribution that runs the kubelet in
                          a container does not have <hostMountPath> in that
                          namespace, so the kubelet binds an empty directory over
                          /nix and every store path is missing. The container then
                          cannot exec out of /nix/var/result/bin, and the error
                          names $PATH rather than the store.

                          Measured on Talos v1.13.9, one pod, one hostPath volume,
                          mounted twice:

                            /vol  (plain)           dev 253:5 xfs   nix/store = 242 paths
                            /nix  (subPath: nix)    dev 0:62 overlay   empty

                          and directly, through /proc:

                            host pid 1  mnt:[4026531832]  .../nix/store -> 242 entries
                            kubelet     mnt:[4026532779]  .../nix/store ->   0 entries

                          A plain hostPath mount is handed to the runtime as a
                          path instead, and resolves against the host tree. That
                          is also why appstarter-init sees a populated store
                          while nix-node does not: appstarter-init writes
                          through the plain mount at /nix-volume.

                          kubeadm runs its kubelet in the host mount namespace, so
                          no test with a kubeadm control plane can see this.
                          See issue #16.
                        */
                        nix-root = {
                          mountPath = "/nix";
                          mountPropagation = "Bidirectional";
                        };

                        ssh-config.mountPath = "/etc/ssh";
                        ssh-dynauth.mountPath = "/etc/ssh-dynauth";
                        ssh-key.mountPath = "/etc/ssh-key";
                        nix-key.mountPath = "/etc/nix-key";
                      };
                      resources = {
                        requests = {
                          memory = "128Mi";
                          cpu = "100m";
                        };
                      };
                    };
                    csi-node-driver-registrar-nix-csi = lib.mkIf cfg.node.compat {
                      image = "registry.k8s.io/sig-storage/csi-node-driver-registrar:v2.16.0";
                      args = [
                        "--v=5"
                        "--csi-address=/csi/nix.csi.store/csi.sock"
                        "--kubelet-registration-path=/var/lib/kubelet/plugins/nix.csi.store/csi.sock"
                      ];
                      env = lib.mkNamedList {
                        KUBE_NODE_NAME.valueFrom.fieldRef.fieldPath = "spec.nodeName";
                      };
                      volumeMounts = lib.mkNamedList {
                        csi-socket.mountPath = "/csi";
                        kubelet.mountPath = "/var/lib/kubelet";
                        registration.mountPath = "/registration";
                      };
                      resources = {
                        requests = {
                          memory = "10Mi";
                          cpu = "10m";
                        };
                      };
                    };
                    livenessprobe-nix-csi = lib.mkIf cfg.node.compat {
                      image = "registry.k8s.io/sig-storage/livenessprobe:v2.18.0";
                      args = [
                        "--csi-address=/csi/nix.csi.store/csi.sock"
                        "--health-port=9809"
                      ];
                      volumeMounts = lib.mkNamedList {
                        csi-socket.mountPath = "/csi";
                        registration.mountPath = "/registration";
                      };
                      resources = {
                        requests = {
                          memory = "10Mi";
                          cpu = "10m";
                        };
                      };
                    };
                    csi-node-driver-registrar-nixkube = {
                      image = "registry.k8s.io/sig-storage/csi-node-driver-registrar:v2.16.0";
                      args = [
                        "--v=5"
                        "--csi-address=/csi/nixkube/csi.sock"
                        "--kubelet-registration-path=/var/lib/kubelet/plugins/nixkube/csi.sock"
                      ];
                      env = lib.mkNamedList {
                        KUBE_NODE_NAME.valueFrom.fieldRef.fieldPath = "spec.nodeName";
                      };
                      volumeMounts = lib.mkNamedList {
                        csi-socket.mountPath = "/csi";
                        kubelet.mountPath = "/var/lib/kubelet";
                        registration.mountPath = "/registration";
                      };
                      resources = {
                        requests = {
                          memory = "10Mi";
                          cpu = "10m";
                        };
                      };
                    };
                    livenessprobe-nixkube = {
                      image = "registry.k8s.io/sig-storage/livenessprobe:v2.18.0";
                      args = [
                        "--csi-address=/csi/nixkube/csi.sock"
                        "--health-port=9808"
                      ];
                      # The port nix-node's livenessProbe reads. Named so the
                      # two stay together when somebody moves one.
                      ports = lib.mkNamedList {
                        healthz.containerPort = 9808;
                      };
                      volumeMounts = lib.mkNamedList {
                        csi-socket.mountPath = "/csi";
                        registration.mountPath = "/registration";
                      };
                      resources = {
                        requests = {
                          memory = "10Mi";
                          cpu = "10m";
                        };
                      };
                    };
                  };
                  volumes = lib.mkNamedList {
                    nix-config.configMap.name = "nix-node";
                    registration.hostPath.path = "/var/lib/kubelet/plugins_registry";
                    # The host's own / in host mode: appstarter-init's chroot
                    # store at /nix-volume is then the host's /nix.
                    nix-store.hostPath =
                      if store == "host" then
                        {
                          path = "/";
                          type = "Directory";
                        }
                      else
                        {
                          path = cfg.hostMountPath;
                          type = "DirectoryOrCreate";
                        };
                    # The store root, as its own volume, so nix-node mounts it
                    # without subPath. See the nix-root volumeMount above.
                    #
                    # DirectoryOrCreate is required, not a convenience: the
                    # kubelet checks hostPath type when it sets up pod volumes,
                    # which happens before appstarter-init runs. `Directory` would fail
                    # on a node that has no store yet.
                    nix-root.hostPath =
                      if store == "host" then
                        {
                          path = "/nix";
                          type = "Directory";
                        }
                      else
                        {
                          path = "${cfg.hostMountPath}/nix";
                          type = "DirectoryOrCreate";
                        };
                    csi-socket.hostPath = {
                      path = "/var/lib/kubelet/plugins/";
                      type = "DirectoryOrCreate";
                    };
                    nri-socket.hostPath = {
                      path = "/var/run/nri";
                      type = "DirectoryOrCreate";
                    };
                    host-root.hostPath = {
                      path = "/";
                      type = "Directory";
                    };
                    kubelet.hostPath = {
                      path = "/var/lib/kubelet";
                      type = "Directory";
                    };

                    ssh-config.configMap = {
                      name = "ssh-config";
                      defaultMode = 292; # 444
                    };
                    ssh-dynauth.configMap = {
                      name = "ssh-dynauth";
                      defaultMode = 292; # 444
                    };
                    ssh-key.secret = {
                      secretName = "ssh-key";
                      defaultMode = 256; # 400
                    };
                    nix-key.secret = {
                      secretName = "nix-key";
                      defaultMode = 256; # 400
                    };
                  };
                };
            };
          };
        };
    in
    lib.mkIf (cfg.enable && cfg.node.enable) {
      kubernetes.resources.${cfg.namespace} = {
        # **The operator's own kind, and the checked path.** An operator
        # ignores the `prometheus.io/*` annotations on the pod template and
        # selects pods by this object instead. A VictoriaMetrics operator
        # converts it into a VMPodScrape, so one object serves both, and this
        # kind has a real schema so a render is validated against an
        # apiserver. `metrics.podMonitor` says why not a VM-native CR, and
        # what start-up order makes an unconverted PodMonitor look like an
        # unscraped one. Issue #40.
        PodMonitor = lib.mkIf (cfg.metrics.enable && cfg.metrics.podMonitor) {
          nixkube = {
            metadata.labels = labels;
            spec = {
              selector.matchLabels = matchLabels;
              podMetricsEndpoints = [
                {
                  # By name, not by number: the container declares the port
                  # under this name, so the two move together.
                  port = "metrics";
                  path = "/metrics";
                }
              ];
            };
          };
        };
        DaemonSet =
          if cfg.hostStore.enable then
            {
              nix-node-separate = nodeDaemonSet "separate";
              nix-node-host = nodeDaemonSet "host";
            }
          else
            { nix-node = nodeDaemonSet null; };
      };
    };
}
