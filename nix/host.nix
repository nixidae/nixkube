# What a NixOS host that a NixOSRollout reboots imports. Issue #1097.
{
  config,
  lib,
  pkgs,
  ...
}:
let
  cfg = config.nixkube.host.bootAssessment;

  # systemd-boot sets this variable only when it boots an entry with a try
  # counter. systemd-bless-boot.service has the same condition.
  counted = "/sys/firmware/efi/efivars/LoaderBootCountPath-4a67b082-0a4c-41cf-b6c7-440b29bb8c4f";
in
{
  options.nixkube.host.bootAssessment = {
    enable = lib.mkEnableOption ''
      a boot assessment that counts a boot as good only once this host's
      Node is Ready.

      It turns on systemd-boot's `bootCounting`. A new generation then gets
      a few tries, and systemd-bless-boot marks it good when
      `boot-complete.target` is reached. This adds a unit that
      `boot-complete.target` requires: it waits until the Node reports this
      boot's boot ID and Ready, and else fails and reboots the host. With
      no tries left, systemd-boot boots the newest good generation.

      A NixOSRollout that reboots this node then sees it come back on its
      old system. Its disarm Job fails and the rollout halts, with the node
      cordoned. Set the rollout's `rebootTimeout` above
      `tries × (boot time + timeout)`, or it halts before the fallback.

      A boot that hangs is a try only if something resets the machine: give
      the host a hardware watchdog and set
      `systemd.settings.Manager.RuntimeWatchdogSec`.
    '';

    kubeconfig = lib.mkOption {
      type = lib.types.str;
      default = "/etc/kubernetes/kubelet.conf";
      description = ''
        The kubeconfig the assessment reads the Node with. The kubelet's
        own may get its Node, and is what kubeadm writes here.
      '';
    };

    nodeName = lib.mkOption {
      type = lib.types.str;
      default = config.networking.hostName;
      defaultText = lib.literalExpression "config.networking.hostName";
      description = "This host's Node.";
    };

    timeout = lib.mkOption {
      type = lib.types.ints.positive;
      default = 300;
      description = ''
        Seconds from the start of the assessment until a Node that is not
        Ready fails the boot.
      '';
    };
  };

  config = lib.mkIf cfg.enable {
    assertions = [
      {
        assertion = config.boot.loader.systemd-boot.enable;
        message = "nixkube.host.bootAssessment needs systemd-boot: only it counts boot tries.";
      }
    ];

    boot.loader.systemd-boot.bootCounting.enable = true;

    /*
      A boot ID and not Ready alone: after a reboot the Node keeps the last
      boot's Ready condition until the node lifecycle controller marks it
      Unknown. Only this boot's kubelet posts this boot's ID.

      Only on a counted boot. A good entry has no counter, so a host that
      cannot reach its apiserver, as in a cluster cold start, does not
      reboot in a loop.
    */
    systemd.services.nixkube-boot-assessment = {
      description = "Wait for this host's Node to be Ready on this boot";
      requiredBy = [ "boot-complete.target" ];
      before = [ "boot-complete.target" ];
      after = [ "network-online.target" ];
      wants = [ "network-online.target" ];
      unitConfig = {
        ConditionPathExists = counted;
        FailureAction = "reboot";
      };
      serviceConfig = {
        Type = "oneshot";
        RemainAfterExit = true;
        TimeoutStartSec = cfg.timeout;
      };
      path = [
        pkgs.kubectl
        pkgs.jq
      ];
      script = ''
        boot=$(cat /proc/sys/kernel/random/boot_id)
        until kubectl --kubeconfig ${lib.escapeShellArg cfg.kubeconfig} get node ${lib.escapeShellArg cfg.nodeName} --output json 2>/dev/null \
          | jq --exit-status --arg boot "$boot" '
              .status.nodeInfo.bootID == $boot
              and any(.status.conditions[]; .type == "Ready" and .status == "True")' >/dev/null; do
          sleep 2
        done
      '';
    };
  };
}
