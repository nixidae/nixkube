# SPDX-License-Identifier: Apache-2.0

/*
  NixOSRollout (#359): a cluster-scoped object that names the system each
  selected NixOS node runs, and the controller that switches the nodes one at
  a time through Jobs made from the `nixkube-nixos-host` PodTemplate.

  A rollout names either `systems`, prebuilt toplevels by node, or a
  `source`: a git URL, a rev and an attribute, a function
  `{ name, labels, system }: <toplevel>` that returns the derivation to
  build. The controller calls it once per node and builds the result through
  pynixd, never looking inside a module system, and the switch Job
  copies the result from pynixd into the host's store.

  A node whose kernel, initrd or kernel modules change is cordoned and
  drained, boots the new system and reboots, then is uncordoned.

  The controller has a ServiceAccount of its own: it may create privileged
  pods in this namespace, and the node DaemonSet must not.
*/
{
  config,
  lib,
  csiPkgs,
  ekn,
  ...
}:
let
  cfg = config.nixkube;
  # The controller runs out of the store, mounted through the node's CSI
  # driver as a pynixd builder is: an apply moves it, no image does.
  storeVolumeAttributes = lib.mapAttrs (
    system: pkgs: "${ekn.lib.packageRef "nixkube-rollout-env-${system}" pkgs.nixkube-rollout-env}"
  ) csiPkgs;
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
    "Building"
    "BuildFailed"
    "Pending"
    "Conflict"
    "Switching"
    "Confirming"
    "Draining"
    "DrainFailed"
    "Booting"
    "Rebooting"
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
                required = [ "nodeSelector" ];
                oneOf = [
                  { required = [ "systems" ]; }
                  { required = [ "source" ]; }
                ];
                properties = {
                  nodeSelector = labelSelector;
                  systems = {
                    description = "The toplevel each selected node runs, by node name.";
                    type = "object";
                    additionalProperties = storePath;
                  };
                  source = {
                    description = "A git repository whose attribute is a function `{ name, labels, system }: <toplevel>`, called once per selected node; the derivation it returns is built and switched to.";
                    type = "object";
                    required = [
                      "url"
                      "rev"
                    ];
                    properties = {
                      url = str;
                      rev = {
                        type = "string";
                        pattern = "^[0-9a-f]{40}$";
                      };
                      attribute = {
                        description = "A dot-separated attribute path in the repository's default.nix; empty is the file itself.";
                        type = "string";
                        default = "";
                      };
                      secretRef = {
                        description = "A Secret in the controller's namespace for the fetch: kubernetes.io/ssh-auth (ssh-privatekey, known_hosts) or kubernetes.io/basic-auth.";
                        type = "object";
                        required = [ "name" ];
                        properties.name = str;
                      };
                    };
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
                  drainTimeout = {
                    description = "Seconds a node that must reboot has to drain. On a timeout the rollout halts before the reboot.";
                    type = "integer";
                    minimum = 1;
                    default = 300;
                  };
                  rebootTimeout = {
                    description = "Seconds a rebooting node has to come back Ready with a new boot ID.";
                    type = "integer";
                    minimum = 1;
                    default = 900;
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
                        bootID = str;
                        drained.type = "boolean";
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
          # A drain: the pods on a node, in every namespace, and their
          # eviction, which honours PodDisruptionBudgets.
          {
            apiGroups = [ "" ];
            resources = [ "pods" ];
            verbs = [ "list" ];
          }
          {
            apiGroups = [ "" ];
            resources = [ "pods/eviction" ];
            verbs = [ "create" ];
          }
          # Patch for a cordon and an uncordon.
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
            inherit (cfg) namespace;
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
          # The fetch credentials a rollout's `source.secretRef` names. kr8s
          # gets by name with a list, as above.
          {
            apiGroups = [ "" ];
            resources = [ "secrets" ];
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
                  image = cfg.images.scratch;
                  inherit (cfg) imagePullPolicy;
                  command = [
                    "/nix/var/result/bin/tini"
                    "--"
                    "/nix/var/result/bin/nixkube-rollout-controller"
                  ];
                  env = lib.mkNamedList {
                    NAMESPACE.valueFrom.fieldRef.fieldPath = "metadata.namespace";
                    REVERT_DELAY.value = toString cfg.nixosHost.revertDelay;
                    RESYNC_SECONDS.value = toString cfg.nixosHost.resyncSeconds;
                    JOB_TTL_SECONDS.value = toString cfg.nixosHost.jobTTLSeconds;
                    PYNIXD_STORE.value = "ssh-ng://nix@pynixd";
                  };
                  resources.requests = {
                    memory = "64Mi";
                    cpu = "10m";
                  };
                  # A source rollout evaluates here and builds through
                  # pynixd, as a node does.
                  volumeMounts = lib.mkNamedList {
                    nix-store = {
                      mountPath = "/nix";
                      subPath = "nix";
                    };
                    nix-config.mountPath = "/etc/nix";
                    ssh-config.mountPath = "/etc/ssh";
                    ssh-dynauth.mountPath = "/etc/ssh-dynauth";
                    ssh-key.mountPath = "/etc/ssh-key";
                  };
                };
              };
              volumes = lib.mkNamedList {
                nix-store.csi = {
                  driver = "nixkube";
                  readOnly = false;
                  volumeAttributes = storeVolumeAttributes;
                };
                nix-config.configMap.name = "nix-node";
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
              };
            };
          };
        };
      };
    };
  };
}
