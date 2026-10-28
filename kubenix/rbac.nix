# SPDX-License-Identifier: Apache-2.0

{
  config,
  lib,
  ...
}:
let
  cfg = config.nixkube;
in
{
  config = lib.mkIf cfg.enable {
    kubernetes.resources.none = {
      ClusterRole.nixkube = {
        metadata.labels = cfg.labels;
        rules = [
          {
            apiGroups = [ "" ];
            resources = [ "pods" ];
            verbs = [
              "get"
              "list"
            ];
          }
          # Kubelet configz via API server proxy for CRI socket discovery
          {
            apiGroups = [ "" ];
            resources = [ "nodes/proxy" ];
            verbs = [ "get" ];
          }
          # Builder manager discovers cluster node architectures, and writes
          # the probe result back as a label and an annotation.
          {
            apiGroups = [ "" ];
            resources = [ "nodes" ];
            verbs = [
              "get"
              "list"
              "watch"
              "patch"
            ];
          }
          # Which nodes have the nixkube CSI driver registered. kubelet writes
          # this on plugin registration, so it is the fact a builder's volume
          # depends on rather than a proxy for it. Read-only: nothing here
          # ever writes a CSINode.
          {
            apiGroups = [ "storage.k8s.io" ];
            resources = [ "csinodes" ];
            verbs = [
              "get"
              "list"
              "watch"
            ];
          }
          # Report events using events.k8s.io/v1 API across all namespaces
          {
            apiGroups = [ "events.k8s.io" ];
            resources = [ "events" ];
            verbs = [
              "get"
              "list"
              "create"
              "patch"
            ];
          }
        ];
      };
      ClusterRoleBinding.nixkube = {
        metadata.labels = cfg.labels;
        subjects = lib.mkNamedList {
          nixkube = {
            kind = "ServiceAccount";
            inherit (cfg) namespace;
          };
        };
        roleRef = {
          kind = "ClusterRole";
          name = "nixkube";
          apiGroup = "rbac.authorization.k8s.io";
        };
      };
    };
    kubernetes.resources.${cfg.namespace} = {
      ServiceAccount.nixkube = {
        metadata.labels = cfg.labels;
      };

      Role.nixkube = {
        metadata.labels = cfg.labels;
        rules = [
          # Cache maintains up2date /etc/machines
          # Builder manager watches builder pods
          {
            apiGroups = [ "" ];
            resources = [ "pods" ];
            verbs = [
              "get"
              "list"
              "watch"
              "delete"
            ];
          }
          # ssh secret, CRUD
          {
            apiGroups = [ "" ];
            resources = [
              "secrets"
              "configmaps"
            ];
            verbs = [
              "get"
              "list"
              "create"
              "patch"
              "delete"
            ];
          }
          # Builder manager creates and deletes Jobs for ephemeral builders
          {
            apiGroups = [ "batch" ];
            resources = [ "jobs" ];
            verbs = [
              "get"
              "list"
              "watch"
              "create"
              "delete"
            ];
          }
          # Builder manager reads PodTemplate for builder pod spec
          {
            apiGroups = [ "" ];
            resources = [ "podtemplates" ];
            verbs = [
              "get"
              "list"
            ];
          }
        ];
      };

      # Binds the Role to the ServiceAccount.
      RoleBinding.nixkube = {
        metadata.labels = cfg.labels;
        subjects = lib.mkNamedList {
          nixkube.kind = "ServiceAccount";
        };
        roleRef = {
          kind = "Role";
          name = "nixkube";
          apiGroup = "rbac.authorization.k8s.io";
        };
      };
    };
  };
}
