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
  readConfigurationFile = import ./readConfigurationFile.nix;
  cfg = config.nixkube;
  nsRes = config.kubernetes.resources.${cfg.namespace};

  # Shared across pynixd central and builders
  image = cfg.images.scratch;

  # What a builder mounts through the node's CSI driver. The central pod
  # fills its own PVC from an initContainer instead -- a builder is created
  # by a pynixd that is already serving, so the node has this path by then.
  storeVolumeAttributes = lib.mapAttrs (
    system: pkgs: "${packageRef "nixkube-pynixd-env-${system}" pkgs.nixkube-pynixd-env}"
  ) csiPkgs;

  # The controller and a builder answer the same way and take the same kind of
  # push, so one shape for both. See `nixkube.pynixd.probes` for why the
  # kubelet's own defaults are not enough. Issue #37.
  probes =
    let
      p = cfg.pynixd.probes;
      /*
        `httpGet /healthz`, not a TCP dial.

        A `tcpSocket` probe calls `connect()`, and the kernel completes that
        from the listen backlog whether or not pynixd ever accepts. So it
        passes against a pynixd that has stopped serving entirely, and fails
        only when a stall outlasts `timeoutSeconds` -- which a pynixd that is
        merely busy also does. It carries no information in either direction.
        Issue #53, and #37 before it.

        `/healthz` has to be answered by the event loop, so it cannot pass
        while the loop is stalled, and pynixd checks its own interfaces and
        loop lag behind it. A failure names the check in the response body.
      */
      http = {
        httpGet = {
          path = "/healthz";
          port = "http";
        };
        inherit (p) timeoutSeconds periodSeconds;
      };
    in
    {
      readinessProbe = http // {
        inherit (p) failureThreshold;
      };
      livenessProbe = http // {
        inherit (p) failureThreshold;
      };
      # Liveness and readiness do not run until this passes, so a cold pynixd
      # restoring its store is not killed part way through.
      startupProbe = http // {
        failureThreshold = p.startupFailureThreshold;
      };
    };

  # The port pynixd's aiohttp server answers on: `/healthz`, `/metrics` and
  # the binary cache. One binding, because the probes, the container port and
  # the scrape discovery all have to name the same number.
  pynixdHttpPort = 8080;

  pynixdLabels = cfg.labels // {
    "app.kubernetes.io/component" = "pynixd";
  };
  pynixdMatchLabels = cfg.matchLabels // {
    "app.kubernetes.io/component" = "pynixd";
  };
  builderLabels = cfg.labels // {
    "app.kubernetes.io/component" = "builder";
  };

  # Only enabled systems
  enabledSystems = lib.filterAttrs (_: v: v) cfg.systems;

  pynixdSettings = lib.mkOption {
    description = ''
      Pynixd configuration as a JSON object. Merged into the PYNIXD_CONFIG
      config file mounted in the pynixd pod. Corresponds to the PynixdSettings
      pydantic model (see pynixd.config).

      Common keys include stores (dict of StoreSpec keyed by store ID),
      ranking weights, GC intervals, etc. When stores include SSH stores,
      their client keys are auto-discovered from HOME/.ssh/ if client_keys
      is omitted.
    '';
    inherit (jsonFormat) type;
    default = { };
    example = lib.literalExpression ''
      {
        stores = {
          builder1 = {
            type = "ssh-subprocess";
            host = "builder.example.com";
            port = 22;
            username = "nix";
            systems = [ "x86_64-linux" ];
          };
        };
      }
    '';
  };

  jsonFormat = curPkgs.formats.json { };
