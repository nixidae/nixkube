{
  pkgs,
  lib,
  kubenixInstance,
  sources,
}:
let
  type = import ../kubenix/configurationFileType.nix { inherit lib; };
  read = import ../kubenix/readConfigurationFile.nix;
  poison = throw "An unused configuration representation is evaluated.";
  custom = (pkgs.writeText "custom-nix.conf" "max-jobs = 2\n") // {
    text = poison;
  };
  evaluate =
    modules:
    (lib.evalModules {
      modules = [
        {
          options.conf = lib.mkOption { inherit type; };
          config.conf = lib.mkDefault {
            file = poison;
            text = "default = true\n";
          };
        }
      ]
      ++ modules;
    }).config.conf;
  ordinary = evaluate [ { conf = custom; } ];
  forced = evaluate [ { conf = lib.mkForce custom; } ];
  fileOnly = evaluate [ { conf.file = custom; } ];
  explicit = evaluate [
    {
      conf = {
        file = poison;
        text = "explicit = true\n";
      };
    }
  ];
  normal = evaluate [
    {
      conf = {
        file = custom;
        text = poison;
      };
    }
  ];
  actualType = import ../kubenix/nixOptions.nix { inherit pkgs; };
  generated =
    (lib.evalModules {
      modules = [
        {
          options.conf = lib.mkOption { type = actualType; };
          config.conf = {
            settings = {
              max-jobs = 2;
              keep-outputs = true;
            };
            extraOptions = "# configuration preview check";
          };
        }
      ];
    }).config.conf.nixConf;
  base = kubenixInstance { };
  instance =
    enabled: closure:
    base.passthru.eval.extendModules {
      specialArgs.ekn = base.passthru.eval._module.args.ekn // {
        lib = base.passthru.eval._module.args.ekn.lib // {
          packageRef = (import sources.easykubenix { inherit pkgs sources; }).lib.packageRef {
            inherit enabled;
          };
        };
      };
      modules = [
        {
          _module.args.csiPkgs = lib.mkForce {
            ${pkgs.stdenv.hostPlatform.system} = {
              nixkube-node-env = closure;
              nixkube-pynixd-env = closure;
            };
          };
          nixkube.systems = {
            ${pkgs.stdenv.hostPlatform.system} = true;
          };
        }
      ];
    };
  preview = instance true poison;
  contextPackage = pkgs.writeText "configuration-preview-closure" "closure";
  normalResources = (instance false contextPackage).config.kubernetes.resources.nixkube;
  builderVolume =
    resources:
    (lib.findFirst (
      volume: volume.name == "nix-store"
    ) (throw "Missing builder store.") resources.PodTemplate.nixkube-builder.template.spec.volumes)
    .csi.volumeAttributes.${pkgs.stdenv.hostPlatform.system};
  resources = preview.config.kubernetes.resources.nixkube;
  expected = "/nix/store/eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee-nixkube-pynixd-env-${pkgs.stdenv.hostPlatform.system}";
  node = resources.DaemonSet.nix-node.spec.template.spec;
  cache = resources.StatefulSet.pynixd.spec.template.spec;
  wanted =
    pod:
    (lib.findFirst (
      env: env.name == "APPSTARTER_WANTED"
    ) (throw "Missing closure reference.") (lib.head pod.initContainers).env).value;
  tests = {
    previewDoesNotForceFile = read true (evaluate [ ]) == "default = true\n";
    normalDoesNotForceText = read false normal == "max-jobs = 2\n";
    ordinaryPackageOverride = ordinary.text == null && read true ordinary == "max-jobs = 2\n";
    forcedPackageOverride = forced.text == null && read true forced == "max-jobs = 2\n";
    fileOnlyOverrideClearsText = fileOnly.text == null && read true fileOnly == "max-jobs = 2\n";
    explicitTextDoesNotForceFile = read true explicit == "explicit = true\n";
    generatedTextMatchesValidatedFile = read true generated == read false generated;
    settingsReachText =
      lib.hasInfix "max-jobs = 2" generated.text && lib.hasInfix "keep-outputs = true" generated.text;
    extraOptionsReachText = lib.hasInfix "# configuration preview check" generated.text;
    normalBuilderKeepsContext =
      builtins.getContext (builderVolume normalResources) == builtins.getContext "${contextPackage}";
    nodeClosureIsDeferred = lib.hasInfix "eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee-nixkube-node-env-" (
      wanted node
    );
    cacheClosureIsDeferred = lib.hasInfix expected (wanted cache);
    builderClosureIsDeferred =
      (lib.findFirst (
        volume: volume.name == "nix-store"
      ) (throw "Missing builder store.") resources.PodTemplate.nixkube-builder.template.spec.volumes)
      .csi.volumeAttributes.${pkgs.stdenv.hostPlatform.system} == expected;
  };
in
assert lib.all (result: result) (builtins.attrValues tests);
pkgs.runCommand "nixkube-check-configuration-preview" { results = builtins.toJSON tests; } ''
  printf '%s\n' "$results" > "$out"
''
