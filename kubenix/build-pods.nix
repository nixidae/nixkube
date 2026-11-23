# SPDX-License-Identifier: Apache-2.0

/**
  pynixd's build-pod mode: each build in a pod of its own, which gives way to
  any ordinary pod. nixidae/nixidae#1560, spike.

  On, it puts two things on the cluster that a build pod's template names: the
  PriorityClasses `buildPods.priorityClassName` and
  `buildPods.nestedPriorityClassName`, and on every node the seccomp
  profile `buildPods.seccompProfile`, which the node DaemonSet writes under
  the kubelet's `seccomp` directory. The DaemonSet's `userns-probe` labels each
  node `nixkube/userns`, and a template selects `true`. It also lets pynixd create the pods and
  read their logs. Off, the apply prunes the PriorityClass and the Role, and
  each node removes the profile when its DaemonSet pod restarts.

  With `buildPods.builds` (the default), central also gets one store per
  system in `nixkube.systems`, `buildpods-<system>`, and a pod template for
  each. Builds then go to pods. The pod's `/nix` is a nixkube farm of the
  build's inputs and of what the template runs, so the pod copies no input.
  The farm's store is writable, on the pod's own emptyDir `store`
  (`storeVolume`): the outputs count against the pod's ephemeral storage, and
  the pod's pynixd uses `/nix` as its store, with every input already valid.

  With `buildPods.unsandboxed`, each system gets a second template, for the
  nodes that cannot run the sandbox.
*/
{
  config,
  lib,
  csiPkgs,
  curPkgs,
  ...
}:
let
  cfg = config.nixkube;
  pods = cfg.buildPods;
  uid = 1000;

  # The settings of the pynixd in each pod. pynixd answers every operation: a Nix daemon would not know the farm.
  podConfig = curPkgs.writeText "nixkube-build-pod-pynixd.json" (
    builtins.toJSON (
      lib.recursiveUpdate {
        stores.local = {
          type = "local-socket";
          store_path = "/";
          managed = true;
          daemon = false;
          pynixd_writes = true;
          op_routes.BuildDerivation = "pynixd";
          sibling_sandbox = "/build/sibling";
        };
        unix_path = "/build/pynixd.sock";
        ssh_port = null;
        gc_enabled = false;
        ranking.min_schedule_score = -1.0e9;
        ranking.min_free_bytes = 0;
      } pods.podSettings
    )
  );

  mounts = [
    {
      name = "nix";
      mountPath = "/nix";
    }
    {
      name = "build";
      mountPath = "/build";
    }
    # The farm's store disk: the pod reads and writes it at /nix. Mounted here as well because kubelet sets up
    # and counts only a volume some container mounts (measured).
    {
      name = "store";
      mountPath = "/store-disk";
      readOnly = true;
    }
  ];
  unprivileged = {
    allowPrivilegeEscalation = false;
    capabilities.drop = [ "ALL" ];
  };
  container = {
    image = cfg.images.scratch;
    inherit (cfg) imagePullPolicy;
    securityContext = unprivileged;
    volumeMounts = mounts;
  };

  # Unsandboxed: for a node whose probe says `false`. The build runs without Nix's sandbox, in the sandbox
  # container still, which holds no job key and no pynixd.
  template = pkgs: sandboxed: {
    pod.spec = {
      terminationGracePeriodSeconds = 5;
      # Central's pod owns each build pod, and central deletes the pods an earlier run left: this bounds the rest.
      activeDeadlineSeconds = pods.deadlineSeconds;
      inherit (pods) priorityClassName;
      nodeSelector = {
        "kubernetes.io/arch" = pkgs.stdenv.hostPlatform.go.GOARCH;
        # `nixkube-userns-probe` sets it, in the sandbox container's security context.
        "nixkube/userns" = lib.boolToString sandboxed;
      };
      securityContext = {
        runAsUser = uid;
        runAsGroup = uid;
        runAsNonRoot = true;
        seccompProfile.type = "RuntimeDefault";
        # The kubelet gives this group read on the job Secret, which pynixd mounts 0400.
        fsGroup = uid;
      };
      containers = [
        (
          container
          // {
            name = "pynixd";
            command = [
              "${pkgs.pynixd-build-pod}/bin/pynixd"
              "build-one"
              "/run/pynixd-job"
            ];
            inherit (pods) resources;
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
                  system-features = ${lib.concatStringsSep " " pods.systemFeatures}
                ''
                + lib.optionalString (!sandboxed) ''
                  sandbox = false
                '';
              }
              {
                name = "PATH";
                value = "${pkgs.nix}/bin:${pkgs.coreutils}/bin";
              }
              # Nix takes HOME only when the user owns it, and the pod's store root `/` is no place for one.
              # `build-one` makes it.
              {
                name = "HOME";
                value = "/build/home";
              }
            ];
          }
        )
        (
          container
          // {
            name = "sandbox";
            command = [
              "${pkgs.pynixd-build-pod.venv}/lib/venv/bin/python"
              "-I"
              "-m"
              "pynixd_store.builder.sibling"
              "run"
              "/build/sibling"
            ];
            # Unsandboxed, the pod's RuntimeDefault seccomp profile: the build makes no namespace.
            securityContext = if sandboxed then pods.sandboxSecurityContext else unprivileged;
          }
        )
      ];
      volumes = [
        # pynixd fills `volumeAttributes` for each build.
        {
          name = "nix";
          csi = {
            driver = "nixkube";
            volumeAttributes.storeVolume = "store";
          };
        }
        {
          name = "store";
          emptyDir = { };
        }
        {
          name = "build";
          emptyDir = { };
        }
      ];
    };
  };

  store = system: {
    type = "kubernetes-build-pods";
    # Central takes the first template a Ready node matches: the unsandboxed one only when no node can sandbox.
    templates = [
      "buildpods-${system}"
    ]
    ++ lib.optional pods.unsandboxed "buildpods-${system}-unsandboxed";
    central_host = "pynixd.${cfg.namespace}.svc.cluster.local";
    central_port =
      (lib.findFirst (port: port.name == "ssh") (throw "Service pynixd has no ssh port")
        config.kubernetes.resources.${cfg.namespace}.Service.pynixd.spec.ports
      ).port;
    systems = [ system ];
    system_features = pods.systemFeatures;
    mandatory_features = pods.mandatoryFeatures;
    max_connections = pods.maxBuilds;
    farm.volume = "nix";
    priority_classes = [
      pods.priorityClassName
      pods.nestedPriorityClassName
    ];
  };
