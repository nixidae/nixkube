# SPDX-License-Identifier: Apache-2.0

# nixkube on a real Kubernetes node, inside a Nix build sandbox.
#
#     nix build --file . umlTest
#
# This runs the node as an ordinary process under User-Mode Linux -- no KVM,
# no root, no tap device -- so the whole thing is a derivation that passes or
# fails, and CI needs nothing but a builder.
#
# What makes that possible is `vivarium.recipes.cache`: a second guest
# serves every store path the run needs, and the node, pynixd and the host
# itself substitute from it and from nothing else, as a real cluster does
# from its binary cache.
#
# The phases are cluster, deploy, workloads, chaos (a pytest phase, one test
# per scenario), runtimes, host and report. By hand, with the guest held open on a failure:
#
#     nix run --file . umlTest.driverDebug -- --out ./o
#     nix run --file . umlTest.driver -- --out ./o -- -k runtime
{
  pkgs,
  lib,
  sources,
  umlImages,
  manifest,
  /*
    One cell of `umlMatrix`: `{ backend, cri, kata }` fixed here, with no
    knob for them, so an environment variable cannot make a cell run
    something other than its name says. `null` is `umlTest`, which reads
    the knobs.

    `erofs = false` refuses the guest kernel's EROFS module, so the node's
    own probe, not an option, has to pick the fallbacks. QEMU only: the UML
    kernel builds EROFS in.
  */
  matrixCell ? null,
}:
let
  vivarium = import (sources.vivarium + "/lib.nix") { inherit pkgs sources; };

  # The rendered manifest, and the root of everything the node needs.
  #
  # `nixkube.discardStringContext = false` (see ./manifest.nix) keeps the
  # string context on the DaemonSet's store paths, so the node environment is
  # in this file's closure. Naming it in `settings` does both jobs: it pulls
  # the closure into the sandbox, and `mkTest` registers what `settings`
  # names, which is what makes Nix inside the guest agree the paths are real.
  manifestFile = manifest.manifestYAMLFile;

  probes = pkgs.callPackage ./probe.nix { };

  # `nixkube.hostMountPath` in ./manifest.nix. The driver's own paths are
  # relative to it, and `settings.hostMountPath` below tells the test where
  # to look for them.
  hostMountPath = "/nixkube";

  # What the cache guest serves: the manifest, whose closure carries the
  # node environment; the probes, whose closures carry what they run; and
  # what a workload asks the driver to mount. The host systems join them
  # below, where the booted guest is known.
  cacheRoots = [
    manifestFile
    probes.ro
    probes.rw
    pkgs.hello
  ];

  # What both nodes share. kubeadm hands the worker the control plane's
  # KubeletConfiguration, so `kubeletSettings` reaches both from here.
  nodeBase = index: role: {
    imports = [ (sources.vivarium + "/modules/k8s.nix") ];

    services.vivarium-k8s = {
      enable = true;
      inherit role;

      # A Lease renewal every 1.25 s, not 10 s: the host phase confirms a
      # switch by one.
      kubeletSettings.nodeLeaseDurationSeconds = 5;

      # There is no registry. Every image the DaemonSet names is imported
      # into containerd before kubelet starts; ./images.nix checks that the
      # tags match what the manifest asks for.
      extraImages = umlImages.tarballs;

      # nixkube's other mount path. The node DaemonSet asks containerd for
      # an NRI connection whether or not containerd is listening, and gets
      # no error when it is not -- so without this the plugin waits, the
      # pods that need it start without a /nix, and nothing says why.
      nri = true;
    };

    vivarium = {
      cpus = 4;
      diskSize = 4096;
      lan = {
        network = "nixkube";
        address = "10.103.0.${toString index}/24";
      };
    };

    # What the sidecar images point into the store, named where Nix can see
    # it. Their layers are gzipped, so nothing else says these paths are
    # needed, and a container whose entrypoint is missing fails in runc
    # rather than anywhere informative. See ./images.nix.
    system.extraDependencies = umlImages.runtimeInputs;

    # For looking around by hand when something fails. The pods get their own
    # configuration from the ConfigMap the manifest carries, not from this.
    nix.settings.experimental-features = [
      "nix-command"
      "flakes"
    ];

    # The chaos scenarios compare what the driver left on the node against
    # what the node says is still alive, and both answers are JSON.
    environment.systemPackages = [ pkgs.jq ];
  };

  cpNode = {
    imports = [ (nodeBase 1 "control-plane") ];

    # One, for pynixd's `nix-store` claim. The StorageClass it creates is
    # `standard`, annotated as the cluster default, which is what
    # `nixkube.pynixd.storageClassName = null` asks for. `bring_up`
    # applies it. Issue #49. Only here: a volume on the worker could bind
    # the claim and hold pynixd on a node it cannot schedule on.
    services.vivarium-k8s.persistentVolumes = 1;

    vivarium = {
      /*
        Measured, with MemTotal - MemAvailable sampled every 2s over a
        whole run (2026-09-30, QEMU): the working set peaks at 1.56 GiB,
        in `runtimes`, and anonymous memory at 822 MiB. At 8192M the host
        still paid 6.3 GB, because a guest fills its RAM with page cache
        and keeps it. At 2048M the run passed and the host paid 2.0 GB,
        but MemFree fell to 64 MiB against kubelet's 50Mi eviction line.
        So this is the lowest that passed, plus a few percent.

        The Kata cells pass at 2200M too (2026-10-01): the working set
        peaks at 1.46 GiB with CRI-O and 1.53 GiB with containerd, in
        `runtimes`, and MemFree bottoms at 64 MiB, as without Kata.

        The same for both backends. vivarium issue #19 is swap, which
        would let this sit nearer the working set.

        The disk stays at 4096. A runner has 14 GB of it for the store,
        the qcow2 and everything else, and this workload has never needed
        more.

        No `nixDatabase` here. `mkTest` registers the closure of everything
        in `settings`, which the phases read on the node: the manifest and
        both probes. What the node runs it fetches from the cache guest.
      */
      memory = "2200M";
    };
  };

  /*
    A second NixOS node, for the host phase to roll out across two nodes.
    `phases/cluster.py` taints it, so every other phase runs on `cp` alone;
    the switch Jobs name their node and tolerate every taint. It runs
    kubelet, the CRI and those Jobs, so vivarium's worker size fits.
  */
  workerNode = {
    imports = [ (nodeBase 2 "worker") ];
    vivarium.memory = "1280M";
  };

  /*
    A booted guest plus one file, for the host phase to switch to and
    back from. The switch then changes that file, and no unit the cluster
    runs on. `extendModules` on the guest `mkTest` booted, so it carries
    everything that guest does -- the cache recipe's interface and
    substituter included.
  */
  hostNext =
    node: extra:
    (node.extendModules {
      modules = [
        { environment.etc."nixkube-host-generation".text = "next\n"; }
        extra
      ];
    }).config.system.build.toplevel;

  # A kubelet that starts and never renews its Lease. switch-to-configuration
  # succeeds, no pod runs afterwards, and only the host's timer reverts. A
  # kubelet that fails to start would take the switch Job's own revert
  # instead, and leave the timer untested.
  hostBroken =
    cp:
    hostNext cp {
      systemd.services.kubelet.serviceConfig.ExecStart =
        lib.mkForce "${lib.getExe' pkgs.coreutils "sleep"} infinity";
    };

  # The RuntimeClasses besides runc a pod here can ask for. gVisor runs only
  # under QEMU and containerd; see `services.vivarium-k8s.runtimes`.
  runtimesFor =
    {
      backend,
      cri,
      kata,
    }:
    [
      "crun"
      "youki"
    ]
    ++ lib.optional (backend != "uml" && cri == "containerd") "runsc"
    ++ lib.optional kata "kata";

  cpFor =
    { cri, kata }:
    { config, ... }:
    {
      imports = [ cpNode ];
      services.vivarium-k8s = {
        inherit cri;
        runtimes = runtimesFor {
          inherit (config.vivarium) backend;
          inherit cri kata;
        };
      };
      vivarium.nestedVirtualization = kata;
    };

  # "kubelet restarts,runtime" to `-k "kubelet-restarts or runtime"`,
  # the ids nix/uml/chaos/ gives the scenarios.
  scenarioFilter =
    text:
    lib.optionals (text != "") [
      "-k"
      (lib.concatMapStringsSep " or " (s: lib.replaceStrings [ " " ] [ "-" ] (lib.trim s)) (
        lib.filter (s: lib.trim s != "") (lib.splitString "," text)
      ))
    ];

  # "cluster,deploy,host" keeps those phases and the `always` ones, and drops
  # edges to the rest. "boot" is vivarium's own and always there.
  phaseFilter =
    text: phases:
    let
      wanted = map lib.trim (lib.filter (s: lib.trim s != "") (lib.splitString "," text));
      unknown = lib.filter (name: !phases ? ${name}) wanted;
      kept = lib.filterAttrs (name: phase: lib.elem name wanted || phase.always or false) phases;
    in
    if text == "" then
      phases
    else if unknown != [ ] then
      throw "NIXKUBE_UML_PHASES names unknown phases: ${lib.concatStringsSep ", " unknown}; known: ${lib.concatStringsSep ", " (lib.attrNames phases)}"
    else
      lib.mapAttrs (
        _: phase: phase // { after = lib.filter (a: a == "boot" || kept ? ${a}) phase.after; }
      ) kept;
