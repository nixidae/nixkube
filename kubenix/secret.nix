# SPDX-License-Identifier: Apache-2.0

{
  config,
  lib,
  curPkgs,
  ...
}:
let
  cfg = config.nixkube;
in
{
  config = lib.mkIf cfg.enable {
    kubernetes.resources.${cfg.namespace} = {
      ConfigMap.ssh-config = {
        metadata.labels = cfg.labels;
        data = {
          # Keys that can connect to us
          "authorized_keys" = lib.concatLines cfg.pynixd.authorizedKeys;
          # Keys that connect to us as trusted clients
          "admin_keys" = lib.concatLines cfg.pynixd.adminKeys;
          # Keys that we can connect to
          "ssh_known_hosts" = lib.concatLines (lib.mapAttrsToList (n: v: "${n} ${v}") cfg.knownHosts);
          # Client configuration
          "ssh_config" = ''
            GlobalKnownHostsFile /etc/ssh/ssh_known_hosts /etc/ssh-dynauth/ssh_known_hosts
            WarnWeakCrypto no-pq-kex

            Host pynixd
                User nix
                Port 22
                IdentityFile /etc/ssh-key/id_ed25519
                IdentitiesOnly yes
                StrictHostKeyChecking yes

            Host *
                User nix
                IdentityFile /etc/ssh-key/id_ed25519
                IdentitiesOnly yes
                StrictHostKeyChecking yes
          '';
        };
      };
      ConfigMap.init-scripts = {
        metadata.labels = cfg.labels;
        data.init-secrets = # bash
          ''
            set -x
            mkdir -p /tmp/{ssh-key,nix-key,ssh-auth}
            if ! kubectl get secret ssh-key &>/dev/null || ! kubectl get configmap ssh-dynauth &>/dev/null; then
              # Create ssh secret
              ssh-keygen -t ed25519 -C "" -N "" -f /tmp/ssh-key/id_ed25519
              kubectl delete --ignore-not-found secret ssh-key
              kubectl create secret generic ssh-key --from-file=/tmp/ssh-key

              # Create pubkey configmap
              cp /tmp/ssh-key/id_ed25519.pub /tmp/ssh-auth/authorized_keys
              echo "* $(cat /tmp/ssh-key/id_ed25519.pub)" > /tmp/ssh-auth/ssh_known_hosts
              kubectl delete --ignore-not-found configmap ssh-dynauth
              kubectl create configmap ssh-dynauth --from-file=/tmp/ssh-auth
            fi

            if ! kubectl get secret nix-key &>/dev/null; then
              # create nix binary cache key
              nix-store --generate-binary-cache-key \
                nix-cache-1 \
                /tmp/nix-key/nix_ed25519 \
                /tmp/nix-key/nix_ed25519.pub
              kubectl delete --ignore-not-found secret nix-key
              kubectl create secret generic nix-key --from-file=/tmp/nix-key
            fi
          '';
      };
      Job.init =
        let
          labels = cfg.labels // {
            "app.kubernetes.io/component" = "init";
          };
        in
        {
          metadata.labels = labels;
          metadata.annotations = {
            "kluctl.io/hook" = "pre-deploy";
            "kluctl.io/hook-delete-policy" = "hook-succeeded"; # seems flaky
            "kluctl.io/hook-wait" = "false"; # true fails CI since other resources aren't deployed yet
          };
          spec = {
            # ttlSecondsAfterFinished = 0; # remove job when it's done
            template = {
              metadata.labels = labels;
              spec = {
                restartPolicy = "OnFailure";
                serviceAccountName = "nixkube";
                containers = lib.mkNamedList {
                  init = {
                    image = cfg.images.nix;
                    inherit (cfg) imagePullPolicy;
                    command = [ "init-secrets" ];
                    volumeMounts = lib.mkNamedList {
                      nix-config.mountPath = "/etc/nix";
                      init-scripts.mountPath = "/opt/bin";
                    };
                  };
                };
                volumes = lib.mkNamedList {
                  nix-config.configMap.name = "nix-node";
                  init-scripts.configMap.name = "init-scripts";
                };
              };
            };
          };
        };
    };
  };
}