in
{
  options.nixkube.buildPods = {
    enable = lib.mkEnableOption "pynixd's build pods: the PriorityClass and seccomp profile they name";
    priorityClassName = lib.mkOption {
      description = "The PriorityClass a build pod's template names in `spec.priorityClassName`.";
      type = lib.types.str;
      readOnly = true;
      default = "pynixd-build";
    };
    nestedPriorityClassName = lib.mkOption {
      description = ''
        The PriorityClass of a build pod for a build that recursive Nix in another build pod asked for.
        Central's store names it second in `priority_classes`.
      '';
      type = lib.types.str;
      readOnly = true;
      default = "pynixd-build-nested";
    };
    seccompProfile = lib.mkOption {
      description = ''
        The profile a build pod's sandbox container names, as
        `securityContext.seccompProfile = { type = "Localhost"; localhostProfile = <this>; }`.
        Relative to the kubelet's `seccomp` directory.
      '';
      type = lib.types.str;
      readOnly = true;
      default = "nixkube/pynixd-builder.json";
    };
    sandboxSecurityContext = lib.mkOption {
      description = ''
        The `securityContext` of a build pod's sandbox container. The node DaemonSet's `userns-probe`
        container runs with the same, so the node label it sets says what a build pod meets.
        The build pods' seccomp profile allows the namespaces the sandbox makes; RuntimeDefault refuses them.
        No AppArmor field: the kubelet rejects one on a node without AppArmor, and containerd's default
        profile does not mediate user namespaces.
      '';
      type = lib.types.attrsOf lib.types.anything;
      readOnly = true;
      internal = true;
      default = unprivileged // {
        runAsUser = uid;
        runAsGroup = uid;
        runAsNonRoot = true;
        seccompProfile = {
          type = "Localhost";
          localhostProfile = pods.seccompProfile;
        };
      };
    };
    builds =
      lib.mkEnableOption ''
        central's build pod stores, one per system: builds that fit one go to a pod of their own.
        Off, the cluster holds the PriorityClasses and the profile, and no build runs in a pod
      ''
      // {
        default = true;
      };
    unsandboxed = lib.mkEnableOption ''
      a second build pod template per system, for the nodes whose `userns-probe` labels `nixkube/userns` `false`.
      Its builds run without Nix's sandbox, in the sandbox container: they share the pod's network and hostname,
      and only the farm keeps a build to its declared inputs. Central takes it only when no Ready node can sandbox
    '';
    systemFeatures = lib.mkOption {
      description = "The features a build pod offers: its store's `system_features`, and the pod's Nix `system-features`.";
      type = lib.types.listOf lib.types.str;
      default = [ ];
    };
    mandatoryFeatures = lib.mkOption {
      description = "The features a build must ask for to go to a build pod. Empty: every build of the system may.";
      type = lib.types.listOf lib.types.str;
      default = [ ];
    };
    maxBuilds = lib.mkOption {
      description = "How many build pods of one system run at once. `null`: as many as central schedules.";
      type = lib.types.nullOr lib.types.ints.positive;
      default = null;
    };
    deadlineSeconds = lib.mkOption {
      description = "A build pod's `activeDeadlineSeconds`: the longest build a pod runs.";
      type = lib.types.ints.positive;
      default = 21600;
    };
    resources = lib.mkOption {
      description = "The resources of a build pod's `pynixd` container, which the build runs beside.";
      type = lib.types.attrsOf lib.types.anything;
      default.requests.cpu = "100m";
    };
    podSettings = lib.mkOption {
      description = "Merged into the settings of the pynixd in each build pod, such as `log_level`.";
      type = lib.types.attrsOf lib.types.anything;
      default = { };
      example.log_level = "INFO";
    };
  };

  config = lib.mkIf (cfg.enable && cfg.buildPods.enable) {
    # At option-default priority, so an operator's settings win key by key.
    nixkube.pynixd.controller.settings = lib.mkIf pods.builds (
      lib.mapAttrsRecursive (_: lib.mkOptionDefault) {
        stores = lib.mapAttrs' (system: _: lib.nameValuePair "buildpods-${system}" (store system)) csiPkgs;
        # The pod's service account: no kubeconfig.
        provisioning.kubernetes = {
          inherit (cfg) namespace;
          templates = lib.concatMapAttrs (
            system: pkgs:
            {
              "buildpods-${system}" = template pkgs true;
            }
            // lib.optionalAttrs pods.unsandboxed {
              "buildpods-${system}-unsandboxed" = template pkgs false;
            }
          ) csiPkgs;
        };
      }
    );
    assertions = [
      {
        assertion = cfg.node.enable;
        message = "nixkube.buildPods.enable needs nixkube.node.enable: the node DaemonSet installs the build pods' seccomp profile.";
      }
    ];
    # Below every ordinary pod, whose default is 0: the scheduler preempts a
    # build pod for one, and the kubelet evicts build pods first. `Never`, so
    # a build pod takes no other pod's place.
    kubernetes.resources.none.PriorityClass.${cfg.buildPods.priorityClassName} = {
      metadata.labels = cfg.labels;
      value = -2;
      preemptionPolicy = "Never";
      globalDefault = false;
      description = "pynixd's build pods: the first to go when another pod needs the room.";
    };
    # A nested build's pod, one above: on a full node it evicts a build pod it
    # may wait on, so the outer build can finish. Every ordinary pod is above
    # it, so it preempts none of them. nixidae/nixidae#1576.
    kubernetes.resources.none.PriorityClass.${cfg.buildPods.nestedPriorityClassName} = {
      metadata.labels = cfg.labels;
      value = -1;
      preemptionPolicy = "PreemptLowerPriority";
      globalDefault = false;
      description = "pynixd's nested build pods: ahead of the build pods they are nested in.";
    };
    # pynixd creates each build pod and follows its log (`store/build_pods.py`). The Role in ./rbac.nix
    # already lets it watch and delete pods, and create the pod's job Secret.
    kubernetes.resources.${cfg.namespace} = {
      # A build pod dials central's SSH port, and after its name, the cluster DNS. Nothing dials a build pod:
      # central reads its log and its end through the API server. Only a CNI that enforces policies holds it.
      NetworkPolicy.nixkube-build-pods = {
        metadata.labels = cfg.labels;
        spec = {
          podSelector.matchLabels."pynixd.nixidae.org/build" = "true";
          policyTypes = [
            "Ingress"
            "Egress"
          ];
          ingress = [ ];
          egress = [
            {
              to = [
                {
                  podSelector.matchLabels = cfg.matchLabels // {
                    "app.kubernetes.io/component" = "pynixd";
                  };
                }
              ];
              ports = [
                {
                  protocol = "TCP";
                  port = "ssh";
                }
              ];
            }
            {
              to = [
                {
                  namespaceSelector.matchLabels."kubernetes.io/metadata.name" = "kube-system";
                  podSelector.matchLabels.k8s-app = "kube-dns";
                }
              ];
              ports = [
                {
                  protocol = "UDP";
                  port = 53;
                }
                {
                  protocol = "TCP";
                  port = 53;
                }
              ];
            }
          ];
        };
      };
      Role.nixkube-build-pods = {
        metadata.labels = cfg.labels;
        rules = [
          {
            apiGroups = [ "" ];
            resources = [ "pods" ];
            verbs = [ "create" ];
          }
          {
            apiGroups = [ "" ];
            resources = [ "pods/log" ];
            verbs = [ "get" ];
          }
        ];
      };
      RoleBinding.nixkube-build-pods = {
        metadata.labels = cfg.labels;
        subjects = lib.mkNamedList {
          nixkube.kind = "ServiceAccount";
        };
        roleRef = {
          kind = "Role";
          name = "nixkube-build-pods";
          apiGroup = "rbac.authorization.k8s.io";
        };
      };
    };
  };
}