in
vivarium.mkTest (
  { config, systems, ... }:
  let
    backend = if matrixCell != null then matrixCell.backend else config.resolved.backend.value;
    cri = if matrixCell != null then matrixCell.cri else config.resolved.cri.value;
    kata = if matrixCell != null then matrixCell.kata else config.resolved.kata.value == "1";
    hostStore = manifest.config.nixkube.hostStore.enable;
    erofs = if matrixCell != null then matrixCell.erofs or true else true;
  in
  {
    name =
      if matrixCell != null then
        "nixkube-${backend}-${cri}${lib.optionalString kata "-kata"}"
      else
        "nixkube";

    vivarium.recipes.cache = {
      enable = true;
      roots = map toString (
        cacheRoots
        ++ [
          (hostNext systems.cp { })
          (hostNext systems.worker { })
          (hostBroken systems.cp)
        ]
      );
    };

    # What every phase and the chaos tests import: waits, probes, checks.
    # Not `lib/`, which .gitignore takes for a Python build directory.
    pythonPath = [ ./helpers ];

    knobs = {
      /*
        Which chaos scenarios to run, by name, comma separated.

        Empty runs all nine, which is what CI wants and what takes most of
        the run. Iterating on one costs the cluster and that scenario:

            NIXKUBE_UML_SCENARIOS=runtime nix build --file . umlTest

        By hand, `-k` does the same without a new evaluation:

            nix run --file . umlTest.driver -- --out ./o -- -k runtime
      */
      scenarios = {
        env = "NIXKUBE_UML_SCENARIOS";
        default = "";
        description = "chaos scenarios to run, by name; empty is all";
      };
      /*
        Which phases to run, by name, comma separated; `report` always runs.
        Empty runs all, which is the gate before landing. The `after` edges
        only order the phases, so name what a phase needs as well:

            NIXKUBE_UML_PHASES=cluster,deploy,host nix build --file . umlTest
      */
      phases = {
        env = "NIXKUBE_UML_PHASES";
        default = "";
        description = "phases to run, by name; empty is all";
      };
    }
    // lib.optionalAttrs (matrixCell == null) {

      /*
        QEMU by default, so `nix build --file . umlTest` is the machine CI
        runs and the one a developer runs.

        A UML guest is one process and one CPU whatever `cpus` says, which
        is what makes it work inside a build sandbox and what makes it slow
        here: this test is a control plane, a CSI driver, an NRI plugin and
        nine chaos scenarios. Measured under QEMU at four processors: 13.4
        minutes for all nine, and the guest boots in 8.5s.

        `NIXKUBE_UML_BACKEND=uml` is the same test on the other machine, for
        a host with no /dev/kvm -- a builder without one refuses to build the
        QEMU variant rather than failing it.
      */
      backend = {
        env = "NIXKUBE_UML_BACKEND";
        default = "qemu";
        description = "qemu, or uml where there is no /dev/kvm";
      };

      /*
        The CRI the node runs: containerd, or crio. The same test either
        way, which is the question issue #74 asks of CRI-O.

            NIXKUBE_UML_CRI=crio nix build --file . umlTest
      */
      cri = {
        env = "NIXKUBE_UML_CRI";
        default = "containerd";
        description = "containerd or crio";
      };

      /*
        Kata Containers as a further RuntimeClass, on either CRI. It starts
        a VM per pod, so the host needs nested virtualization, and a GitHub
        runner has none: by hand only.

            NIXKUBE_UML_KATA=1 NIXKUBE_UML_CRI=crio nix build --file . umlTest
      */
      kata = {
        env = "NIXKUBE_UML_KATA";
        default = "";
        description = "1 to offer Kata Containers";
      };
    };

    inherit backend;

    phases = phaseFilter config.resolved.phases.value (
      {
        cluster = {
          script = ./phases/cluster.py;
          after = [ "boot" ];
        };
        deploy = {
          script = ./phases/deploy.py;
          after = [ "cluster" ];
        };
        workloads = {
          script = ./phases/workloads.py;
          after = [ "deploy" ];
        };
        # After chaos, so a scheduler that picks chaos first cannot leave
        # these waiting behind a breakpoint on it.
        runtimes = {
          script = ./phases/runtimes.py;
          after = [ "chaos" ];
        };
        host = {
          script = ./phases/host.py;
          after = [ "runtimes" ];
        };
        chaos = {
          pytest = {
            tests = ./chaos;
            args = scenarioFilter config.resolved.scenarios.value;
          };
          after = [ "workloads" ] ++ lib.optional hostStore "hoststore";
        };
        report = {
          script = ./phases/report.py;
          after = [ "host" ];
          always = true;
        };
      }
      // lib.optionalAttrs hostStore {
        # The node shares the guest's own /nix: a NixOS host. Issue #25.
        hoststore = {
          script = ./phases/hoststore.py;
          after = [ "workloads" ];
        };
      }
    );

    nodes.cp = {
      imports = [
        (cpFor { inherit cri kata; })
      ]
      ++ lib.optional (!erofs) {
        # What a kernel without EROFS answers: `mount -t erofs` finds no
        # filesystem, as on Talos and GKE's COS.
        boot.extraModprobeConfig = "install erofs ${pkgs.coreutils}/bin/false";
      };
    };
    nodes.worker = {
      imports = [ workerNode ];
      services.vivarium-k8s = { inherit cri; };
    };

    settings = {
      manifest = "${manifestFile}";
      # One pod using both mount paths, with a generated name, created after
      # the driver is up and again after each thing that breaks it. See
      # ./probe.nix.
      probeRo = "${probes.ro}";
      probeRw = "${probes.rw}";

      # What to run inside the resident pod to ask whether it still has what
      # it was given. The first only resolves through the CSI volume; the
      # second prints the mount table the NRI plugin wrote. `/mnt/csi` is the
      # same literal ./workloads.nix and ./probe.nix use.
      residentHello = "/mnt/csi${lib.getExe pkgs.hello}";
      # From the resident's *own* closure, not the node's. The NRI mount is a
      # narrowed /nix holding what that container needs and nothing else --
      # which is the point of nixkube -- so a binary from anywhere else is not
      # in there. Measured: busybox was not, and the exec failed with "no such
      # file or directory" on a path the node certainly has.
      residentCat = "${lib.getExe' pkgs.coreutils "cat"}";

      # Where the driver's own state lands on the node -- see CSI_ROOT in
      # pkgs/nixkube/src/constants.py, which is a path inside the node
      # container, and this is what its /nix comes from.
      inherit hostMountPath;
      kubernetesVersion = pkgs.kubernetes.version;
      # What a workload will ask the CSI driver to mount. A store path this
      # file names is a path the sandbox has.
      workloadStorePath = "${pkgs.hello}";
      workloadImage = "vivarium.test/busybox:1";
      runtimes = runtimesFor { inherit backend cri kata; };
      inherit cri;
      # How the node presents a CSI volume and an NRI /nix: composefs where
      # the manifest asks for it and the kernel has EROFS. The workloads
      # phase checks every CSI mount, and each probe its NRI /nix.
      csiComposefs = manifest.config.nixkube.csi.composefs && erofs;
      nriComposefs = manifest.config.nixkube.nri.composefs && erofs;
      # Whether the node runs from the guest's own /nix, and so which
      # DaemonSet serves it. Issue #25.
      inherit hostStore;
      nodeDaemonSet = if hostStore then "nix-node-host" else "nix-node";
      # Strings, not store paths: `settings` registers what it names on
      # every guest, and the host phase has to fetch these from the cache
      # guest, as a real host would.
      hostNext = lib.mapAttrs (_: node: builtins.unsafeDiscardStringContext "${hostNext node { }}") {
        inherit (systems) cp worker;
      };
      hostBroken = builtins.unsafeDiscardStringContext "${hostBroken systems.cp}";
    };
  }
)
