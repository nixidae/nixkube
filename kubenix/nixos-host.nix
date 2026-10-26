# SPDX-License-Identifier: Apache-2.0

/*
  The pod a NixOS host switch runs in (#359). The controller makes a Job from
  this template per action and sets `nodeName`, `ACTION` and `TOPLEVEL`.

  The switch container brings no tools. It mounts the host's /nix and runs
  the host's own nsenter, then the host's own nix, systemd-run and
  switch-to-configuration in PID 1's namespaces. So its image is `scratch`,
  and a switch does not depend on the CSI driver or the node DaemonSet.

  When the controller built the toplevel, it sets `FETCH_FROM` to pynixd's
  store. The `fetch` init container, on the nix image, then copies the
  toplevel from pynixd into the host's store through the host's nix-daemon
  socket, with the pod's ssh key and nix configuration. The host's nix.conf
  need not name pynixd. Root on that socket is a trusted user, so the copy
  needs no signature the host trusts.
*/
{
  config,
  lib,
  ...
}:
let
  cfg = config.nixkube;
  labels = cfg.labels // {
    "app.kubernetes.io/component" = "nixos-host";
  };

  revertUnit = "nixkube-revert";
  profile = "/nix/var/nix/profiles/system";

  # The revert is armed before the profile moves, so a failure after this
  # point always has a way back. A switch that fails starts it at once; one
  # the controller cannot confirm is reverted by the controller, or by the
  # timer when no pod can run on the node any more.
  script = ''
    set -eu
    export PATH=/run/current-system/sw/bin
    case "$ACTION" in
    switch)
      previous=$(readlink /run/current-system)
      if [ "$previous" = "$TOPLEVEL" ]; then
        echo "already running $TOPLEVEL"
        exit 0
      fi
      if systemctl is-active --quiet ${revertUnit}.timer; then
        echo "a revert is armed: an earlier switch is not confirmed" >&2
        exit 1
      fi
      nix-store --realise "$TOPLEVEL" >/dev/null
      systemd-run --unit=${revertUnit} --on-active="$REVERT_DELAY" \
        --timer-property=AccuracySec=1s --service-type=oneshot \
        --property=KillMode=process --collect --quiet \
        "$previous/sw/bin/sh" -c \
        "$previous/sw/bin/nix-env --profile ${profile} --set $previous && exec $previous/bin/switch-to-configuration switch"
      nix-env --profile ${profile} --set "$TOPLEVEL"
      unit="nixkube-switch-$(basename "$TOPLEVEL" | cut -c1-12)"
      echo "switching to $TOPLEVEL; host journal: journalctl --unit $unit"
      if ! "$TOPLEVEL/sw/bin/systemd-run" --unit="$unit" --wait --collect --quiet \
        --service-type=exec --property=KillMode=process \
        "$TOPLEVEL/bin/switch-to-configuration" switch; then
        echo "switch-to-configuration failed; reverting to $previous" >&2
        systemctl start ${revertUnit}.service
        systemctl stop ${revertUnit}.timer
        exit 1
      fi
      ;;
    disarm)
      # The timer first: stopping it does not stop a revert it already
      # started, so a check before the stop can pass while the host reverts.
      # A switch to the running system arms nothing, and stop on a unit
      # that is not loaded fails.
      if systemctl is-active --quiet ${revertUnit}.timer; then
        systemctl stop ${revertUnit}.timer
      fi
      case "$(systemctl show --property=ActiveState --value ${revertUnit}.service)" in
      activating | active | deactivating)
        echo "the revert is running: the switch was not confirmed in time" >&2
        exit 1
        ;;
      esac
      # The timer may have reverted the host before the switch Job could
      # report, and then a confirmed kubelet runs the old system.
      running=$(readlink /run/current-system)
      if [ "$running" != "$TOPLEVEL" ]; then
        echo "running $running, not $TOPLEVEL: the switch was reverted" >&2
        exit 1
      fi
      ;;
    revert)
      if ! systemctl is-active --quiet ${revertUnit}.timer; then
        echo "no revert is armed" >&2
        exit 1
      fi
      systemctl start ${revertUnit}.service
      systemctl stop ${revertUnit}.timer
      ;;
    *)
      echo "unknown ACTION: $ACTION" >&2
      exit 2
      ;;
    esac
  '';
in
{
  config = lib.mkIf (cfg.enable && cfg.nixosHost.enable) {
    kubernetes.resources.${cfg.namespace}.PodTemplate.nixkube-nixos-host = {
      metadata.labels = labels;
      template = {
        metadata.labels = labels;
        # `TOPLEVEL` is a store path in fetch's environment, and nixkube's NRI
        # plugin would realise it on the node before the container starts:
        # the path fetch exists to bring.
        metadata.annotations."nixkube/fetch-exclude" = "true";
        spec = {
          restartPolicy = "Never";
          hostPID = true;
          automountServiceAccountToken = false;
          # `nodeName` skips the scheduler, so a cordon does not stop the
          # pod; a NoExecute taint still would.
          tolerations = [ { operator = "Exists"; } ];
          initContainers = lib.mkNamedList {
            fetch = {
              image = cfg.images.nix;
              inherit (cfg) imagePullPolicy;
              command = [ "nixkube-host-fetch" ];
              env = lib.mkNamedList {
                ACTION.value = "switch";
                TOPLEVEL.value = "";
                FETCH_FROM.value = "";
              };
              volumeMounts = lib.mkNamedList {
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
          containers = lib.mkNamedList {
            switch = {
              image = cfg.images.scratch;
              inherit (cfg) imagePullPolicy;
              # The host's /run, through PID 1's root: the container's /run
              # is its own, and a host booted from an image may have no
              # system profile. The binary's store paths resolve in the
              # mounted /nix.
              command = [
                "/proc/1/root/run/current-system/sw/bin/nsenter"
                "--target=1"
                "--all"
                "--"
                "/run/current-system/sw/bin/sh"
                "-c"
                script
              ];
              env = lib.mkNamedList {
                ACTION.value = "switch";
                TOPLEVEL.value = "";
                REVERT_DELAY.value = toString cfg.nixosHost.revertDelay;
              };
              securityContext.privileged = true;
              volumeMounts = lib.mkNamedList {
                host-nix = {
                  mountPath = "/nix";
                  readOnly = true;
                };
              };
            };
          };
          volumes = lib.mkNamedList {
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
