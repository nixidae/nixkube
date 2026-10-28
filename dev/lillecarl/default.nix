rec {
  default = import ../../. { };
  inherit (default) pkgs;
  inherit (pkgs) lib;

  hostname = "pynixd.lillecarl.com";

  easykube = default.easykubenix {
    inherit (default) pkgs;
    modules = [
      ../../kubenix
      (
        { config, lib, ... }:
        {
          config = {
            kubernetes.objects.nixkube = {
              Service.pynixd-lb.metadata.annotations = {
                "external-dns.alpha.kubernetes.io/hostname" = hostname;
                "external-dns.alpha.kubernetes.io/ttl" = "60";
              };
              StatefulSet.pynixd.spec.template.metadata.labels."cilium.io/ingress" = "true";
              # StatefulSet.pynixd.spec.volumeClaimTemplates."1".spec.resources.requests.storage = lib.mkForce "20Gi";
            };
            kluctl = {
              preDeployScript = # bash
                ''
                  expected_context="hetzkube"
                  current_context=$(kubectl config current-context)

                  if [[ "$current_context" != *"$expected_context" ]]; then
                      echo "Warning: Current context is $current_context, not *$expected_context" >&2
                      read -rp "Continue anyway? [y/N] " confirm
                      if [[ ! "$confirm" =~ ^[Yy]$ ]]; then
                          echo "Aborted." >&2
                          exit 1
                      fi
                  fi

                  ${lib.getExe pkgs.cachix} push nixkube ${config.kluctl.projectDir} || true
                  nix copy \
                    --substitute-on-destination \
                    --no-check-sigs \
                    --to ssh-ng://nix@${hostname}:2222 \
                    ${config.kluctl.projectDir} || true
                '';
            };
            nixkube = {
              enable = true;
              node.compat = false;
              cache.enable = true;
              discardStringContext = false;
              systems = {
                "x86_64-linux" = true;
                "aarch64-linux" = false;
              };
              cache.storageClassName = "hcloud-volumes";
              pynixd.trustClusterKey = true;
              pynixd.controller.nixConfig.settings.max-jobs = lib.mkForce 0;
              pynixd.adminKeys = [
                (builtins.readFile /home/lillecarl/.ssh/id_ed25519.pub)
              ];
              loggingConfig = {
                renderer = "console";
                loggers = {
                  nixkube.level = "DEBUG";
                };
              };
            };
          };
        }
      )
    ];
  };
  ITIME =
    let
      env = builtins.getEnv "ITIME";
      time = if env == "" then builtins.currentTime else env;
    in
    time;

  normalDerivation = pkgs.stdenv.mkDerivation {
    name = "normal";
    phases = [ "build" ];
    buildCommand = "echo hello > $out";
  };

  fixedOutputDerivation = pkgs.stdenv.mkDerivation {
    name = "fixed-output";
    phases = [ "build" ];
    buildCommand = "echo 'hello from fixed-output' > $out";
    outputHashMode = "flat";
    outputHashAlgo = "sha256";
    outputHash = "bd1745c8d3bb8d97faf6b7949755af7ba3fbcfb8bf5edd614c0134297082405a";
  };

  impureDerivation = pkgs.stdenv.mkDerivation {
    inherit ITIME;
    name = "impure";
    phases = [ "build" ];
    buildCommand = ''
      echo "hello world, its $ITIME o-clock" > $out
    '';
  };

  caDerivation = pkgs.stdenv.mkDerivation {
    inherit ITIME;
    name = "ca";
    __contentAddressed = true;
    outputHashMode = "nar";
    outputHashAlgo = "sha256";
    outputs = [ "out" ];
    buildCommand = ''
      echo "hello world, its $ITIME o-clock" > $out
    '';
  };

  dynamicFromNormal = pkgs.stdenv.mkDerivation {
    name = "dynamic-from-normal";
    phases = [ "build" ];
    buildCommand = ''
      cp ${builtins.unsafeDiscardOutputDependency normalDerivation.drvPath} $out
    '';
    __contentAddressed = true;
    outputHashMode = "text";
    outputHashAlgo = "sha256";
  };

  dynamicFromCa = pkgs.stdenv.mkDerivation {
    name = "dynamic-from-ca";
    phases = [ "build" ];
    buildCommand = ''
      cp ${builtins.unsafeDiscardOutputDependency caDerivation.drvPath} $out
    '';
    __contentAddressed = true;
    outputHashMode = "text";
    outputHashAlgo = "sha256";
  };

  dynamicFromFixed = pkgs.stdenv.mkDerivation {
    name = "dynamic-from-fixed";
    phases = [ "build" ];
    buildCommand = ''
      cp ${builtins.unsafeDiscardOutputDependency fixedOutputDerivation.drvPath} $out
    '';
    __contentAddressed = true;
    outputHashMode = "text";
    outputHashAlgo = "sha256";
  };

  testClusterRun = pkgs.writeShellApplication {
    name = "testClusterRun";
    runtimeInputs = [
      pkgs.kubectl
    ];
    text =
      let
        config = easykube.eval.config;
        ns = config.nixkube.namespace;
        inherit ((builtins.unsafeGetAttrPos "x" { x = "y"; })) file;
        common = "--file ${file} --no-link --print-out-paths --max-jobs 0 --print-build-logs";
        storeUri = "ssh-ng://nix@${hostname}:2222";
        builderUri = ''"${storeUri} x86_64-linux - 10 1 ca-derivations,dynamic-derivations,recursive-nix - -"'';
        targets = [
          "normalDerivation"
          "fixedOutputDerivation"
          "impureDerivation"
          "caDerivation"
          "dynamicFromNormal"
          "dynamicFromCa"
          "dynamicFromFixed"
        ];
        testBuild = target: ''
          timeout 60 nix build ${common} --builders ${builderUri} ${target}
          timeout 60 nix build ${common} --store ${storeUri} --eval-store auto ${target}
        '';
        allTests = lib.concatStringsSep "\n" (map testBuild targets);
      in
      ''
        set -x
        ${lib.getExe easykube.deploymentScript} --yes
        kubectl --namespace ${ns} rollout status --watch --timeout=60s statefulset/pynixd daemonset/nix-node

        ${allTests}
      '';
  };
}
