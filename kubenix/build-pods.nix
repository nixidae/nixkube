# SPDX-License-Identifier: Apache-2.0

/**
  pynixd's build-pod mode: each build in a pod of its own, which gives way to
  any ordinary pod. nixidae/nixidae#1560, spike.

  On, it puts two things on the cluster that a build pod's template names: the
  PriorityClasses `buildPods.priorityClassName` and
  `buildPods.nestedPriorityClassName`, and on every node the seccomp
  profile `buildPods.seccompProfile`, which the node DaemonSet writes under
  the kubelet's `seccomp` directory. It also lets pynixd create the pods and
  read their logs. Off, the apply prunes the PriorityClass and the Role, and
  each node removes the profile when its DaemonSet pod restarts.
*/
{
  config,
  lib,
  ...
}:
let
  cfg = config.nixkube;
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
  };

  config = lib.mkIf (cfg.enable && cfg.buildPods.enable) {
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