in
{
  options.nixkube.pynixd = {
    enable =
      (lib.mkEnableOption "pynixd StatefulSet (shared Nix binary cache and build distributor)")
      // {
        default = true;
      };
    settings = pynixdSettings;

    controller = {
      settings = pynixdSettings;
      nixConfig = lib.mkOption {
        description = "nix.conf for pynixd pod";
        type = (import ./nixOptions.nix) {
          pkgs = curPkgs;
          nix = config.nixkube.nix.package;
        };
      };
    };
    adminKeys = lib.mkOption {
      description = ''
        SSH public keys that connect to the cache as trusted clients, what
        Nix's `trusted-users` grants. A deploy that pushes paths it built
        itself needs one: an untrusted client may only add signed paths.
        Keys in `authorizedKeys` stay untrusted.
      '';
      type = lib.types.listOf (lib.types.either lib.types.str lib.types.path);
      apply = lib.map (v: lib.trim (if lib.typeOf v == "path" then builtins.readFile v else v));
      default = [ ];
    };
    trustClusterKey = lib.mkOption {
      description = ''
        Trust the cluster's own SSH key on the cache, as `adminKeys` does.
        Every node, the controller and the builders hold that key, so this
        trusts all of them.
      '';
      type = lib.types.bool;
      default = false;
    };
    authorizedKeys = lib.mkOption {
      description = "SSH public keys that can connect to cache, untrusted. Used by nodes to push built store paths to the cache.";
      type = lib.types.listOf (lib.types.either lib.types.str lib.types.path);
      apply = lib.map (v: lib.trim (if lib.typeOf v == "path" then builtins.readFile v else v));
      default = [ ];
      example = lib.literalExpression ''
        [
          "ssh-ed25519 AAAA... user@host"
          ./keys/deploy.pub
        ]
      '';
    };
    probes = {
      timeoutSeconds = lib.mkOption {
        description = ''
          How long the kubelet waits for one `/healthz` request.

          **The kubelet's own default is 1 second, and that is not enough.**
          A pynixd busy ingesting a multi-hundred-megabyte store transfer does
          not answer inside a second, so the liveness probe fails, the kubelet
          kills the container, and the push dies with it. Measured twice on one
          cluster while pushing a 186 MiB path: `Liveness probe failed: dial
          tcp ...: i/o timeout`, then `exitCode: 143`.

          The push does not report a probe failure. It reports `Nix daemon
          disconnected unexpectedly`, which sends the investigation towards
          the network instead. Issue #37.

          This bounds how long the kubelet waits. What counts as too long a
          stall is pynixd's own `health_loop_lag_max`, 5 seconds by default and
          settable through `nixkube.pynixd.settings`. Set it from
          `pynixd_event_loop_lag_seconds` on `/metrics`, not from a guess.
        '';
        type = lib.types.ints.positive;
        default = 10;
      };
      periodSeconds = lib.mkOption {
        description = "How often the kubelet probes.";
        type = lib.types.ints.positive;
        default = 10;
      };
      failureThreshold = lib.mkOption {
        description = ''
          Failed probes in a row before the kubelet acts. With the defaults
          here that is 60 seconds of no answer, against the 3 seconds the
          kubelet's own defaults give.
        '';
        type = lib.types.ints.positive;
        default = 6;
      };
      startupFailureThreshold = lib.mkOption {
        description = ''
          The same, for the startup probe. Liveness and readiness do not run
          until the startup probe passes, so this is how long a cold pynixd
          may take to restore its store before anything kills it. With the
          default period that is ten minutes.
        '';
        type = lib.types.ints.positive;
        default = 60;
      };
    };
    storageClassName = lib.mkOption {
      description = "StorageClass for the pynixd PVC. null uses the cluster's default StorageClass.";
      type = lib.types.nullOr lib.types.str;
      default = null;
      example = "fast-ssd";
    };
    storageSize = lib.mkOption {
      description = ''
        Size of the pynixd PVC.

        It holds the store pynixd serves *and* the one it boots from --
        `appstarter init` fills this claim, and `cacheEnv` alone is about
        512 MiB. The default leaves room for what a cluster then pushes into
        it.

        Lower it for a test. A single-node guest with a 4 GiB disk cannot
        honour the default, and a claim that large against a small disk only
        looks satisfied because a `hostPath` volume enforces nothing.

        **volumeClaimTemplates are immutable after creation.** Changing this
        on a live StatefulSet needs the StatefulSet deleted with
        `--cascade=orphan` and the PVC resized by hand.
      '';
      type = lib.types.str;
      default = "10Gi";
      example = "1Gi";
    };
    loadBalancerPort = lib.mkOption {
      description = ''
        External SSH port for the pynixd LoadBalancer Service.
        Set to null to disable the LoadBalancer (cluster-internal access only).
      '';
      type = lib.types.nullOr lib.types.int;
      default = 2222;
    };
    builder = {
      settings = pynixdSettings;
      nixConfig = lib.mkOption {
        description = "nix.conf for builder pods";
        type = (import ./nixOptions.nix) {
          pkgs = curPkgs;
          nix = config.nixkube.nix.package;
        };
      };
    };
    extraVolumes = lib.mkOption {
      description = ''
        Extra Kubernetes volumes keyed by name. Merged into the
        StatefulSet pod spec volumes. Useful for mounting Secrets
        containing SSH client keys for external stores.
      '';
      type = lib.types.attrsOf jsonFormat.type;
      default = { };
      example = lib.literalExpression ''
        {
          my-builder-key.secret.secretName = "my-builder-key";
        }
      '';
    };
    extraVolumeMounts = lib.mkOption {
      description = ''
        Extra volume mounts keyed by name. Merged into the pynixd
        container volumeMounts. Mount external SSH client keys into
        HOME/.ssh/ for asyncssh auto-discovery.
      '';
      type = lib.types.attrsOf jsonFormat.type;
      default = { };
      example = lib.literalExpression ''
        {
          my-builder-key.mountPath = "/nix/var/nix-csi/root/.ssh/id_ed25519";
        }
      '';
    };
  };
  config = lib.mkIf (cfg.enable && cfg.pynixd.enable) {
    # shared settings -> controller settings
    #
    # The defaults here are mkOptionDefault, and the shared settings pass
    # through with whatever priority the consumer gave them.
    #
    # Both sides used to be wrapped in mkDefault, which made
    # `nixkube.pynixd.settings` inert for exactly the keys that have a default:
    # two mkDefault definitions of builder-max conflict rather than one winning,
    # and mkForce did not help either, because mapAttrsRecursive re-wrapped it.
    # So an operator could not cap or disable builders at all, and the option
    # that looks like the way to do it failed with a priority conflict.
    nixkube.pynixd.controller.settings = lib.mkMerge [
      (lib.mapAttrsRecursive (n: v: lib.mkOptionDefault v) {
        builder-max = 3;
        builder-min = 1;
        idle-timeout = 300;
        # A builder that never becomes Ready is deleted after this many
        # seconds, and counts as a failure.
        #
        # Not activeDeadlineSeconds on the Job. That field measures a Job's
        # whole life, and a healthy builder Job runs for hours -- one measured
        # at 3h36m on a live cluster. Any value small enough to bound a Pod
        # that hangs before it starts would kill working builders mid-build.
        # The bound is in the manager, which knows whether a builder has ever
        # answered.
        #
        # 10 minutes. A warm node reaches Ready in 20 to 30 seconds, so this
        # leaves room for a cold image pull on a slow node.
        builder-startup-timeout = 600;
        # Longest delay between retries after builders of one system fail one
        # after another. The delay doubles from 30 seconds up to this, and a
        # builder that reaches Ready clears it.
        builder-backoff-cap = 600;
        # A builder older than this many seconds takes no new builds once its
        # replacement is Ready, and its Job is deleted when it has none in
        # flight. 0 keeps builders for ever.
        #
        # 6 hours, well under the 12 hours at which kube-prometheus'
        # KubeJobNotCompleted fires, so a build that is running at 6 hours
        # has 6 more to finish.
        builder-max-age = 21600;
        # Listen on every interface, and carried in config.json rather than in
        # an env var.
        #
        # `EnvVar.Value` is `json:"value,omitempty"`, so the apiserver drops an
        # empty string: the object renders with `"value": ""` and comes back
        # without it. The manifest then states a value no object ever holds.
        #
        # Not because it causes GitOps drift. That was reported and then
        # disproved on the same cluster -- ArgoCD calls another Deployment with
        # the same shape Synced.
        #
        # Not fixed by removing the setting. pynixd defaults `ssh_host` to
        # 127.0.0.1, so an absent value binds loopback and nothing outside the
        # Pod can reach it. `""` is what asyncssh takes for every interface,
        # v4 and v6, which "0.0.0.0" would not give.
        #
        # PynixdSettings reads env first and this file second, so moving the
        # value here keeps the behaviour and drops the phantom field. A string
        # inside a ConfigMap is not an EnvVar and survives.
        ssh_host = "";
        # The same value for the HTTP server, and stated rather than left to
        # the image's default: the probes are `httpGet` on this port, and an
        # IPv4-only bind fails every one of them on a single-stack IPv6
        # cluster. Stating it here fixes a running pod on a render alone.
        http_host = "";
        # pynixd admits only the keys these files list, and reads them at
        # each authentication, so a ConfigMap update applies with no restart.
        # nixidae/nixidae#769.
        ssh_user_keys = [
          "/etc/ssh/authorized_keys"
        ]
        ++ lib.optional (!cfg.pynixd.trustClusterKey) "/etc/ssh-dynauth/authorized_keys";
        ssh_admin_keys = [
          "/etc/ssh/admin_keys"
        ]
        ++ lib.optional cfg.pynixd.trustClusterKey "/etc/ssh-dynauth/authorized_keys";
      })
      config.nixkube.pynixd.settings
    ];
    # shared settings -> builder settings
    nixkube.pynixd.builder.settings = lib.mkMerge [
      (lib.mapAttrsRecursive (n: v: lib.mkDefault v) {
        # builder-specific JSON defaults go here (e.g., schedule-mode)
        #
        # Was empty on purpose, and `config.json = {}` reads like a bug and is
        # not one: it means nothing is configured, and PynixdSettings()
        # supplies its own defaults. It has been reported as a fault once.
        #
        # ssh_host is here for the same reason it is on the controller: an
        # empty env var is dropped by the apiserver, and pynixd's own default
        # of 127.0.0.1 would leave the builder unreachable from the
        # controller. See the controller block above. http_host is there for
        # the probes, and a builder carries the same ones.
        ssh_host = "";
        http_host = "";
        # The controller dials a builder with the cluster key, and the builder
        # trusts it: an untrusted sender's input-addressed `BuildDerivation`
        # is refused (`daemon.cc:634`), so delegation needs it. The secret
        # holds the public half beside the private key.
        ssh_admin_keys = [ "/etc/ssh-key/id_ed25519.pub" ];
      })
      config.nixkube.pynixd.settings
    ];

    nixkube.pynixd.builder.nixConfig.settings = {
      max-jobs = lib.mkDefault 5;
      warn-dirty = lib.mkDefault false;
    };

    kubernetes.resources.${cfg.namespace} = {
      # The controller and the builders both serve `/metrics`, and neither is
      # a node pod, so `daemonset.nix`'s PodMonitor does not select them.
      # These two do, and they read the same `nixkube.metrics` options: an
      # operator asked once for nixkube to be scraped.
      #
      # Two objects and not one. The controller and a builder answer for
      # different work, and a builder is short-lived, so a shared selector
      # would mix a steady series with a series that comes and goes.
      PodMonitor = lib.mkIf (cfg.metrics.enable && cfg.metrics.podMonitor) {
        pynixd = {
          metadata.labels = pynixdLabels;
          spec = {
            selector.matchLabels = pynixdMatchLabels;
            podMetricsEndpoints = [
              {
                # By name, not by number: the container declares the port
                # under this name, so the two move together.
                port = "http";
                path = "/metrics";
              }
            ];
          };
        };
        pynixd-builder = {
          metadata.labels = builderLabels;
          spec = {
            selector.matchLabels = builderLabels;
            podMetricsEndpoints = [
              {
                port = "http";
                path = "/metrics";
              }
            ];
          };
        };
      };
      StatefulSet.pynixd = {
        metadata.labels = pynixdLabels;
        metadata.annotations."nixkube/discard" = "true";
        spec = {
          serviceName = "pynixd";
          replicas = 1;
          selector.matchLabels = pynixdLabels;
          template = {
            metadata.labels = pynixdLabels;
            metadata.annotations = {
              "kubectl.kubernetes.io/default-container" = "pynixd";
              # `appstarter-init` brings its own store and must not be
              # injected into. It mounts the claim at `/nix-volume` and
              # realises the closure there as a chroot store, so the `/nix`
              # check in `CreateContainer` does not cover it, and it cannot
              # mount `/nix` either -- that would shadow the image's own
              # store, which is where its fallback copy lives (issue #49).
              #
              # Without this, NRI reads `APPSTARTER_WANTED` out of that
              # container's environment and realises pynixd's closure into
              # the node's store, whose only substituter is the pynixd this
              # pod is starting. Measured on nixlab2: the container died with
              # StartError 128 after nri-wait gave up, and pynixd could not
              # be rolled forward at all. Issue #55, and issue #27's cycle.
              "nixkube/appstarter-init-exclude" = "true";
              configHash = lib.hashAttrs (
                { }
                // nsRes.ConfigMap.pynixd or { }
                // nsRes.ConfigMap.ssh-config or { }
                // nsRes.ConfigMap.pynixd-config or { }
              );
            }
            // lib.optionalAttrs (cfg.metrics.enable && cfg.metrics.annotations) {
              "prometheus.io/scrape" = "true";
              "prometheus.io/port" = toString pynixdHttpPort;
              "prometheus.io/path" = "/metrics";
            };
            spec = {
              serviceAccountName = "nixkube";
              priorityClassName = "system-cluster-critical";

              /*
                Fill the PVC before pynixd starts, the same way the node
                DaemonSet fills its host store. Issue #49.

                A CSI ephemeral volume from the node driver would serve
                `cacheEnv` too, and it is not reliable enough to boot from:
                nixkube garbage collects hard when a pod exits, so the
                environment pynixd needs can be gone by the next start. It is
                also the #27 cycle -- the node holds `cacheEnv` only once
                pynixd has served it, and pynixd cannot start until the node
                holds it.

                The image's own `cacheEnv` breaks both. `appstarter init`
                takes it when nothing else can serve the path, so pynixd
                starts behind rather than not at all.

                `PYNIXD_ENABLED` is false here whatever the deployment says.
                The pynixd substituter is this pod, and it is not listening
                yet.
              */
              initContainers = lib.mkNumberedList {
                "1" = {
                  name = "appstarter-init";
                  image = cfg.images.nix;
                  inherit (cfg) imagePullPolicy;
                  securityContext.privileged = true; # chroot store
                  command = [
                    "appstarter"
                    "init"
                  ];
                  env = lib.mkNamedList {
                    APPSTARTER_WANTED.value = builtins.toJSON (
                      lib.mapAttrs (
                        system: sysPkgs: "${packageRef "nixkube-pynixd-env-${system}" sysPkgs.nixkube-pynixd-env}"
                      ) csiPkgs
                    );
                    APPSTARTER_ROLE.value = "cache";
                    PYNIXD_ENABLED.value = "false";
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
              };

              containers = lib.mkNamedList {
                pynixd = {
                  command = [
                    "tini"
                    "--"
                    "appstarter"
                    "run"
                    "pynixd-nixkube-central"
                  ];
                  inherit image;
                  env = lib.mkNamedList {
                    PYNIXD_ENABLED.value = lib.boolToString cfg.pynixd.enable;
                    # PYNIXD_SSH_HOST is deliberately absent. It is set in
                    # config.json instead -- see nixkube.pynixd.settings above.
                    PYNIXD_SSH_PORT.value = "22";
                    PYNIXD_HTTP_PORT.value = toString pynixdHttpPort;
                    PYNIXD_SSH_HOST_KEY.value = "/etc/ssh-key/id_ed25519";
                    HOME.value = "/data/var/nix-csi/root";
                    PYNIXD_KUBE_NAMESPACE.valueFrom.fieldRef.fieldPath = "metadata.namespace";
                    # From the settings, not literals.
                    #
                    # NixkubeCentralSettings reads these three from the
                    # environment only -- it has no config-file source, unlike
                    # PynixdSettings. So a literal here silently wins over
                    # anything an operator puts in nixkube.pynixd.settings, and
                    # the option to cap builders did nothing whatever its
                    # priority.
                    PYNIXD_BUILDER_MAX.value = toString cfg.pynixd.controller.settings.builder-max;
                    PYNIXD_BUILDER_MIN.value = toString cfg.pynixd.controller.settings.builder-min;
                    PYNIXD_IDLE_TIMEOUT.value = toString cfg.pynixd.controller.settings.idle-timeout;
                    PYNIXD_BUILDER_STARTUP_TIMEOUT.value = toString cfg.pynixd.controller.settings.builder-startup-timeout;
                    PYNIXD_BUILDER_BACKOFF_CAP.value = toString cfg.pynixd.controller.settings.builder-backoff-cap;
                    PYNIXD_BUILDER_MAX_AGE.value = toString cfg.pynixd.controller.settings.builder-max-age;
                    PYNIXD_SCHEDULE_MODE.value = "scheduler";
                    PYNIXD_SYSTEMS.value = lib.concatStringsSep "," (builtins.attrNames enabledSystems);
                    PYNIXD_CONFIG.value = "/etc/pynixd-config/config.json";
                  };
                  ports = lib.mkNamedList {
                    ssh.containerPort = 22;
                    # The probes are httpGet on this port; see `probes` above,
                    # and a PodMonitor scrapes `/metrics` on it by this name.
                    http.containerPort = pynixdHttpPort;
                  };
                  inherit (probes) readinessProbe livenessProbe startupProbe;
                  # A plain list, not `mkNamedList`. That helper keys on the
                  # volume name and writes it into each entry, and the PVC
                  # below is mounted twice under the one name.
                  volumeMounts =
                    lib.mapAttrsToList (name: mount: mount // { inherit name; }) (
                      {
                        nix-config.mountPath = "/etc/nix";
                        nix-key.mountPath = "/etc/nix-key";
                        nix-store.mountPath = "/data";

                        ssh-config.mountPath = "/etc/ssh";
                        ssh-dynauth.mountPath = "/etc/ssh-dynauth";
                        ssh-key.mountPath = "/etc/ssh-key";
                        pynixd-config.mountPath = "/etc/pynixd-config";
                      }
                      // cfg.pynixd.extraVolumeMounts
                    )
                    ++ [
                      /*
                        The PVC a second time, at the prefix a `/nix/...` path
                        resolves under. /data is the chroot store root the
                        initContainer filled, so /data/nix is exactly this.

                        `subPath` is safe on a PVC. The kubelet performs the
                        bind in its own mount namespace, and a PVC is already
                        staged there. The node's store is a hostPath and is
                        not, which is why `nix-root` is a volume of its own
                        over there -- see issue #16.
                      */
                      {
                        name = "nix-store";
                        mountPath = "/nix";
                        subPath = "nix";
                      }
                    ];
                  resources = {
                    requests = {
                      memory = "64Mi";
                      cpu = "100m";
                    };
                  };
                };
              };
              volumes = lib.mkNamedList (
                {
                  nix-config.configMap.name = "pynixd";
                  nix-key.secret.secretName = "nix-key";

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
                  pynixd-config.configMap = {
                    name = "pynixd-config";
                    defaultMode = 292; # 444
                  };
                }
                // cfg.pynixd.extraVolumes
              );
            };
          };
          volumeClaimTemplates = lib.mkNumberedList {
            "1" = {
              metadata.name = "nix-store";
              spec = {
                accessModes = [ "ReadWriteOnce" ];
                resources.requests.storage = cfg.pynixd.storageSize;
              }
              # Omitted when null, and not rendered as `storageClassName: null`.
              #
              # The option means "use the cluster's default StorageClass", and
              # the way to say that is to leave the key out. Rendering a null
              # says something else: the apiserver defaults an unset
              # storageClassName to the name of the default class and stores
              # it, so the live object holds a string where the manifest holds
              # null, permanently.
              #
              # volumeClaimTemplates are immutable after creation, so nothing
              # can reconcile that difference later either.
              #
              # Same rule as the empty env var above -- a field the apiserver
              # rewrites must not be rendered -- and this is the null case of
              # it rather than the empty-string one.
              // lib.optionalAttrs (cfg.pynixd.storageClassName != null) {
                inherit (cfg.pynixd) storageClassName;
              };
            };
          };
        };
      };

      Service.pynixd = {
        metadata.labels = pynixdLabels;
        spec = {
          selector = pynixdMatchLabels;
          ports = lib.mkNamedList {
            ssh = {
              port = 22;
              targetPort = "ssh";
            };
          };
          type = "ClusterIP";
        };
      };
      Service.pynixd-lb = lib.mkIf (cfg.pynixd.loadBalancerPort != null) {
        metadata.labels = pynixdLabels;
        spec = {
          selector = pynixdMatchLabels;
          ports = lib.mkNamedList {
            ssh = {
              port = cfg.pynixd.loadBalancerPort;
              targetPort = "ssh";
            };
          };
          type = "LoadBalancer";
        };
      };

      ConfigMap.builder = {
        metadata.labels = builderLabels;
        data = {
          "nix.conf" = readConfigurationFile packageRef.enabled cfg.pynixd.builder.nixConfig.nixConf;
        };
      };
      ConfigMap.pynixd-config = {
        metadata.labels = pynixdLabels;
        data = {
          "config.json" = builtins.toJSON cfg.pynixd.controller.settings;
        };
      };
      ConfigMap.builder-config = {
        metadata.labels = builderLabels;
        data = {
          "config.json" = builtins.toJSON cfg.pynixd.builder.settings;
        };
      };

      PodTemplate.nixkube-builder = {
        metadata.labels = builderLabels;
        # Name the store paths, do not depend on them.
        #
        # This template's `nix-store` volume carries `storeVolumeAttributes`,
        # a `cacheEnv` per enabled system. Without this annotation the
        # transformer in `options.nix` keeps the string context, so rendering
        # on x86_64 must *build* the aarch64 `cacheEnv` -- measured at 3120
        # aarch64 derivations for `kubenixApply.manifestJSONFile`, which the
        # `release` job renders on `ubuntu-latest`.
        #
        # It can never succeed there: `buildEnv` sets `allowSubstitutes =
        # false`, and such a derivation is built, never fetched. Not a cache
        # race. The StatefulSet beside it has always carried this; this
        # template was missed.
        metadata.annotations."nixkube/discard" = "true";
        template = {
          metadata.labels = builderLabels;
          metadata.annotations = lib.optionalAttrs (cfg.metrics.enable && cfg.metrics.annotations) {
            "prometheus.io/scrape" = "true";
            "prometheus.io/port" = toString pynixdHttpPort;
            "prometheus.io/path" = "/metrics";
          };
          spec = {
            serviceAccountName = "nixkube";
            restartPolicy = "Never";
            affinity = {
              podAntiAffinity = {
                preferredDuringSchedulingIgnoredDuringExecution = [
                  {
                    weight = 100;
                    podAffinityTerm = {
                      topologyKey = "kubernetes.io/hostname";
                      labelSelector.matchLabels = builderLabels;
                    };
                  }
                ];
              };
            };
            containers = lib.mkNamedList {
              pynixd = {
                command = [
                  "/nix/var/result/bin/tini"
                  "--"
                  "/nix/var/result/bin/pynixd-nixkube-builder"
                ];
                inherit image;
                env = lib.mkNamedList {
                  # PYNIXD_SSH_HOST is deliberately absent, as on the
                  # controller. builder.settings carries it into config.json.
                  PYNIXD_SSH_PORT.value = "22";
                  PYNIXD_HTTP_PORT.value = toString pynixdHttpPort;
                  # The key the controller already pins. Without this,
                  # `start_ssh_server` falls through to
                  # `generate_private_key("ssh-rsa", ...)` and a builder
                  # presents a fresh random RSA host key every start.
                  #
                  # `ssh_known_hosts` names one ed25519 key for `*`, and
                  # asyncssh derives the acceptable server-host-key algorithms
                  # from that entry. ed25519 against rsa is an empty
                  # intersection, so the exchange dies before authentication:
                  #
                  #   asyncssh.misc.KeyExchangeFailed:
                  #     Unable to find compatible server host key
                  #
                  # Measured on nixlab2: 82 builders registered, 82
                  # ssh_connect_failed, zero reachable, ever. The builders were
                  # healthy -- a builder's own local store probes clean -- so
                  # this is the whole of why no node has ever been labelled.
                  # The controller StatefulSet has always set this; this
                  # template was missed.
                  PYNIXD_SSH_HOST_KEY.value = "/etc/ssh-key/id_ed25519";
                  PYNIXD_IDLE_TIMEOUT.value = toString cfg.pynixd.controller.settings.idle-timeout;
                  HOME.value = "/nix/var/nix-csi/root";
                  PYNIXD_CONFIG.value = "/nix/etc/builder-config/config.json";
                };
                ports = lib.mkNamedList {
                  ssh.containerPort = 22;
                  # The probes are httpGet on this port; see `probes` above,
                  # and a PodMonitor scrapes `/metrics` on it by this name.
                  http.containerPort = pynixdHttpPort;
                };
                inherit (probes) readinessProbe livenessProbe startupProbe;
                volumeMounts = lib.mkNamedList {
                  nix-config.mountPath = "/etc/nix";
                  nix-store = {
                    mountPath = "/nix";
                    subPath = "nix";
                  };
                  builder-config.mountPath = "/nix/etc/builder-config";
                  ssh-key.mountPath = "/etc/ssh-key";
                };
                resources = {
                  requests = {
                    memory = "256Mi";
                    cpu = "250m";
                  };
                };
              };
            };
            volumes = lib.mkNamedList {
              nix-config.configMap.name = "builder";
              nix-store.csi = {
                driver = "nixkube";
                readOnly = false;
                volumeAttributes = storeVolumeAttributes;
              };
              builder-config.configMap.name = "builder-config";
              ssh-key.secret = {
                secretName = "ssh-key";
                defaultMode = 256; # 400
              };
            };
          };
        };
      };
    };
  };
}
