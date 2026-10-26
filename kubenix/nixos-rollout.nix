# SPDX-License-Identifier: Apache-2.0

/*
  NixOSRollout (#359): a cluster-scoped object that names the system each
  selected NixOS node runs, and the controller that switches the nodes one at
  a time through Jobs made from the `nixkube-nixos-host` PodTemplate.

  The controller has a ServiceAccount of its own: it may create privileged
  pods in this namespace, and the node DaemonSet must not.
*/
{
  config,
  lib,
  ...
}:
let
  cfg = config.nixkube;
  name = "nixkube-nixos-rollout";
  labels = cfg.labels // {
    "app.kubernetes.io/component" = "nixos-rollout";
  };
  matchLabels = cfg.matchLabels // {
    "app.kubernetes.io/component" = "nixos-rollout";
  };

  group = "nixkube.nixidae.org";

  str = {
    type = "string";
  };
  storePath = {
    type = "string";
    pattern = "^/nix/store/[0-9a-z]{32}-.+$";
  };
  labelSelector = {
    type = "object";
    properties = {
      matchLabels = {
        type = "object";
        additionalProperties = str;
      };
      matchExpressions = {
        type = "array";
        items = {
          type = "object";
          required = [
            "key"
            "operator"
          ];
          properties = {
            key = str;
            operator = {
              type = "string";
              enum = [
                "In"
                "NotIn"
                "Exists"
                "DoesNotExist"
              ];
            };
            values = {
              type = "array";
              items = str;
            };
          };
        };
      };
    };
  };
  nodePhases = [
    "Pending"
    "Conflict"
    "Switching"
    "Confirming"
    "Disarming"
    "Done"
    "Reverted"
  ];

  crd = {
    apiVersion = "apiextensions.k8s.io/v1";
    kind = "CustomResourceDefinition";
    metadata = {
      name = "nixosrollouts.${group}";
      inherit labels;
    };
    spec = {
      inherit group;
      scope = "Cluster";
      names = {
        kind = "NixOSRollout";
        listKind = "NixOSRolloutList";
        plural = "nixosrollouts";
        singular = "nixosrollout";
        shortNames = [ "nor" ];
      };
      versions = [
        {
          name = "v1alpha1";
          served = true;
          storage = true;
          subresources.status = { };
          additionalPrinterColumns = [
            {
              name = "Phase";
              type = "string";
              jsonPath = ".status.phase";
            }
            {
              name = "Age";
              type = "date";
              jsonPath = ".metadata.creationTimestamp";
            }
          ];
          schema.openAPIV3Schema = {
            type = "object";
            required = [ "spec" ];
            properties = {
              spec = {
                type = "object";
                required = [
                  "nodeSelector"
                  "systems"
                ];
                properties = {
                  nodeSelector = labelSelector;
                  systems = {
                    description = "The toplevel each selected node runs, by node name.";
                    type = "object";
                    additionalProperties = storePath;
                  };
                  maxUnavailable = {
                    type = "integer";
                    minimum = 1;
                    default = 1;
                  };
                  confirmTimeout = {
                    description = "Seconds after a switch before an unconfirmed node is reverted. Less than the revert delay.";
                    type = "integer";
                    minimum = 1;
                    default = 300;
                  };
                  paused = {
                    type = "boolean";
                    default = false;
                  };
                };
              };
              status = {
                type = "object";
                properties = {
                  observedGeneration.type = "integer";
                  phase = {
                    type = "string";
                    enum = [
                      "Progressing"
                      "Complete"
                      "Halted"
                    ];
                  };
                  message = str;
                  nodes = {
                    type = "object";
                    additionalProperties = {
                      type = "object";
                      properties = {
                        phase = {
                          type = "string";
                          enum = nodePhases;
                        };
                        target = str;
                        current = str;
                        failedSystem = str;
                        job = str;
                        since = {
                          type = "string";
                          format = "date-time";
                        };
                        leaseRenewTime = str;
                        generation.type = "integer";
                        message = str;
                      };
                    };
                  };
                };
              };
            };
          };
        }
      ];
    };
  };
in
{
  config = lib.mkIf (cfg.enable && cfg.nixosHost.enable) {
    kubernetes.crds = [ crd ];

    kubernetes.resources.none = {
      ClusterRole.${name} = {
        metadata.labels = labels;
        rules = [
          {
            apiGroups = [ group ];
            resources = [ "nixosrollouts" ];
            verbs = [
              "get"
              "list"
              "watch"
            ];
          }
          {
            apiGroups = [ group ];
            resources = [ "nixosrollouts/status" ];
            verbs = [ "patch" ];
          }
          # Patch for the cordon after a revert.
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
          # The kubelet's own heartbeat, in kube-node-lease: a renewal after
          # the switch confirms it.
          {
            apiGroups = [ "coordination.k8s.io" ];
            resources = [ "leases" ];
            verbs = [
              "get"
              "list"
              "watch"
            ];
          }
          {
            apiGroups = [ "events.k8s.io" ];
            resources = [ "events" ];
            verbs = [
              "create"
              "patch"
            ];
          }
        ];
      };
      ClusterRoleBinding.${name} = {
        metadata.labels = labels;
        subjects = lib.mkNamedList {
          ${name} = {
            kind = "ServiceAccount";
            namespace = cfg.namespace;
          };
        };
        roleRef = {
          kind = "ClusterRole";
          inherit name;
          apiGroup = "rbac.authorization.k8s.io";
        };
      };
    };

    kubernetes.resources.${cfg.namespace} = {
      ServiceAccount.${name}.metadata.labels = labels;

      Role.${name} = {
        metadata.labels = labels;
        rules = [
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
          # Not limited by resourceNames: kr8s gets an object by name with a
          # list and a field selector, and resourceNames does not cover list.
          {
            apiGroups = [ "" ];
            resources = [ "podtemplates" ];
            verbs = [
              "get"
              "list"
            ];
          }
          # A failed Job's log goes into the node's status message.
          {
            apiGroups = [ "" ];
            resources = [ "pods" ];
            verbs = [ "list" ];
          }
          {
            apiGroups = [ "" ];
            resources = [ "pods/log" ];
            verbs = [ "get" ];
          }
        ];
      };
      RoleBinding.${name} = {
        metadata.labels = labels;
        subjects = lib.mkNamedList {
          ${name}.kind = "ServiceAccount";
        };
        roleRef = {
          kind = "Role";
          inherit name;
          apiGroup = "rbac.authorization.k8s.io";
        };
      };

      Deployment.${name} = {
        metadata.labels = labels;
        spec = {
          # One controller, and never two during a rollout of this
          # Deployment: two would race on the same node's Jobs.
          replicas = 1;
          strategy.type = "Recreate";
          selector.matchLabels = matchLabels;
          template = {
            metadata.labels = labels;
            spec = {
              serviceAccountName = name;
              containers = lib.mkNamedList {
                controller = {
                  image = cfg.images.nix;
                  inherit (cfg) imagePullPolicy;
                  command = [ "nixkube-nixos-rollout" ];
                  env = lib.mkNamedList {
                    NAMESPACE.valueFrom.fieldRef.fieldPath = "metadata.namespace";
                    REVERT_DELAY.value = toString cfg.nixosHost.revertDelay;
                  };
                  resources.requests = {
                    memory = "64Mi";
                    cpu = "10m";
                  };
                };
              };
            };
          };
        };
      };
    };
  };
}
