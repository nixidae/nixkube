# SPDX-License-Identifier: Apache-2.0

{
  # Where every dependency lives, as directories. nix/sources.nix says how
  # this repository finds the umbrella that owns them.
  sources ? import ./nix/sources.nix,
  system ? builtins.currentSystem,
  # The digest `nixImage.tagRelease` printed, for `kubenixRelease`. Only the
  # release job passes it, with `--argstr`.
  releaseDigest ? null,
}:
rec {
  pkgs = import sources.nixpkgs {
    inherit system;
    config = {
      allowUnfree = true;
    };
    overlays = [
      (final: prev: { nixkubeSources = sources; })
      (import ./pkgs)
    ];
  };
  inherit sources;
  lib = pkgs.lib;

  # This repository's Python projects, built by pyproject.nix rather than by
  # nixpkgs' Python builders. nix/python-set.nix says why. Issue #50.
  inherit (pkgs) pythonSet;
  pythonTests = import ./nix/tests.nix { inherit lib pkgs pythonSet; };

  easykubenix = import sources.easykubenix;

  kubenixApply = kubenixInstance { };

  # The manifest a release publishes: `kubenixApply`, with the init image
  # pinned by the digest of its index rather than named by a tag (#825).
  kubenixRelease =
    assert lib.assertMsg (
      releaseDigest != null
    ) "kubenixRelease needs --argstr releaseDigest sha256:...";
    kubenixInstance {
      module.nixkube.images.nix = "${nixImage.repo}/nix@${releaseDigest}";
    };

  # The two CI deployments as module lists, because each one is deployed
  # twice: once as itself, and once by a guest test that adds a single
  # setting. Naming the list is what stops the guest's copy drifting from
  # what CI deploys -- see `kubenixCIGuest` below.
  ciModules = {
    one = [
      ./kubenix/ci
      {
        nixkube.systems = {
          x86_64-linux = true;
          aarch64-linux = false;
        };
      }
    ];
    two = [
      ./kubenix/ci
      {
        nixkube.pynixd.enable = false;
        nixkube.systems = {
          x86_64-linux = true;
          aarch64-linux = false;
        };
      }
    ];
  };

  /*
    The same deployment, adapted to a guest.

    Two settings: nixkube's own image comes from this checkout rather than
    from ghcr.io, and pynixd's claim fits the guest's disk.
    ./nix/uml/ci-guest.nix carries the reason and the measurement for each.

    The rest is the list above, so this cannot become a second definition
    of what CI deploys.
  */
  kubenixCIGuest =
    modules:
    kubenixInstance {
      module.imports = modules ++ [ ./nix/uml/ci-guest.nix ];
    };

  kubenixCI1 = kubenixInstance { module.imports = ciModules.one; };
  # Without pynixd; `ciTest` deploys it. Disables aarch64-linux to avoid
  # needing cross-compilation support.
  kubenixCI2 = kubenixInstance { module.imports = ciModules.two; };
  # Separate instance for test workload Jobs, deployed after infrastructure
  # is fully rolled out so CSI and NRI are ready before pods start.
  kubenixCITest = easykubenix {
    inherit pkgs sources;
    modules = [
      ./kubenix
      ./kubenix/ci/test-workloads.nix
      {
        _module.args.sources = sources;
      }
      {
        ekn.environment = "nixkube-test";
        nixkube.enable = false;
      }
    ];
  };

  kubenixLocal = kubenixInstance {
    module.imports = [
      ./kubenix/ci
      (
        { config, ... }:
        {
          kluctl.preDeployScript = # bash
            ''
              expected_context="kind"
              current_context=$(kubectl config current-context)

              if [[ "$current_context" != *"$expected_context" ]]; then
                  echo "Warning: Current context is $current_context, not *$expected_context"* >&2
                  read -rp "Continue anyway? [y/N] " confirm
                  if [[ ! "$confirm" =~ ^[Yy]$ ]]; then
                      echo "Aborted." >&2
                      exit 1
                  fi
              fi
              cachix push nixkube ${config.internal.manifestJSONFile}
            '';
          nixkube.pynixd.enable = true;
          nixkube.discardStringContext = false;
        }
      )
    ];
  };
  kubenixPush = kubenixInstance {
    module.config = {
      nixkube.discardStringContext = false;
      nixkube.systems = {
        ${builtins.currentSystem} = true;
      };
    };
  };
  kubenixPushBoth = kubenixInstance {
    module.config = {
      nixkube.discardStringContext = false;
      nixkube.systems = {
        x86_64-linux = true;
        aarch64-linux = true;
      };
    };
  };
  # One rendered object by name, so a test forces only its object checks.
  renderedObject =
    instance: name:
    lib.findFirst (
      object: object.metadata.name == name
    ) (throw "no rendered object ${name}") instance.config.kubernetes.generated;
  kubenixInstance =
    {
      module ? { },
    }:
    easykubenix {
      inherit pkgs sources;
      modules = [
        module
        ./kubenix
        {
          _module.args.sources = sources;
        }
        {
          config = {
            # Disabled by default so you can include the module in an easykubenix project
            nixkube.enable = true;
            # The architecture rendering the manifest, which is this
            # evaluation's own. The module cannot ask for it: `hostSystem`
            # has to stay answerable under `--pure-eval`, and this file is
            # the impure entry point where `system` is already a parameter.
            nixkube.hostSystem = system;
            # Allow easily adding your pubkeys to the cache
            nixkube.pynixd.authorizedKeys = lib.pipe (lib.filesystem.listFilesRecursive ./keys) [
              (lib.filter (name: lib.hasSuffix ".pub" name))
              (lib.map (name: builtins.readFile name))
              (lib.map (key: lib.trim key))
            ];
          };
        }
      ];
    };

  push =
    pkgs.writeScriptBin "push" # bash
      ''
        #! ${pkgs.runtimeShell}
        export PATH=${lib.makeBinPath [ pkgs.cachix ]}:$PATH
        # ${lib.concatStrings (lib.attrValues sources)}
        #
        # The runtime closure, and not the derivation closure with its outputs.
        #
        # `-qR --include-outputs` on the .drv uploads every build-time
        # dependency's output as well: compilers, sources, intermediate
        # results. A node needs none of them. That was there to speed up
        # rebuilds while nixkube carried a patched Nix, and nixkube no longer
        # overrides Nix at all, so the reason has gone.
        #
        # Measured on the aarch64 side, which is where the cost showed:
        #
        #   derivation closure   1270 paths   1197 absent from cachix   4.91 GiB
        #   runtime closure       328 paths    298 absent               1.37 GiB
        #
        # cachix compresses each path with zstd on the runner. Five gigabytes
        # of that on a 4-core arm runner is what "the hosted runner lost
        # communication with the server ... starves it for CPU/Memory" looks
        # like, and build-arm64 died that way twice. amd64 never did, because
        # its build-time outputs went to cachix over months of runs and were
        # skipped. See issue #21.
        nix-store -qR ${kubenixPush.deploymentScript} | cachix push nixkube
      '';

  # Push environments for both x86_64-linux and aarch64-linux to cachix.
  #
  # Needs a machine that can build both, so it is a hand-run script and not a
  # CI step: CI builds each architecture on a runner of that architecture
  # instead (`build-amd64`, `build-arm64`), which is what `push` above is for.
  # Point it at your own ssh builder if you need it.
  push-env =
    pkgs.writeScriptBin "push-env" # bash
      ''
        #! ${pkgs.runtimeShell}
        export PATH=${lib.makeBinPath [ pkgs.cachix ]}:$PATH
        # ${lib.concatStrings (lib.attrValues sources)}
        # The runtime closure, for the reason given on `push` above.
        nix-store -qR ${kubenixPushBoth.deploymentScript} | cachix push nixkube
      '';

  # Publish both `nix:` images and the multi-arch manifest, by hand.
  #
  # `build-arm64` cannot do this. It stalls part-way through substituting from
  # cachix, on a warm cache and a clean runner, and issue #21 has the
  # elimination list. Until that is understood, this is how a `nix:` tag gets
  # published, and it takes about two minutes.
  #
  # Needs a machine that can build both architectures, exactly like `push-env`.
  # Point Nix at a builder for the one it cannot do itself:
  #
  #   nix run --file . push-images --builders "@$HOME/tmp/builders"
  #
  # REPO_USERNAME and REPO_TOKEN must be set, and the token needs
  # `write:packages`. A `gh` token does not have that by default:
  #
  #   gh auth refresh -s write:packages
  #   REPO_USERNAME=<user> REPO_TOKEN="$(gh auth token)" nix run --file . push-images
  #
  # The manifest step needs both per-architecture tags to exist already, which
  # is why they are pushed first and in order.
  push-images = pkgs.writeShellApplication {
    name = "push-images";
    text = ''
      : "''${REPO_USERNAME:?set REPO_USERNAME}"
      : "''${REPO_TOKEN:?set REPO_TOKEN, with write:packages}"
      ${lib.getExe' nixImage.pushArch.x86_64-linux "push-nix-x86_64-linux"}
      ${lib.getExe' nixImage.pushArch.aarch64-linux "push-nix-aarch64-linux"}
      ${lib.getExe' nixImage.pushManifest "push-nix-manifest"}
    '';
  };

  # The module options as CommonMark. `genModDoc` writes it into the tree and
  # `docOptionsCheck` compares the tree against it, so both read one binding.
  optionsDocs = pkgs.nixosOptionsDoc {
    inherit (kubenixCI1) options;
    warningsAreErrors = false;
    transformOptions =
      opt:
      opt
      // {
        # Remove internal options, modify declarations, etc.
        visible =
          opt.visible or true && (lib.hasPrefix "nix-csi" opt.name || lib.hasPrefix "nixkube" opt.name);
        declarations = map (
          decl:
          let
            prefix = builtins.toString ./.;
          in
          if lib.hasPrefix prefix (toString decl) then lib.removePrefix prefix (toString decl) else decl
        ) opt.declarations;
      };
  };

  genModDoc =
    pkgs.writeScriptBin "genModDoc" # bash
      ''
        #! ${pkgs.runtimeShell}
        set -euo pipefail
        # GIT_ROOT comes from the dev shell. Without it this wrote to
        # `/doc/options.md`, which fails with "No such file or directory" and,
        # unset `-e`, still exited 0. Ask git when the variable is absent, so
        # `nix run --file . genModDoc` works from any directory in the
        # checkout and from no shell at all.
        root="''${GIT_ROOT:-$(${lib.getExe pkgs.git} rev-parse --show-toplevel)}"
        # In the nixidae monorepo the checkout's root holds this source.
        if [ -d "$root/nixkube/doc" ]; then
          root="$root/nixkube"
        fi
        cp --no-preserve=mode ${optionsDocs.optionsCommonMark} "$root/doc/options.md"
      '';

  # Evaluate the hostPath+subPath check against a container that has no
  # volumeMounts at all.
  #
  # easykubenix renders through typed submodules whose optional fields default
  # to null, so such a container has `volumeMounts = null`, not a missing
  # attribute. `x or [ ]` does not catch that, and the assertion crashed on a
  # consumer tree with "expected a list but found null".
  #
  # Every container nixkube declares has volumeMounts, so none of the six
  # instances above can produce the shape. This attribute does, on purpose.
  /*
    `metrics.podMonitor` emits one object, and turns the other path off.

    The two discovery mechanisms are mutually exclusive in practice and the
    failure is silent both ways round: an operator ignores the
    `prometheus.io/*` annotations, so a cluster with only those scrapes
    nothing and looks configured; a cluster with both scrapes every pod twice.

    The named port matters as much as the object. A PodMonitor selecting
    `port = "metrics"` against a container that declares no such name is
    accepted by the apiserver and scrapes nothing. Issue #40.
  */
  metricsPodMonitorShape =
    let
      render =
        podMonitor:
        (kubenixInstance {
          module = {
            nixkube.metrics.podMonitor = podMonitor;
          };
        }).config;
      off = render false;
      on = render true;
      nsOf = cfg: cfg.kubernetes.resources.nixkube;
      podOf = cfg: (nsOf cfg).DaemonSet.nix-node.spec.template;
      nodeContainer = cfg: lib.head (lib.filter (c: c.name == "nix-node") (podOf cfg).spec.containers);
      annotated = cfg: (podOf cfg).metadata.annotations ? "prometheus.io/scrape";
      monitor = (nsOf on).PodMonitor.nixkube;
    in
    assert !((nsOf off) ? PodMonitor);
    assert (nsOf on) ? PodMonitor;
    assert annotated off;
    assert !(annotated on);
    assert monitor.spec.selector.matchLabels."app.kubernetes.io/component" == "node";
    assert (lib.head monitor.spec.podMetricsEndpoints).port == "metrics";
    # The endpoint names a port, so a port of that name has to exist.
    assert lib.any (p: p.name == "metrics") (nodeContainer on).ports;
    pkgs.runCommand "metrics-podmonitor-shape" { } "echo ok > $out";

  /*
    The same shape for the pynixd pods, which are not node pods.

    `daemonset.nix`'s PodMonitor selects `component = node`, so it never
    reaches the controller or a builder. Both serve `/metrics` on the same
    aiohttp server that answers `/healthz`, and `http_metrics_no_auth`
    defaults true, so the endpoint is readable. Without these two objects it
    is readable and nothing reads it.

    The port is named `http` here and `metrics` on a node, because pynixd
    serves the binary cache, the health probe and the metrics on one port.
    The endpoint names it, so a port of that name has to exist on the
    container.
  */
  pynixdPodMonitorShape =
    let
      render =
        podMonitor:
        (kubenixInstance {
          module = {
            nixkube.pynixd.enable = true;
            nixkube.metrics.podMonitor = podMonitor;
          };
        }).config;
      off = render false;
      on = render true;
      nsOf = cfg: cfg.kubernetes.resources.nixkube;
      podOf = cfg: (nsOf cfg).StatefulSet.pynixd.spec.template;
      pynixdContainer = cfg: lib.head (lib.filter (c: c.name == "pynixd") (podOf cfg).spec.containers);
      annotated = cfg: (podOf cfg).metadata.annotations ? "prometheus.io/scrape";
      monitors = (nsOf on).PodMonitor;
    in
    assert !((nsOf off) ? PodMonitor);
    assert annotated off;
    assert !(annotated on);
    assert monitors ? pynixd;
    # A builder is a separate object: it is short-lived, and mixing it with
    # the controller's series would hide which one stopped answering.
    assert monitors ? pynixd-builder;
    assert monitors.pynixd.spec.selector.matchLabels."app.kubernetes.io/component" == "pynixd";
    assert (lib.head monitors.pynixd.spec.podMetricsEndpoints).port == "http";
    assert lib.any (p: p.name == "http") (pynixdContainer on).ports;
    pkgs.runCommand "pynixd-podmonitor-shape" { } "echo ok > $out";

  /*
    Both pynixd config maps bind every interface, not IPv4 alone.

    The probes are `httpGet` on the HTTP port. A single-stack IPv6 cluster
    gives the Pod one v6 address, the kubelet dials that, and an
    IPv4-only bind answers ECONNREFUSED -- the probe never passes and the
    container restarts for ever. Measured on nixlab2: 61 failures in 14
    minutes, with `0.0.0.0:8080` and `:::22` held by the same process.

    Asserted on the rendered `config.json` rather than on the option,
    because that string is what the container reads. `pynixd` defaults
    both to the same value now; these lines are what keeps a deployment
    right against an image that does not.
  */
  pynixdBindsEveryInterface =
    let
      resources =
        (kubenixInstance {
          module.nixkube.pynixd.enable = true;
        }).config.kubernetes.resources.nixkube;
      settings = name: builtins.fromJSON resources.ConfigMap.${name}.data."config.json";
    in
    assert (settings "pynixd-config").http_host == "";
    assert (settings "pynixd-config").ssh_host == "";
    assert (settings "builder-config").http_host == "";
    assert (settings "builder-config").ssh_host == "";
    pkgs.runCommand "pynixd-binds-every-interface" { } "echo ok > $out";

  /*
    Every `appstarter-init` is exempt from NRI injection, and the exemption
    names it.

    Both pods run one, and both mount their store at `/nix-volume`, so the
    `/nix` check in `CreateContainer` covers neither. Neither can mount
    `/nix` instead: that shadows the image's own store, which is where
    `appstarter`'s fallback copy lives (issue #49).

    Without the annotation NRI reads `APPSTARTER_WANTED` out of the
    container's environment and realises those paths into the *node's* store
    through `fetch_packages`, which passes no `--store`. On the pynixd pod
    the only substituter for that closure is the pynixd this pod is starting.
    On the node it is the same store `appstarter init` is about to fill, so
    the injection does that work first and without the fallback. Measured on
    nixlab2: StartError 128 after nri-wait gave up. Issue #55, issue #27.

    The annotation keys on the container's name, so a rename breaks the
    exemption silently. This asserts the two agree, on both pods.
  */
  appstarterInitIsExemptFromInjection =
    let
      cfg = (kubenixInstance { module.nixkube.pynixd.enable = true; }).config;
      res = cfg.kubernetes.resources.nixkube;
      excluded = pod: name: pod.metadata.annotations."nixkube/${name}-exclude" or null == "true";
      hasInit = pod: lib.elem "appstarter-init" (map (c: c.name) pod.spec.initContainers);
      pynixdPod = res.StatefulSet.pynixd.spec.template;
      nodePod = res.DaemonSet.nix-node.spec.template;
    in
    assert hasInit pynixdPod;
    assert excluded pynixdPod "appstarter-init";
    # Both pods, for the same reason and not by symmetry. The node's
    # `/nix-volume` is its own store by hostPath, so injection there does
    # `appstarter init`'s work before it runs and without its fallback --
    # which is the only thing that rescues a node whose pynixd is down.
    assert hasInit nodePod;
    assert excluded nodePod "appstarter-init";
    # Neither main container is exempt. Both mount `/nix`, so
    # `CreateContainer` stands down for its own reason, and an annotation
    # here would hide a regression in that check.
    assert !(excluded pynixdPod "pynixd");
    assert !(excluded nodePod "nix-node");
    pkgs.runCommand "appstarter-init-is-exempt-from-injection" { } "echo ok > $out";

  /*
    `nixkube.hostStore.enable` splits the node DaemonSet in two, on the
    `nixkube/host` Node label. Issue #25.

    Off, there is one DaemonSet, as before. On, each DaemonSet's selector
    matches only its own pods: two DaemonSets whose selectors overlap each
    see the other's pods as theirs. The node affinities exclude each other,
    `nixkube-host-check` runs first with its own DaemonSet's mode, and the
    host DaemonSet mounts nothing under `hostMountPath`, so a NixOS node
    never gets the separate store from it.
  */
  hostStoreSplitsTheNodeDaemonSet =
    let
      render = enable: (kubenixInstance { module.nixkube.hostStore.enable = enable; }).config;
      off = (render false).kubernetes.resources.nixkube.DaemonSet;
      onCfg = render true;
      on = onCfg.kubernetes.resources.nixkube.DaemonSet;
      hostMountPath = onCfg.nixkube.hostMountPath;
      pod = ds: ds.spec.template;
      matches =
        selector: labels: lib.all (k: labels.${k} or null == selector.${k}) (lib.attrNames selector);
      initNames = ds: map (c: c.name) (pod ds).spec.initContainers;
      firstInit = ds: lib.head (pod ds).spec.initContainers;
      node = ds: lib.head (lib.filter (c: c.name == "nix-node") (pod ds).spec.containers);
      envOf = ds: lib.listToAttrs (map (e: lib.nameValuePair e.name (e.value or null)) (node ds).env);
      hostPaths = ds: map (v: v.hostPath.path) (lib.filter (v: v ? hostPath) (pod ds).spec.volumes);
      volume = ds: name: (lib.head (lib.filter (v: v.name == name) (pod ds).spec.volumes)).hostPath.path;
      term =
        ds:
        let
          terms =
            (pod ds)
            .spec.affinity.nodeAffinity.requiredDuringSchedulingIgnoredDuringExecution.nodeSelectorTerms;
        in
        (lib.head terms).matchExpressions;
      separate = on.nix-node-separate;
      host = on.nix-node-host;
    in
    assert lib.attrNames off == [ "nix-node" ];
    assert !(lib.elem "host-check" (initNames off.nix-node));
    assert !((pod off.nix-node).spec ? affinity);
    assert
      lib.attrNames on == [
        "nix-node-host"
        "nix-node-separate"
      ];
    # Each selects its own pods and not the other's.
    assert matches separate.spec.selector.matchLabels (pod separate).metadata.labels;
    assert matches host.spec.selector.matchLabels (pod host).metadata.labels;
    assert !(matches separate.spec.selector.matchLabels (pod host).metadata.labels);
    assert !(matches host.spec.selector.matchLabels (pod separate).metadata.labels);
    # Both still carry what the PodMonitor and the tests select on.
    assert (pod host).metadata.labels."app.kubernetes.io/component" == "node";
    assert
      term separate == [
        {
          key = "nixkube/host";
          operator = "NotIn";
          values = [ "nixos" ];
        }
      ];
    assert
      term host == [
        {
          key = "nixkube/host";
          operator = "In";
          values = [ "nixos" ];
        }
      ];
    assert
      (firstInit separate).command == [
        "nixkube-host-check"
        "--mode"
        "separate"
      ];
    assert
      (firstInit host).command == [
        "nixkube-host-check"
        "--mode"
        "host"
      ];
    assert
      initNames host == [
        "host-check"
        "appstarter-init"
      ];
    assert (envOf host).HOST_STORE == "true";
    assert (envOf separate).HOST_STORE == "false";
    assert volume host "nix-root" == "/nix";
    assert volume separate "nix-root" == "${hostMountPath}/nix";
    assert !(lib.any (lib.hasPrefix hostMountPath) (hostPaths host));
    pkgs.runCommand "host-store-splits-the-node-daemonset" { } "echo ok > $out";

  /*
    `nixkube.node.enable = false` takes the node half out of the render.

    The option was declared and never read, so the DaemonSet, its
    PodMonitor and the CSIDriver all rendered with it off. A render is the
    only thing that shows it: nothing errors, and the objects the node pods
    read -- the ConfigMaps, the Secrets, the Role -- still render, because
    the cache side of an instance reads them too.
  */
  nodeEnableTakesTheNodeObjectsOut =
    let
      render =
        node:
        (kubenixInstance {
          module = {
            nixkube.node.enable = node;
            # On, so the PodMonitor is in scope for the assertion below.
            # Off by default, because it asserts the operator's CRDs.
            nixkube.metrics.podMonitor = true;
          };
        }).config;
      on = render true;
      off = render false;
      nsOf = cfg: cfg.kubernetes.resources.nixkube;
      # `or { }` because a driver object is cluster-scoped and no other
      # module here writes one: with the node off the whole bucket is gone.
      driversOf = cfg: cfg.kubernetes.resources.none.CSIDriver or { };
    in
    assert lib.attrNames (nsOf on).DaemonSet == [ "nix-node" ];
    # By name, because `PodMonitor` itself stays: `pynixd.nix` emits one for
    # the controller and the builders, and those are not node pods.
    assert (nsOf on).PodMonitor ? "nixkube";
    assert (driversOf on) ? "nixkube";
    # The legacy name goes with the node: `cfg.node.compat` gates it.
    assert (driversOf on) ? "nix.csi.store";
    assert !((nsOf off) ? DaemonSet);
    assert !(((nsOf off).PodMonitor or { }) ? "nixkube");
    assert !((driversOf off) ? "nixkube");
    pkgs.runCommand "node-enable-takes-the-node-objects-out" { } "echo ok > $out";

  assertionsNullShape =
    let
      instance = kubenixInstance {
        module.imports = [
          (
            { lib, ... }:
            {
              kubernetes.resources.nixkube.Deployment.nullprobe = {
                # Labelled as ours, so this exercises the assertion rather
                # than the warning for a neighbour's object.
                metadata.labels."app.kubernetes.io/part-of" = "nixkube";
                spec.template.spec.containers = lib.mkNamedList {
                  backend = {
                    image = "example/backend";
                    # Present and null, which is the shape that broke the
                    # assertion. Leaving it out gives a *missing* attribute
                    # instead, and `x or [ ]` handles that one -- so a probe
                    # that omits it passes whether the code is fixed or not.
                    #
                    # This attribute made exactly that mistake, and the test
                    # that convinced me otherwise was `c.volumeMounts or null
                    # == null`, which answers null for a missing attribute too.
                    volumeMounts = null;
                  };
                };
              };
            }
          )
        ];
      };
      # Evaluate the probe, and do not build the manifest.
      #
      # `test -s ${"$"}{instance.manifestYAMLFile}` realises the whole closure,
      # and failed the check job on a runner with a cold store, for a test
      # whose entire subject is an evaluation. Forcing the one rendered
      # object runs the object check on it, and nothing is built.
      rendered = renderedObject instance "nullprobe";
    in
    builtins.deepSeq rendered (pkgs.runCommand "assertions-null-shape" { } "echo ok > $out");

  # Our own object with an empty env var is refused. The neighbour test
  # below proves the opposite half; this one proves the check fires at all.
  assertionsOwnRefused =
    let
      instance = kubenixInstance {
        module.imports = [
          (
            { lib, ... }:
            {
              kubernetes.resources.nixkube.Deployment.ours = {
                metadata.labels."app.kubernetes.io/part-of" = "nixkube";
                spec.template.spec.containers = lib.mkNamedList {
                  backend = {
                    image = "example/backend";
                    env = lib.mkNamedList {
                      EMPTY.value = "";
                    };
                  };
                };
              };
            }
          )
        ];
      };
    in
    assert !(builtins.tryEval (builtins.deepSeq (renderedObject instance "ours") true)).success;
    pkgs.runCommand "assertions-own-refused" { } "echo ok > $out";

  # A neighbour's object is reported and not refused.
  #
  # config.kubernetes.resources holds every object of the whole instance, not
  # nixkube's, so an assertion that walks the lot lets a nixkube module veto a
  # consumer's unrelated chart. That happened: the empty-string check refused
  # to render a 448-object tree over two env vars in Rook's ceph-csi
  # controller, where `WATCH_NAMESPACE: ""` is the ordinary idiom for "all
  # namespaces". Nothing of nixkube's was wrong.
  #
  # This renders exactly that shape -- an empty env var on an object without
  # nixkube's part-of label -- and must evaluate. The warning still names it.
  assertionsNeighbourScope =
    let
      instance = kubenixInstance {
        module.imports = [
          (
            { lib, ... }:
            {
              kubernetes.resources.nixkube.Deployment.neighbour = {
                spec.template.spec.containers = lib.mkNamedList {
                  manager = {
                    image = "example/rook";
                    env = lib.mkNamedList {
                      WATCH_NAMESPACE.value = "";
                    };
                  };
                };
              };
            }
          )
        ];
      };
    in
    builtins.deepSeq (renderedObject instance "neighbour") (
      pkgs.runCommand "assertions-neighbour-scope" { } "echo ok > $out"
    );

  # An operator can cap or disable builders through nixkube.pynixd.settings.
  #
  # Both the built-in defaults and the shared settings were wrapped in
  # mkDefault, so two definitions of builder-max met at the same priority and
  # the evaluation failed with a conflict rather than one winning. mkForce did
  # not help, because mapAttrsRecursive re-wrapped it. The option that looks
  # like the way to cap builders could not set the three keys that have
  # defaults, which is every key anyone would want to change.
  builderSettingsOverride =
    let
      capped = kubenixInstance {
        module.config.nixkube.pynixd.settings = {
          builder-min = 0;
          builder-max = 0;
          builder-startup-timeout = 120;
          builder-backoff-cap = 60;
          builder-max-age = 0;
        };
      };
      stock = kubenixInstance { };
      got = capped.config.nixkube.pynixd.controller.settings;
      base = stock.config.nixkube.pynixd.controller.settings;

      # The value the program actually reads.
      #
      # Asserting the option alone is not enough, and this check made that
      # mistake first: the option merged correctly while the StatefulSet still
      # carried a hardcoded PYNIXD_BUILDER_MAX of "3". NixkubeCentralSettings
      # reads these from the environment only, so the literal won and capping
      # builders did nothing.
      envOf =
        instance: name:
        let
          pod = instance.config.kubernetes.resources.nixkube.StatefulSet.pynixd.spec.template.spec;
          container = lib.head (lib.filter (c: c.name == "pynixd") pod.containers);
        in
        (lib.head (lib.filter (e: e.name == name) container.env)).value;
    in
    assert got.builder-min == 0 && got.builder-max == 0;
    # The defaults still apply where the operator said nothing.
    assert got.idle-timeout == 300;
    assert base.builder-min == 1 && base.builder-max == 3;
    assert envOf capped "PYNIXD_BUILDER_MAX" == "0";
    assert envOf capped "PYNIXD_BUILDER_MIN" == "0";
    assert envOf stock "PYNIXD_BUILDER_MAX" == "3";
    assert envOf stock "PYNIXD_BUILDER_MIN" == "1";
    # The startup watchdog and the failure backoff read the environment the
    # same way, so they need the same assertion.
    assert envOf capped "PYNIXD_BUILDER_STARTUP_TIMEOUT" == "120";
    assert envOf capped "PYNIXD_BUILDER_BACKOFF_CAP" == "60";
    assert envOf stock "PYNIXD_BUILDER_STARTUP_TIMEOUT" == "600";
    assert envOf stock "PYNIXD_BUILDER_BACKOFF_CAP" == "600";
    # Under the 12 hours at which KubeJobNotCompleted fires.
    assert envOf capped "PYNIXD_BUILDER_MAX_AGE" == "0";
    assert envOf stock "PYNIXD_BUILDER_MAX_AGE" == "21600";
    # activeDeadlineSeconds is deliberately absent from the builder Job. It
    # measures a Job's whole life, and a builder Job runs for hours, so any
    # value that bounds a Pod hung before startup kills working builders
    # mid-build. The bound is `builder-startup-timeout`, in the manager.
    assert
      !(builtins.hasAttr "activeDeadlineSeconds" (
        stock.config.kubernetes.resources.nixkube.PodTemplate.nixkube-builder.template.spec
      ));
    pkgs.runCommand "builder-settings-override" { } "echo ok > $out";

  # A caller's `sources` decide which pynixd and dinix nixkube uses, through
  # the overlay and through the kubenix modules alike. Both read this
  # repository's own lock instead, so a caller that bumped its umbrella still
  # ran a pynixd 124 commits behind (#79).
  callerSourcesReachPynixd =
    let
      fixture = ./nix/fixtures/caller-pynixd;
      marked = sources // {
        pynixd = fixture;
        dinix = fixture;
      };
      viaOverlay = (import ./. { sources = marked; }).pkgs.pynixd;
      viaModule =
        (kubenixInstance { module._module.args.sources = lib.mkForce marked; }).config.nixkube.dinix;
    in
    assert viaOverlay == "caller-pynixd";
    assert viaModule == fixture;
    pkgs.runCommand "caller-sources-reach-pynixd" { } "echo ok > $out";

  # Every source is a fetched tree, not a directory on somebody's disk.
  #
  # A source read as a directory is a different input from the tree CI
  # fetches, so the same commit builds different packages in the two places.
  # Nothing says so at the time: the build succeeds, and a node later asks its
  # substituters for a store path that was never built anywhere.
  #
  # Three separate places had this: working copies read as directories, an
  # overlay that resolved pynixd from a sibling directory or an unpinned
  # branch, and Python's caches in the package sources. All three produced
  # one symptom, and the first two are invisible to any check that only looks
  # at one checkout. nixidae's nix/wire.nix copies what git tracks of each
  # source to a store path, so a build here and in CI read one tree.
  sourcesAreLocked =
    let
      loose = lib.filterAttrs (_: value: !(lib.hasPrefix builtins.storeDir (toString value))) sources;
      names = lib.attrNames loose;
    in
    assert lib.assertMsg (names == [ ]) ''
      These sources are directories rather than fetched trees:

      ${lib.concatMapStringsSep "\n" (n: "  ${n} -> ${toString loose.${n}}") names}

      A build here and a build in CI disagree when that happens, and the
      difference shows up on a cluster rather than here: a node asks its
      substituters for a store path that was never built anywhere.
    '';
    pkgs.runCommand "sources-are-locked" { } "echo ok > $out";

  # nix-node reports whether its driver answers.
  #
  # The container had a livenessProbe and no readinessProbe. The liveness
  # path works -- restartCount climbed 3 -> 6 on nixlab2 while the driver was
  # dead -- but a container with no readinessProbe is Ready as soon as it
  # runs. So `kubectl get pods` said ready=true for six minutes of a node
  # that could not mount anything, and builders kept being sent to it.
  #
  # Asserted against the rendered container, not the option, and against the
  # sidecar that serves the port. A probe pointing at a port nothing serves
  # would pass an option-level check and fail on a cluster.
  nodeDriverReadiness =
    let
      instance = kubenixInstance { };
      podSpec = instance.config.kubernetes.resources.nixkube.DaemonSet.nix-node.spec.template.spec;
      containerNamed = name: lib.head (lib.filter (c: c.name == name) podSpec.containers);
      node = containerNamed "nix-node";
      sidecar = containerNamed "livenessprobe-nixkube";
      port = node.readinessProbe.httpGet.port;
    in
    assert node.readinessProbe.httpGet.path == "/healthz";
    assert node.livenessProbe.httpGet.port == port;
    # Something has to answer on that port.
    assert lib.elem "--health-port=${toString port}" sidecar.args;
    # NotReady has to be reported before the restart, or it says nothing new.
    assert
      node.readinessProbe.failureThreshold * node.readinessProbe.periodSeconds
      < node.livenessProbe.failureThreshold * node.livenessProbe.periodSeconds;
    pkgs.runCommand "node-driver-readiness" { } "echo ok > $out";

  # A probe that a busy pynixd cannot answer kills the push it is busy with.
  #
  # The kubelet's own `timeoutSeconds` is 1. A pynixd ingesting a
  # multi-hundred-megabyte store transfer does not answer a dial inside a
  # second, so the liveness probe fails and the container is killed. Measured
  # twice on one cluster while pushing 186 MiB: `exitCode: 143`,
  # `restartCount: 2`. The push reports `Nix daemon disconnected
  # unexpectedly`, never a probe failure, so nothing points at the probe.
  # Issue nixidae/nixidae#432.
  #
  # Asserted on the rendered pod spec, because the defect was a field nobody
  # wrote: an option-level check would have passed while the kubelet applied
  # its own default.
  pynixdProbesSurviveAPush =
    let
      instance = kubenixInstance { };
      res = instance.config.kubernetes.resources.nixkube;
      # A StatefulSet keeps its pod spec under `spec.template.spec`, a
      # PodTemplate under `template.spec`.
      containerNamed = podSpec: name: lib.head (lib.filter (c: c.name == name) podSpec.containers);
      subjects = {
        controller = containerNamed res.StatefulSet.pynixd.spec.template.spec "pynixd";
        builder = containerNamed res.PodTemplate.nixkube-builder.template.spec "pynixd";
      };
      # Every probe asks the event loop a question, over the port that
      # answers it. A `tcpSocket` probe is completed by the kernel from the
      # listen backlog, so it passes against a pynixd that has stopped serving
      # -- issue #53. The port is named rather than numbered because the
      # container, the probes and the scrape discovery all have to agree, and
      # a name is the one spelling that cannot drift.
      asks =
        p:
        assert p ? httpGet;
        assert p.httpGet.path == "/healthz";
        assert p.httpGet.port == "http";
        true;
      ok =
        c:
        assert lib.all asks [
          c.livenessProbe
          c.readinessProbe
          c.startupProbe
        ];
        # The whole of the defect: absent means 1.
        assert c.livenessProbe.timeoutSeconds > 1;
        assert c.readinessProbe.timeoutSeconds > 1;
        # One slow answer must not kill it either.
        assert c.livenessProbe.failureThreshold > 1;
        # A cold pynixd restoring its store gets longer than a running one.
        assert
          c.startupProbe.failureThreshold * c.startupProbe.periodSeconds
          > c.livenessProbe.failureThreshold * c.livenessProbe.periodSeconds;
        true;
    in
    assert lib.all ok (lib.attrValues subjects);
    pkgs.runCommand "pynixd-probes-survive-a-push" { } "echo ok > $out";

  # Does any private key reach the Nix store?
  #
  # /nix/store is world readable, and a manifest rendered here is a store
  # path. So a private key that reaches this render is readable by every user
  # on every machine that builds or substitutes it, and it stays readable --
  # a store path is immutable and lives until it is garbage collected.
  #
  # nixkube never generates a keypair at evaluation time. `kubenix/secret.nix`
  # runs `ssh-keygen` in the cluster, at deploy time, and hands the result to
  # `kubectl create secret`. What this repository renders is the script, the
  # secret's name, and a mount path. `keys/*.pub` are public halves and are
  # filtered by suffix.
  #
  # That is an invariant, not an observation, so it is checked rather than
  # documented. Adding a `Secret` with `data` to the module would be an
  # ordinary-looking change and this is what says no.
  noPrivateKeysInManifest =
    pkgs.runCommand "no-private-keys-in-manifest" { nativeBuildInputs = [ pkgs.jq ]; }
      ''
        manifest=${kubenixApply.manifestJSONFile}

        # A rendered Secret carrying material. `nixkube/discard` does not help
        # here: the key would be in the JSON itself, not in a store path it
        # names.
        carriers=$(jq -r '
          [ (.items // [.])[]
            | select(.kind == "Secret")
            | select((.data // .stringData) != null)
            | .metadata.name ] | join(", ")
        ' "$manifest")
        if [ -n "$carriers" ]; then
          echo "Secret objects rendered with inline material: $carriers" >&2
          echo "Generate them in the cluster instead. See kubenix/secret.nix." >&2
          exit 1
        fi

        # And the shape a key takes when it arrives some other way -- read from
        # a path with builtins.readFile, or pasted into an option.
        if grep -qE 'PRIVATE KEY|BEGIN OPENSSH' "$manifest"; then
          echo "The rendered manifest contains private key material." >&2
          echo "/nix/store is world readable, so this publishes it." >&2
          exit 1
        fi

        touch $out
      '';

  # Can the builder manager watch what it decides on?
  #
  # It watches CSINode to find nodes where the nixkube CSI driver is
  # registered, and patches the Node with the result. A missing grant does
  # not crash it: `_watch_nodes` catches, logs `node_watch_error` and sleeps
  # ten seconds, for ever. So the cluster looks healthy, no node is ever
  # probed, and the only evidence is one line in a log nobody reads.
  #
  # Against the rendered manifest, because the ClusterRole is what the
  # apiserver reads.
  probeWatchHasRbac = pkgs.runCommand "probe-watch-has-rbac" { nativeBuildInputs = [ pkgs.jq ]; } ''
    # `$rules` is bound before the map, because inside `map` the input is
    # the triple being tested and not the document.
    missing=$(jq -r '
      [ (.items // [.])[] | select(.kind == "ClusterRole") | .rules[] ] as $rules
      | def granted($group; $resource; $verb):
          [ $rules[]
            | select(.apiGroups | index($group))
            | select(.resources | index($resource))
            | select((.verbs | index($verb)) or (.verbs | index("*")))
          ] | length > 0;

        [ ["storage.k8s.io", "csinodes", "watch"],
          ["storage.k8s.io", "csinodes", "list"],
          ["", "nodes", "get"],
          ["", "nodes", "patch"]
        ]
        | map(select(granted(.[0]; .[1]; .[2]) | not) | join(" "))
        | join("; ")
    ' ${kubenixApply.manifestJSONFile})

    if [ -n "$missing" ]; then
      echo "The ClusterRole does not grant: $missing" >&2
      echo "Node probing fails silently without these. See kubenix/rbac.nix." >&2
      exit 1
    fi
    touch $out
  '';

  # Does a builder present the host key the controller pins?
  #
  # `ssh_known_hosts` names one ed25519 key for `*`, and asyncssh derives the
  # acceptable server-host-key algorithms from that entry. A builder with no
  # `PYNIXD_SSH_HOST_KEY` generates a fresh ssh-rsa key instead, the
  # intersection is empty, and the exchange dies before authentication:
  #
  #   asyncssh.misc.KeyExchangeFailed: Unable to find compatible server host key
  #
  # Measured on nixlab2: 82 builders registered, 82 ssh_connect_failed, none
  # ever reachable. The builders were healthy. Nothing else reports this --
  # the Job runs, the pod is Ready, and only the controller's own traceback
  # says why -- so it is asserted here.
  #
  # Against the rendered template and the rendered StatefulSet, and against
  # the volume rather than the string. A path that matches no mount would
  # pass a string comparison and fail on a cluster.
  builderPresentsPinnedHostKey =
    let
      res = kubenixApply.config.kubernetes.resources.nixkube;
      hostKeyOf =
        spec:
        let
          container = lib.head (lib.filter (c: c.name == "pynixd") spec.containers);
          env = lib.filter (e: e.name == "PYNIXD_SSH_HOST_KEY") (container.env or [ ]);
        in
        assert lib.assertMsg (env != [ ]) "pynixd container has no PYNIXD_SSH_HOST_KEY";
        rec {
          path = (lib.head env).value;
          # `mountPath + "/"`, because these are path prefixes and not string
          # prefixes. The controller mounts ssh-config at /etc/ssh, which is a
          # string prefix of /etc/ssh-key/id_ed25519 and not a path one, and
          # sorts first -- so a plain hasPrefix picks the ConfigMap and asks
          # it for a secretName it does not have.
          matches = lib.filter (m: lib.hasPrefix (m.mountPath + "/") path) (container.volumeMounts or [ ]);
          mount =
            assert lib.assertMsg (
              lib.length matches == 1
            ) "PYNIXD_SSH_HOST_KEY ${path} is under ${toString (lib.length matches)} mounts";
            lib.head matches;
          secret = (lib.head (lib.filter (v: v.name == mount.name) spec.volumes)).secret.secretName;
        };
      builder = hostKeyOf res.PodTemplate.nixkube-builder.template.spec;
      controller = hostKeyOf res.StatefulSet.pynixd.spec.template.spec;
    in
    # Both sides of the connection have to name one keypair, or the pin the
    # controller carries cannot match what the builder offers.
    assert builder.secret == controller.secret;
    # ed25519, because the known_hosts entry is. A key of another type is the
    # defect this check exists for.
    assert lib.hasInfix "ed25519" builder.path;
    pkgs.runCommand "builder-presents-pinned-host-key" { } "echo ok > $out";

  /**
    A CSI volume as a composefs image, built with this pkgs' tools.
    `nix/composefs.nix` says what it holds and why it takes its tools as
    arguments.
  */
  composefsImage =
    {
      roots,
      primary ? null,
      opaque ? true,
    }:
    import ./nix/composefs.nix {
      inherit roots primary opaque;
      tools = { inherit (pkgs) nixkube composefs nix; };
      inherit (pkgs.stdenv.hostPlatform) system;
    };

  # The image for hello holds hello's whole closure as references, so a GC
  # root on an image keeps what it presents; and it carries its database.
  composefsImageHoldsItsClosure =
    let
      # Not the form nodes share: that one names its inputs as bare store
      # paths, and builds only where they exist already.
      image = composefsImage {
        roots = [ pkgs.hello ];
        primary = pkgs.hello;
        opaque = false;
      };
      held = pkgs.closureInfo { rootPaths = [ image ]; };
      wanted = pkgs.closureInfo { rootPaths = [ pkgs.hello ]; };
    in
    pkgs.runCommand "composefs-image-holds-its-closure" { } ''
      test -s ${image}/image.cfs
      test -s ${image}/db/db.sqlite
      while read -r path; do
        grep -qx "$path" ${held}/store-paths || { echo "the image does not hold $path"; exit 1; }
      done < ${wanted}/store-paths
      ${pkgs.composefs}/bin/composefs-info ls ${image}/image.cfs > listing
      grep -q '^/bin/hello' listing || { echo "no /bin/hello at the root"; head listing; exit 1; }
      grep -q '^/nix/var/nix/db/db.sqlite' listing || { echo "no database"; exit 1; }
      echo ok > $out
    '';

  # The nixkube wrapper names the tools `composefsImage` passes. A node
  # writes the image's derivation from these, and any other path is another
  # derivation: the node would never find CI's image.
  nixkubeNamesComposefsTools =
    let
      wrapper = "${pkgs.nixkube}/bin/nixkube";
    in
    pkgs.runCommand "nixkube-names-composefs-tools" { } ''
      grep -q "NIXKUBE_PACKAGE='\?${pkgs.nixkube}'\?" ${wrapper} || { echo "NIXKUBE_PACKAGE"; exit 1; }
      grep -q "NIXKUBE_COMPOSEFS='\?${pkgs.composefs}'\?" ${wrapper} || { echo "NIXKUBE_COMPOSEFS"; exit 1; }
      grep -q "NIXKUBE_NIX='\?${pkgs.nix}'\?" ${wrapper} || { echo "NIXKUBE_NIX"; exit 1; }
      echo ok > $out
    '';

  # NixOS modules for the hosts nixkube runs on. `host` is the boot
  # assessment a NixOSRollout that reboots wants.
  nixosModules.host = ./nix/host.nix;

  # Tests written as nixos-test modules, run as vivarium guests: a
  # sandboxed build, a `.driver` run and an MCP session are the same test.
  nixosTests =
    let
      inherit (import (sources.vivarium + "/lib.nix") { inherit pkgs sources; }) fromNixosTest;
    in
    {
      # Whether `nix build` works inside a store made of bind mounts. Issue
      # #65 rests on the answer, and nothing short of real root can ask it.
      bind-farm = fromNixosTest (import ./tests/nixos/bind-farm.nix { inherit pkgs lib; });

      # A mount flag can only be checked by mounting. Issue #66.
      csi-mount-rec = fromNixosTest (import ./tests/nixos/csi-mount-rec.nix { inherit pkgs lib; });

      # nixkube's own `build_farm`, as root against a real closure. Issue #65.
      farm-builder = fromNixosTest (import ./tests/nixos/farm-builder.nix { inherit pkgs lib; });

      # Whether composefs can present a CSI volume: one mount, real files,
      # nothing linked out of the store. A measurement. Issues #68 and #25.
      composefs = fromNixosTest (import ./tests/nixos/composefs.nix { inherit pkgs lib; });

      # A CSI volume as one composefs mount, through nixkube's own code, with
      # the node building CI's derivation. Issues #68 and #25.
      csi-composefs = fromNixosTest (
        import ./tests/nixos/csi-composefs.nix { inherit pkgs lib composefsImage; }
      );
    };

  treefmt = (import sources.treefmt-nix).mkWrapper pkgs {
    projectRootFile = "default.nix";
    programs.fish_indent.enable = true;
    programs.nixfmt.enable = true;
    programs.ruff-check.enable = true;
    programs.ruff-format.enable = true;
    programs.shellcheck.enable = true;
    programs.typos.enable = true;
    programs.yamlfmt.enable = true;
  };

  # `graphviz` by name, or python3Packages' library of that name arrives
  # and Sphinx finds no `dot`.
  inherit (import sources.nixvenv { inherit pkgs; }) nixvenv;
  nixkube-docs = pkgs.python3Packages.callPackage ./nix/docs.nix {
    inherit (pkgs) graphviz;
    inherit nixvenv;
  };

  nixImage = pkgs.callPackage ./niximage.nix {
    inherit sources;
    inherit (sources) dinix;
  };
  scratchImage = pkgs.callPackage ./scratchimage.nix { };

  # Two spikes on how the image is layered. Nothing in CI reads these, and
  # `niximage.nix` is untouched. nixkube issue #47.
  spike = {
    n2c = import ./nix/image-spike/nix2container.nix { inherit pkgs sources; };
    dockertools = import ./nix/image-spike/dockertools.nix { inherit pkgs; };
  };

  # Every image the node DaemonSet names, as a tarball a UML guest can import.
  # There is no registry in a Nix build sandbox. See nix/uml/images.nix.
  umlImages = pkgs.callPackage ./nix/uml/images.nix { inherit nixImage scratchImage; };

  # Do those tarballs carry the tags the DaemonSet asks for? Seconds, against
  # twenty minutes of cluster before an ErrImagePull says the same thing.
  umlImagesMatch = umlImages.check umlManifest.manifestJSONFile;

  # nixkube on a real node, under User-Mode Linux, in a Nix build sandbox --
  # so no KVM, no root, and a derivation that passes or fails. See nix/uml.
  umlManifest = kubenixInstance {
    module.imports = [
      ./kubenix/ci
      ./nix/uml/manifest.nix
      ./nix/uml/workloads.nix
    ];
  };

  umlTest = pkgs.callPackage ./nix/uml {
    inherit sources umlImages;
    manifest = umlManifest;
  };

  /*
    `umlTest` on every combination the README's runtime table claims, one
    attribute each, by hand and never in CI:

        nix run --file . umlMatrix.uml-crio.driver -- --out ./o
        nix build --file . umlMatrix.qemu-crio-kata

    A cell fixes backend, CRI and Kata, so no environment variable can
    change what it runs. The `-kata` cells need nested virtualization.
  */
  umlMatrix =
    let
      cell =
        backend: cri: kata:
        let
          name = "${backend}-${cri}${lib.optionalString kata "-kata"}";
        in
        lib.nameValuePair name (
          pkgs.callPackage ./nix/uml {
            inherit sources umlImages;
            manifest = umlManifest;
            matrixCell = { inherit backend cri kata; };
            attrPath = [
              "umlMatrix"
              name
            ];
          }
        );
    in
    lib.listToAttrs [
      (cell "qemu" "containerd" false)
      (cell "qemu" "crio" false)
      (cell "uml" "containerd" false)
      (cell "uml" "crio" false)
      (cell "qemu" "containerd" true)
      (cell "qemu" "crio" true)
    ]
    // lib.genAttrs' [ "containerd" "crio" ] (
      # The node runs from the guest's own /nix, as on a NixOS host, with
      # the separate store before it to retire. Issue #25.
      cri:
      lib.nameValuePair "uml-${cri}-hoststore" (
        pkgs.callPackage ./nix/uml {
          inherit sources umlImages;
          manifest = kubenixInstance {
            module.imports = [
              ./kubenix/ci
              ./nix/uml/manifest.nix
              ./nix/uml/workloads.nix
              { nixkube.hostStore.enable = true; }
            ];
          };
          matrixCell = {
            backend = "uml";
            inherit cri;
            kata = false;
          };
          attrPath = [
            "umlMatrix"
            "uml-${cri}-hoststore"
          ];
        }
      )
    )
    // {
      # A kernel without EROFS, as on Talos and GKE's COS: the node's own
      # probe sends CSI to hardlink trees and NRI to the bind farm.
      qemu-containerd-noerofs = pkgs.callPackage ./nix/uml {
        inherit sources umlImages;
        manifest = umlManifest;
        matrixCell = {
          backend = "qemu";
          cri = "containerd";
          kata = false;
          erofs = false;
        };
        attrPath = [
          "umlMatrix"
          "qemu-containerd-noerofs"
        ];
      };

      # CSI volumes as hardlink trees, which every node whose kernel fails
      # the composefs probe still uses. Issue #68.
      uml-containerd-hardlinks = pkgs.callPackage ./nix/uml {
        inherit sources umlImages;
        manifest = kubenixInstance {
          module.imports = [
            ./kubenix/ci
            ./nix/uml/manifest.nix
            ./nix/uml/workloads.nix
            { nixkube.csi.composefs = false; }
          ];
        };
        matrixCell = {
          backend = "uml";
          cri = "containerd";
          kata = false;
        };
        attrPath = [
          "umlMatrix"
          "uml-containerd-hardlinks"
        ];
      };
    };

  /*
    The kind jobs, on a guest instead of on a container.

    `umlTest` breaks a node nine ways to see whether the driver comes back.
    This one asks what the kind jobs ask: that `kubenixDeploy` works, that
    the DaemonSet rolls out, that the test workloads complete, and that
    deleting them cleans up -- on a node that pulls its own images and has
    had nothing done to it.

    Not a check and not in CI's sandbox: the node pulls from
    registry.k8s.io and what it deploys comes from ghcr.io.

    Two of them, for the two kind jobs: `test-kind-nocache` deploys
    kubenixCI2 and `test-kind-cache` deploys kubenixCI1, which brings the
    pynixd cache with it.  Everything else is the same, down to the job
    lists -- ./ci/test-jobs.nix.

        nix run --file . ciTest.driver -- --out ./o        # no pynixd, kubenixCI2
        nix run --file . ciTestCache.driver -- --out ./o   # pynixd, kubenixCI1

    A guest takes 14 GB, so these are two runs and not one.
  */
  # What the test workloads are, and what each one is supposed to do.
  testJobs = import ./ci/test-jobs.nix;

  ciTest = pkgs.callPackage ./nix/uml/ci.nix {
    inherit sources umlImages;
    name = "nixkube-ci";
    instance = kubenixCIGuest ciModules.two;
    workloads = kubenixCITest;
    deployedJobs = testJobs.deployed;
    assertedJobs = testJobs.asserted;
    rejectedJobs = testJobs.rejected;
    lan = {
      network = "nixkube-ci";
      address = "10.105.0.1/24";
    };
  };

  ciTestCache = pkgs.callPackage ./nix/uml/ci.nix {
    inherit sources umlImages;
    name = "nixkube-ci-cache";
    instance = kubenixCIGuest ciModules.one;
    workloads = kubenixCITest;
    deployedJobs = testJobs.deployed;
    assertedJobs = testJobs.asserted;
    rejectedJobs = testJobs.rejected;
    lan = {
      network = "nixkube-ci-cache";
      address = "10.106.0.1/24";
    };
  };
  /*
    `ciTest` and `ciTestCache` on CRI-O, by hand and never in CI: the same
    deployment and jobs on the other CRI.

        nix run --file . ciMatrix.crio.driver -- --out ./o
  */
  ciMatrix =
    let
      onCrio =
        test:
        test.extend {
          modules = [
            {
              name = lib.mkForce "${test.config.name}-crio";
              nodes.cp.services.vivarium-k8s.cri = "crio";
            }
          ];
        };
    in
    {
      crio = onCrio ciTest;
      crio-cache = onCrio ciTestCache;
    }
    //
      lib.mapAttrs
        (
          name:
          { foreignHost, subnet }:
          ciTest.override {
            name = "nixkube-ci-${name}";
            instance = kubenixCIGuest (ciModules.two ++ [ { nixkube.hostStore.enable = true; } ]);
            inherit foreignHost;
            lan = {
              network = "nixkube-ci-${name}";
              address = "10.${toString subnet}.0.1/24";
            };
          }
        )
        {
          # `ciTest` with host store on: the NixOS guest runs nixkube from
          # its own /nix. Issue #25.
          hoststore = {
            foreignHost = false;
            subnet = 107;
          };
          # And on a node that is not NixOS, which keeps the separate store.
          hoststore-foreign = {
            foreignHost = true;
            subnet = 108;
          };
        };

  ci-debug = pkgs.callPackage ./pkgs/ci-debug { };

  # Is every store path this manifest names actually fetchable?
  #
  # A nixkube CSI volume names an output path, so a node substitutes it and
  # can never build it. A path on no substituter is a mount that fails, and
  # the failure surfaces as `MountVolume.SetUp failed ... Failed to build
  # store path`, on a node, well away from whatever forgot to push it.
  #
  # Ask before deploying, not after. The substituter list comes from the
  # module, so this asks about the caches a node really carries.
  assert-cached = pkgs.callPackage ./pkgs/assert-cached {
    substituters = kubenixApply.config.nixkube.nixConfig.settings.substituters;
    inherit (kubenixApply.passthru) ekn;
  };

  # Does this attribute build on the machine that is asked to build it?
  arch-audit = pkgs.callPackage ./pkgs/arch-audit { };

  # Every gate CI builds, as one attribute.
  #
  # `nix build --file ./checks.nix all` is the answer to "does this pass
  # CI", and before this it was "read ci.yaml and copy six commands". A gate
  # that is not in here is a gate nobody runs by hand, so adding one to the
  # workflow and not to this set is the mistake to avoid -- which is why the
  # `check` job builds this attribute rather than listing them again.
  #
  # It holds exactly what the workflow held, so this is a refactor and not a
  # widening. treefmt stays outside it: it is checked with `git diff` against
  # a working tree, which a derivation does not have. zuban runs inside each
  # Python suite (nix/tests.nix).
  #
  # `umlImagesMatch` is the one member that builds rather than evaluates: it
  # writes two image tarballs, so on a cold store it also fetches the nix
  # image closure. That is the whole cost of this set.
  #
  #   this step, without it   69s    check job  3m25s
  #   this step, with it     168s    check job  5m13s
  #
  # Measured on runs 34856944613 and 34857619341, cold runners both. So it
  # costs about 100s against a 30 minute bound, and it earns that: the
  # failure it catches -- a tarball that does not carry the tag the DaemonSet
  # asks for -- otherwise appears as ErrImagePull, on a cluster, twenty
  # minutes after the change that caused it.
  #
  # If it ever stops being worth 100s, move it to `build-amd64`, which builds
  # the same image and so already holds the closure. Dropping it is the wrong
  # answer; nothing else asks this question.
  # `all` is `attrValues` of this set and not a second list. The list was the
  # second list, and it dropped `pynixdProbesSurviveAPush`: the check existed,
  # `nix build --file . checks.pynixdProbesSurviveAPush` passed, and the
  # `check` job never ran it, because that job builds `all`. A member added to
  # one place and not the other is silent both ways round.
  checkMembers = {
    configurationPreview = import ./nix/configuration-preview.nix {
      inherit
        pkgs
        lib
        kubenixInstance
        sources
        ;
    };
    inherit
      assertionsNullShape
      assertionsNeighbourScope
      assertionsOwnRefused
      builderSettingsOverride
      callerSourcesReachPynixd
      metricsPodMonitorShape
      nodeDriverReadiness
      appstarterInitIsExemptFromInjection
      hostStoreSplitsTheNodeDaemonSet
      nodeEnableTakesTheNodeObjectsOut
      composefsImageHoldsItsClosure
      nixkubeNamesComposefsTools
      pynixdPodMonitorShape
      pynixdBindsEveryInterface
      pynixdProbesSurviveAPush
      sourcesAreLocked
      ciWorkflowCheck
      presentationChartIsCurrent
      docOptionsCheck
      builderPresentsPinnedHostKey
      noPrivateKeysInManifest
      probeWatchHasRbac
      umlImagesMatch
      ;
  }
  # The four Python suites, each its own member.
  #
  # **A member here is a test result, not a package.** Naming a package whose
  # build happens to run a suite answers "does the package build", and a suite
  # that stops running then looks exactly like a suite that passes.
  // pythonTests;

  checks = checkMembers // {
    all = pkgs.runCommand "nixkube-checks" {
      checks = builtins.attrValues checkMembers;
    } "printf '%s\\n' $checks > $out";
  };

  # Every GitHub Actions workflow as a value, beside the file it renders to.
  # ci/workflows/*.nix hold them and say why.
  ghanix = import sources.ghanix { inherit lib; };

  ciWorkflows = {
    pages = {
      committed = ./.github/workflows/pages.yaml;
      value = import ./ci/workflows/pages.nix {
        inherit lib;
        ghalib = ghanix;
      };
    };
  };

  # The workflows that run the tests. nixidae renders each one to
  # `.github/workflows/nixkube-<name>.yml`.
  ciTestWorkflows.tests = import ./ci/workflows/tests.nix {
    inherit lib;
    ghalib = ghanix;
  };

  # Those values as the files GitHub reads.
  #
  # `ghanix.toYamlScript` and not `pkgs.formats.yaml`, which is remarshal: remarshal
  # writes a multi-line string as one escaped double-quoted scalar, so a
  # ten-line `run:` body arrives as a single 600-column line holding `\n`.
  # The script writes those as literal blocks, and emits no `%YAML 1.1`
  # directive and no `---` marker, which is why nothing trims a header here
  # any more. It quotes `'on'` for the same YAML 1.1 reason the directive
  # used to appear, which is what a workflow needs: unquoted, `on` is the
  # boolean `true` to a 1.1 parser.
  #
  # Then yamlfmt, because treefmt runs yamlfmt over this file and the `check`
  # job ends in `git diff --exit-code`. A render in any other style is a
  # failing job on every run. Running the same formatter here makes the two
  # agree by construction rather than by taste. yamlfmt keeps the blocks.
  #
  # Key order is alphabetical and nothing depends on it: GitHub does not, and
  # ciWorkflowCheck compares parsed documents rather than text.
  ciWorkflowFiles = lib.mapAttrs (
    name: wf:
    let
      header = ''
        # SPDX-License-Identifier: Apache-2.0
        # GENERATED FILE -- do not edit by hand.
        # Edit ci/workflows/${name}.nix, then run: nix run --file . ci-workflow-update
      '';
    in
    pkgs.runCommand "${name}.yaml"
      {
        nativeBuildInputs = [
          (nixvenv {
            python = pkgs.python3;
            packages = ps: [ ps.pyyaml ];
            executables = [ "python3" ];
          })
          pkgs.yamlfmt
        ];
        value = builtins.toJSON wf.value;
        passAsFile = [ "value" ];
      }
      ''
        {
          printf '%s\n' ${lib.escapeShellArg header}
          python3 ${ghanix.toYamlScript} "$valuePath"
        } > out.yaml
        HOME=$PWD yamlfmt out.yaml
        mv out.yaml $out
      ''
  ) ciWorkflows;

  # Does each committed workflow still say what its ci/workflows/*.nix says?
  #
  # Parsed, not compared as text, so key order and quoting cannot fail this.
  # yq reads YAML 1.2, where a bare `on:` key is the string "on". A YAML 1.1
  # parser answers the boolean `true` for it, which would compare a boolean
  # key against a string one and fail every run.
  #
  # Does doc/options.md still say what the modules say?
  #
  # The same question ciWorkflowCheck asks about the workflows, and it exists
  # because nothing asked it: `genModDoc` was broken two ways for weeks and
  # the committed file drifted 573 lines with nothing to notice. A generated
  # file with no gate is a file that silently stops being generated.
  #
  # Text, not parsed. Markdown has no canonical form to compare, and the
  # generator is a derivation, so its output is byte-stable for a given
  # module set.
  docOptionsCheck = pkgs.runCommand "doc-options-check" { } ''
    if ! diff --unified ${./doc/options.md} ${optionsDocs.optionsCommonMark}; then
      echo >&2
      echo "doc/options.md and the module options disagree." >&2
      echo "Run: nix run --file . genModDoc" >&2
      exit 1
    fi
    touch $out
  '';

  ciWorkflowCheck =
    pkgs.runCommand "ci-workflow-check"
      {
        nativeBuildInputs = [
          pkgs.yq-go
          pkgs.jq
        ];
      }
      (
        lib.concatStrings (
          lib.mapAttrsToList (name: wf: ''
            yq --output-format=json '.' ${wf.committed} | jq --sort-keys . > committed.json
            jq --sort-keys . < ${pkgs.writeText "${name}-rendered.json" (builtins.toJSON wf.value)} > rendered.json
            if ! diff --unified committed.json rendered.json; then
              echo >&2
              echo ".github/workflows/${name}.yaml and ci/workflows/${name}.nix disagree." >&2
              echo "Run: nix run --file . ci-workflow-update" >&2
              exit 1
            fi
          '') ciWorkflows
        )
        + "touch $out\n"
      );

  # `docs/presentation.dot`, drawn from the functions CSI and NRI decide
  # with. src/presentation.py imports nothing, so two files are enough.
  presentationChart = pkgs.runCommand "presentation.dot" { nativeBuildInputs = [ pkgs.python3 ]; } ''
    mkdir src
    cp ${./pkgs/nixkube/src/__init__.py} src/__init__.py
    cp ${./pkgs/nixkube/src/presentation.py} src/presentation.py
    python3 -m src.presentation > $out
  '';

  presentationChartIsCurrent = pkgs.runCommand "presentation-chart-is-current" { } ''
    if ! diff --unified ${./docs/presentation.dot} ${presentationChart}; then
      echo >&2
      echo "docs/presentation.dot and src/presentation.py disagree." >&2
      echo "Run: nix run --file . presentation-chart-update" >&2
      exit 1
    fi
    touch $out
  '';

  presentation-chart-update = pkgs.writeScriptBin "presentation-chart-update" ''
    #! ${pkgs.runtimeShell}
    cp --no-preserve=mode ${presentationChart} docs/presentation.dot
  '';

  ci-workflow-update = pkgs.writeScriptBin "ci-workflow-update" ''
    #! ${pkgs.runtimeShell}
    set -euo pipefail
    ${lib.concatStrings (
      lib.mapAttrsToList (
        name: file: "cp --no-preserve=mode ${file} .github/workflows/${name}.yaml\n"
      ) ciWorkflowFiles
    )}
  '';
}
