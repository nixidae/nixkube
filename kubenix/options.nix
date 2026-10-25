# SPDX-License-Identifier: Apache-2.0

{
  config,
  curPkgs,
  lib,
  # The caller's `_module.args.sources`; ./default.nix falls back to this
  # repository's own lock (#79).
  sources,
  ...
}:
let
  cfg = config.nixkube;
  defaultLoggers = {
    "nixkube".level = "INFO";
    "nixkube.nix_daemon".level = "WARNING";
    "httpx".level = "WARNING";
  };
in
{
  imports = [
    (lib.mkRenamedOptionModule [ "nix-csi" ] [ "nixkube" ])
    (lib.mkRenamedOptionModule [ "nixkube" "cache" ] [ "nixkube" "pynixd" ])
  ];
  options.nixkube = {
    enable = lib.mkEnableOption "nixkube";
    undeploy = lib.mkOption {
      description = "When true, removes all nixkube Kubernetes resources on the next apply.";
      type = lib.types.bool;
      default = false;
    };
    deploySecrets = lib.mkOption {
      description = "Deploy SSH keypair Secrets to Kubernetes. Disable if managing secrets externally (e.g., with Vault or Sealed Secrets).";
      type = lib.types.bool;
      default = true;
    };
    namespace = lib.mkOption {
      description = "Which namespace to deploy nixkube to";
      type = lib.types.str;
      default = "nixkube";
    };
    knownHosts = lib.mkOption {
      description = ''
        SSH host keys to accept when connecting to cache.
        Keys are written to known_hosts on nodes so they can connect without interactive verification.
      '';
      type = lib.types.attrsOf (lib.types.either lib.types.str lib.types.path);
      apply = lib.mapAttrs (n: v: lib.trim (if lib.typeOf v == "path" then builtins.readFile v else v));
      default = { };
      example = lib.literalExpression ''
        {
          "nix-cache" = "ssh-ed25519 AAAA...";
        }
      '';
    };
    metadata = lib.mkOption {
      description = "Metadata (labels, annotations) applied to nixkube resources";
      type = (curPkgs.formats.json { }).type;
      default = { };
    };
    version = lib.mkOption {
      internal = true;
      type = lib.types.str;
      default =
        let
          pyproject = builtins.fromTOML (builtins.readFile ../pkgs/nixkube/pyproject.toml);
        in
        pyproject.project.version;
    };
    imagePullPolicy = lib.mkOption {
      description = ''
        How every container that runs nixkube's own image gets it.

        `Always` for a cluster: the tag moves, and a node holding an older
        image under the same name would run it forever.

        `IfNotPresent` for a test that imported the image this checkout
        built. `Always` sends kubelet to the registry whatever is on the
        node, so the test then runs whatever was published last and cannot
        see a local change at all -- which is how `appstarter`'s image
        fallback shipped broken. Only nixkube's own containers take this;
        the CSI sidecars come from registry.k8s.io and still pull.
      '';
      type = lib.types.enum [
        "Always"
        "IfNotPresent"
        "Never"
      ];
      default = "Always";
    };
    images = {
      nix = lib.mkOption {
        description = ''
          The image of every init container: appstarter, and the environments
          it falls back to.

          The default is a content tag, from the store hash of the image this
          checkout builds, so it names one content and rendering builds no
          image. Set any ref instead, such as `ghcr.io/nixidae/nixkube/nix:main`,
          to run an image this checkout did not build; set `imagePullPolicy`
          to `Always` with a tag that moves.
        '';
        type = lib.types.str;
        default =
          (import ../niximage.nix {
            pkgs = curPkgs;
            inherit lib sources;
            inherit (cfg) dinix;
          }).refFor
            (lib.attrNames (lib.filterAttrs (_: enabled: enabled) cfg.systems));
        defaultText = lib.literalMD "the content tag of this checkout's image, for `nixkube.systems`";
        example = "ghcr.io/nixidae/nixkube/nix:main";
      };
      scratch = lib.mkOption {
        description = ''
          The image of the main containers. It holds nothing but
          `PATH=/nix/var/result/bin`; `appstarter run` starts the program out
          of the store. The default pins the digest of `scratchImage.index`.
        '';
        type = lib.types.str;
        default = "ghcr.io/nixidae/nixkube/scratch@${(import ../nix/scratch-digest.nix).digest}";
        defaultText = lib.literalMD "the digest of `scratchImage.index`";
      };
    };
    nix = {
      package = lib.mkOption {
        description = "Nix package to use for nix.conf generation and daemon";
        type = lib.types.package;
        default = curPkgs.nix;
      };
    };
    nixConfig = lib.mkOption {
      description = "Shared nix.conf defaults inherited by node, pynixd controller, and builder.";
      type = (import ./nixOptions.nix) {
        pkgs = curPkgs;
        nix = config.nixkube.nix.package;
      };
    };
    hostMountPath = lib.mkOption {
      description = "Where on the host to put nixkube store, / is untested and not recommended";
      type = lib.types.path;
      default = "/var/lib/nix-csi";
    };

    hostStore.enable = lib.mkOption {
      description = ''
        Share the host's /nix with nixkube on nodes whose host runs NixOS.
        Issue #25.

        The node DaemonSet becomes two. `nix-node-host` mounts the host's
        /nix and talks to the host's nix-daemon; `nix-node-separate` keeps
        nixkube's own store at `hostMountPath` everywhere else. They split
        on the `nixkube/host=nixos` Node label, which each pod's first init
        container, `nixkube-host-check`, sets or removes: a pod on the wrong
        host moves its node to the other DaemonSet and never starts.

        In host mode the host owns garbage collection. nixkube keeps its GC
        roots, under /nix/var/nix/gcroots, correct and never sweeps or copies
        the store. Its state (/nix/var/nix-csi) lives in the host's /nix/var,
        and its nix client talks to the host's nix-daemon, whose version the
        host chooses; nothing here tests a version skew.
      '';
      type = lib.types.bool;
      default = false;
    };

    csi.composefs = lib.mkOption {
      description = ''
        Present a CSI volume as one composefs mount where the node's kernel
        can: an EROFS image of the closure over the store. The node probes its
        kernel once and keeps the hardlink tree where the probe fails (Linux
        before 6.5, no EROFS -- Talos and GKE's COS among them). Issue #68.

        False keeps the hardlink tree on every node. A node that shares its
        host's store (`hostStore.enable`) has no hardlink tree, so there it
        refuses the publish instead.
      '';
      type = lib.types.bool;
      default = true;
    };

    nixosHost.enable = lib.mkOption {
      description = ''
        Render the `nixkube-nixos-host` PodTemplate. A Job made from it
        switches a NixOS host to a system, arms a revert, disarms it or runs
        it (#359).

        Whoever can create a Job from it chooses what a machine runs, as
        root.
      '';
      type = lib.types.bool;
      default = false;
    };

    nixosHost.revertDelay = lib.mkOption {
      description = ''
        Seconds after a switch starts before the host reverts on its own,
        unless a `disarm` Job ran first. It must outlast the switch and its
        confirmation: it is the recovery when no pod can run on the node.
      '';
      type = lib.types.ints.positive;
      default = 1200;
    };

    nri.composefs = lib.mkOption {
      description = ''
        Give an NRI container its /nix as one composefs mount where the node's
        kernel can: the same image and probe as `csi.composefs`. Where the
        probe fails, the container gets the bind farm. VM runtimes always get
        a hardlink tree.

        False keeps the bind farm on every node.
      '';
      type = lib.types.bool;
      default = true;
    };

    nri.vmRuntimeHandlers = lib.mkOption {
      description = ''
        RuntimeClass handlers that run each pod in a virtual machine, such as
        Kata Containers. For these, the NRI plugin shares the container's
        /nix as a bind mount, which the runtime passes into the VM over
        virtio-fs, and the container waits for it before its own command.

        Read-only only. A writable /nix and store path mounts are refused.
      '';
      type = lib.types.listOf lib.types.str;
      default = [ "kata" ];
    };

    verifyStorePaths = lib.mkOption {
      description = "Verify Nix store paths after building or fetching, before mounting into pods.";
      type = lib.types.bool;
      default = true;
    };
    metrics = {
      enable = lib.mkOption {
        description = ''
          Serve Prometheus metrics from each node pod, on `port`.

          A DaemonSet answers for its own node, so the series are per-node:
          the size and free space of that node's /nix, and what its garbage
          collection and its volumes have done.
        '';
        type = lib.types.bool;
        default = true;
      };
      port = lib.mkOption {
        description = ''
          The port `/metrics` answers on. Arbitrary: nixkube holds no entry
          in the Prometheus port registry.
        '';
        type = lib.types.port;
        default = 9099;
      };
      annotations = lib.mkOption {
        description = ''
          Add `prometheus.io/*` annotations to the node pods, which is what a
          Prometheus configured for annotation discovery reads. Turn this off
          where a PodMonitor or a ServiceMonitor selects the pods instead, so
          that the two do not both scrape.

          Defaults to the opposite of `podMonitor`: an operator ignores these
          annotations, so leaving both on gives a cluster where one discovery
          path is dead and looks live.
        '';
        type = lib.types.bool;
        default = !config.nixkube.metrics.podMonitor;
        defaultText = lib.literalExpression "!config.nixkube.metrics.podMonitor";
      };
      podMonitor = lib.mkOption {
        description = ''
          Emit a `monitoring.coreos.com/v1` PodMonitor selecting the node
          pods.

          **Off by default, because it asserts something about the cluster.**
          A PodMonitor needs the prometheus-operator CRDs to exist, and
          nixkube neither ships them nor assumes them: a consumer installs
          them as its own component and turns this on to say so.

          A PodMonitor and not a `VMPodScrape`, even on a VictoriaMetrics
          cluster. The VictoriaMetrics operator converts prometheus-operator
          objects into its own, and the prometheus-operator kind has a real
          schema, so validation checks this object against an apiserver. A
          VM-native CR declares `x-kubernetes-preserve-unknown-fields` and
          would ship unchecked, which gives up the only reason to prefer one
          kind over the other.

          **The VictoriaMetrics operator converts only what it knew at
          start-up.** On a cluster where the CRDs and this object arrive in
          one apply, the PodMonitor lands, nothing converts it, and nothing
          reports that. Restart the operator once. A PodMonitor that is never
          converted looks exactly like one that is never scraped.
        '';
        type = lib.types.bool;
        default = false;
      };
    };
    nodeBuildTimeout = lib.mkOption {
      description = ''
        Timeout in seconds for Nix build operations on node pods.
        Builds exceeding this timeout will be terminated.
      '';
      type = lib.types.ints.positive;
      default = 300; # 5 minutes
    };
    loggingConfig = lib.mkOption {
      description = "Logging configuration for the nixkube service (structlog-based).";
      default = { };
      type = lib.types.submodule {
        options = {
          renderer = lib.mkOption {
            type = lib.types.enum [
              "json"
              "logfmt"
              "console"
            ];
            default = "json";
            description = ''
              Log output renderer:

              - `"json"` (default): Structured JSON, one object per line. Recommended
                for production and log aggregation (Loki, ELK, Datadog). Each
                structured field is a top-level JSON key, enabling rich queries:
                ```
                {app="nixkube"} | json | elapsed_time > 10
                {app="nixkube"} | json | returncode != 0
                {app="nixkube"} | json | container_id =~ "abc"
                ```

              - `"logfmt"`: `key=value` pairs on a single line. Human-readable and
                machine-parseable. Works well with `stern`, `kubectl logs | grep`,
                and log shippers with native logfmt support (Vector, Fluentd).
                Example line:
                ```
                level=info logger=nixkube.nri event=build_task_completed container_id=abc123
                ```

              - `"console"`: Coloured, aligned output for local development.
                Not suitable for log aggregation or machine parsing.
            '';
            example = "logfmt";
          };
          loggers = lib.mkOption {
            type = lib.types.attrsOf (
              lib.types.submodule {
                options.level = lib.mkOption {
                  type = lib.types.enum [
                    "DEBUG"
                    "INFO"
                    "WARNING"
                    "ERROR"
                    "CRITICAL"
                  ];
                  description = "Log level for this logger.";
                };
              }
            );
            default = defaultLoggers;
            description = ''
              Per-logger level overrides. Keys are Python logger names (dotted hierarchy).
              All loggers under `nixkube.*` inherit from `nixkube` unless individually overridden.
            '';
            example = lib.literalExpression ''
              {
                "nixkube".level = "DEBUG";
                "nixkube.nri".level = "DEBUG";
                "httpx".level = "ERROR";
              }
            '';
          };
          root = lib.mkOption {
            description = "Root logger configuration (catch-all for third-party libraries).";
            default = { };
            type = lib.types.submodule {
              options.level = lib.mkOption {
                type = lib.types.enum [
                  "DEBUG"
                  "INFO"
                  "WARNING"
                  "ERROR"
                  "CRITICAL"
                ];
                default = "WARNING";
                description = "Root logger level. All loggers inherit this unless overridden in `loggers`.";
              };
            };
          };
        };
      };
      example = lib.literalExpression ''
        # JSON renderer (default) — production/Loki
        {
          renderer = "json";
          loggers.nixkube.level = "DEBUG";
          root.level = "WARNING";
        }

        # Logfmt renderer — stern / grep-friendly
        {
          renderer = "logfmt";
          loggers.nixkube.level = "INFO";
        }

        # Console renderer — local development
        {
          renderer = "console";
          loggers.nixkube.level = "DEBUG";
          root.level = "DEBUG";
        }
      '';
    };
    systems = lib.mkOption {
      description = ''
        Which CPU architectures to build nixkube environments for.
        Disable aarch64-linux to skip cross-compilation if your cluster is x86_64-only.
      '';
      type = lib.types.attrsOf lib.types.bool;
      default = {
        "x86_64-linux" = true;
        "aarch64-linux" = true;
      };
      example = lib.literalExpression ''
        {
          "x86_64-linux" = true;
          "aarch64-linux" = false;
        }
      '';
    };
    hostSystem = lib.mkOption {
      description = ''
        Which architecture builds the host-side artefacts: the `nix.conf` and
        JSON config derivations, and the `nix` package that writes them. They
        are built wherever the manifest is rendered, never on a node, so this
        is unrelated to `systems` -- that names what the *nodes* run.

        It falls back to the one enabled system when `systems` names exactly
        one, and otherwise has no answer to infer. `builtins.currentSystem`
        is not that answer: it is absent under `--pure-eval`, so a module
        that reads it fails every pure consumer on "attribute
        'currentSystem' missing" before they reach anything of their own.
      '';
      type = lib.types.str;
      default =
        let
          enabled = lib.attrNames (lib.filterAttrs (_: e: e) cfg.systems);
        in
        if lib.length enabled == 1 then
          lib.head enabled
        else
          throw ''
            nixkube.hostSystem: ${toString (lib.length enabled)} architectures are enabled, so
            there is no host architecture to infer. Name the one that renders
            the manifest:

              nixkube.hostSystem = "x86_64-linux";

            It is not a node architecture -- `nixkube.systems` stays as it is.
          '';
      example = "x86_64-linux";
    };

    pkgs = lib.mkOption {
      type = lib.types.path;
      default = sources.nixpkgs;
      internal = true;
    };
    discardStringContext = lib.mkOption {
      type = lib.types.bool;
      default = true;
      description = ''
        Strip Nix string context from every resource annotated
        `nixkube/discard`, so rendering a manifest does not make the deployer
        realise the store paths it names.

        On by default, and it is an optimisation rather than a correctness
        choice. The node and pynixd environments are `buildEnv` outputs, one
        per enabled system, and `buildEnv` sets `allowSubstitutes = false`.
        Keeping their context therefore makes a deployer *build* each of
        them, never fetch them. Measured on an x86_64 machine with binfmt
        off:

          error: Cannot build '...-nodeEnv.drv'
                 Reason: platform mismatch
                 Required system: 'aarch64-linux'

        Its aarch64 dependencies substituted normally; only the `buildEnv`
        output refused. So a deployer who has not arranged foreign-platform
        builds cannot render a manifest for a cluster with a second
        architecture. That is what this avoids, and it is why the default
        holds even though the paths are on cachix: the cache cannot serve a
        derivation that declines to be substituted.

        What it costs. `ekn.cachePackage` defaults to the manifest and finds
        referenced paths through string context, so with this on it finds
        none of these:

          store paths named as text in manifest.json   4
          paths in its closure                         1   (itself)

        `ekn deploy` then reports a successful cache push and seeds none of
        the paths a node needs to boot. A cluster that relies on that push
        must name the environments itself, through the `csiPkgs` module
        argument -- `ekn.cachePackage`'s own documentation carries the
        fragment.

        Turning this off is reasonable when every enabled system is one the
        deployer can realise, or when `always-allow-substitutes = true` is
        set, which lets the fetch happen despite `allowSubstitutes = false`
        and needs no derivation change. nixkube's own CI instances turn it
        off for exactly that reason.

        See issue #29.
      '';
    };
    dinix = lib.mkOption {
      type = lib.types.path;
      internal = true;
      default = sources.dinix;
    };
    labels = lib.mkOption {
      type = lib.types.attrsOf lib.types.str;
      internal = true;
      description = "All nixkube labels including version (for metadata.labels)";
    };
    matchLabels = lib.mkOption {
      type = lib.types.attrsOf lib.types.str;
      internal = true;
      description = "nixkube base labels without version (for selector.matchLabels)";
    };
  };
  config =
    let
      mkPkgs =
        system:
        import cfg.pkgs {
          inherit system;
          overlays = [
            (final: prev: { nixkubeSources = sources; })
            (import ../pkgs)
            (
              final: prev:
              let
                callPackage =
                  packagePath:
                  final.callPackage packagePath {
                    inherit (cfg) dinix;
                  };
              in
              {
                nixkube-node-env = callPackage ../environments/node;
                nixkube-pynixd-env = callPackage ../environments/cache;

              }
            )
          ];
        };
    in
    lib.mkIf cfg.enable {
      # Provide helpers to all modules via _module.args
      _module.args = {
        csiPkgs = lib.pipe cfg.systems [
          (lib.filterAttrs (_: enabled: enabled))
          (lib.mapAttrs (system: _: mkPkgs system))
        ];
        curPkgs = mkPkgs cfg.hostSystem;
        subPath = spath: lib.removePrefix "/" (toString spath);
      };

      # Set default loggers
      nixkube.loggingConfig.loggers = lib.mapAttrsRecursive (_: v: lib.mkDefault v) defaultLoggers;

      # Set internal label options using the derived values
      nixkube.matchLabels = {
        "app.kubernetes.io/name" = "nixkube";
        "app.kubernetes.io/part-of" = "nixkube";
        "app.kubernetes.io/managed-by" = "nix";
      };
      nixkube.labels = cfg.matchLabels // {
        "app.kubernetes.io/version" = cfg.version;
      };

      kubernetes.transformers = lib.optional cfg.discardStringContext (
        resource:
        let
          mapRecursive =
            f: value:
            if builtins.isAttrs value then
              builtins.mapAttrs (n: v: mapRecursive f v) value
            else if builtins.isList value then
              map (mapRecursive f) value
            else
              f value;
        in
        if resource.metadata.annotations."nixkube/discard" or null == "true" then
          mapRecursive (x: if lib.isString x then builtins.unsafeDiscardStringContext x else x) resource
        else
          resource
      );
    };
}
