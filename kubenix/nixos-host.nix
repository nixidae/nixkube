# SPDX-License-Identifier: Apache-2.0

/*
  The pod a NixOS host switch runs in (#359). The controller makes a Job from
  this template per action and sets `nodeName`, `ACTION` and `TOPLEVEL`.

  The pod brings no tools. It mounts the host's /nix and runs the host's own
  nsenter, then the host's own nix, systemd-run and switch-to-configuration
  in PID 1's namespaces. So the image is `scratch`, and a switch does not
  depend on the CSI driver or the node DaemonSet.
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
      systemctl stop ${revertUnit}.timer
      ;;
    revert)
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
        spec = {
          restartPolicy = "Never";
          hostPID = true;
          automountServiceAccountToken = false;
          # `nodeName` skips the scheduler, so a cordon does not stop the
          # pod; a NoExecute taint still would.
          tolerations = [ { operator = "Exists"; } ];
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
          };
        };
      };
    };
  };
}
