# SPDX-License-Identifier: Apache-2.0

/*
  The pod a NixOS host switch runs in (#359). The controller makes a Job from
  this template per action and sets `nodeName`, `ACTION`, `TOPLEVEL` and
  `FETCH_FROM`.

  Its one container runs `nixkube-nixos-switch` (pkgs/nixkube/src/nixos_switch.py)
  out of the store the CSI driver mounts, as the controller runs. The program
  copies its own closure into the host's store, and the toplevel too when
  `FETCH_FROM` names the store the controller built it in. Then it enters PID
  1's namespaces and switches with the host's own tools, still running from
  the same store paths, now the host's.

  The copies go through the host's nix-daemon socket, with the pod's ssh key
  and nix configuration: the host's nix.conf need not name pynixd. Root on that
  socket is a trusted user, so a copy needs no signature the host trusts.
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
  labels = cfg.labels // {
    "app.kubernetes.io/component" = "nixos-host";
  };
  storeVolumeAttributes = lib.mapAttrs (
    system: pkgs:
    "${ekn.lib.packageRef "nixkube-nixos-switch-env-${system}" pkgs.nixkube-nixos-switch-env}"
  ) csiPkgs;
in
{
  config = lib.mkIf (cfg.enable && cfg.nixosHost.enable) {
    kubernetes.resources.${cfg.namespace}.PodTemplate.nixkube-nixos-host = {
      metadata.labels = labels;
      template = {
        metadata.labels = labels;
        # `TOPLEVEL` is a store path in the environment. Without this, the
        # CSI driver fetches it into the volume and NRI into the node's
        # store, before the container starts: the path the program exists to
        # bring, which a prebuilt rollout's host may hold alone.
        metadata.annotations."nixkube/pod-exclude" = "true";
        spec = {
          restartPolicy = "Never";
          # /proc/1 is the host's init, whose namespaces the program joins.
          hostPID = true;
          automountServiceAccountToken = false;
          # `nodeName` skips the scheduler, so a cordon does not stop the
          # pod; a NoExecute taint still would.
          tolerations = [ { operator = "Exists"; } ];
          containers = lib.mkNamedList {
            switch = {
              image = cfg.images.scratch;
              inherit (cfg) imagePullPolicy;
              command = [ "/nix/var/result/bin/nixkube-nixos-switch" ];
              env = lib.mkNamedList {
                ACTION.value = "switch";
                TOPLEVEL.value = "";
                FETCH_FROM.value = "";
                REVERT_DELAY.value = toString cfg.nixosHost.revertDelay;
              };
              securityContext.privileged = true;
              volumeMounts = lib.mkNamedList {
                nix-store = {
                  mountPath = "/nix";
                  subPath = "nix";
                };
                # Read-only: the host's daemon writes the paths.
                host-nix = {
                  mountPath = "/host/nix";
                  readOnly = true;
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
            host-nix.hostPath = {
              path = "/nix";
              type = "Directory";
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
}
