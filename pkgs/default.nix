# SPDX-License-Identifier: Apache-2.0

self: pkgs:
let
  # The caller's sources, when it names them, and this repository's own lock
  # otherwise. Reading `nix/sources.nix` here when a caller passed a set
  # resolved the nixidae revision this commit was written against, so bumping
  # the caller's never moved pynixd (#79).
  sources = pkgs.nixkubeSources or (import ../nix/sources.nix);
in
{
  # Overlay lib
  lib = pkgs.lib.extend (import ../lib);

  # First argument is NIX_STATE_DIR which is where we init the dumped database
  nix_init_db =
    pkgs.writeScriptBin "nix_init_db" # bash
      ''
        #! ${pkgs.runtimeShell}
        NSD="$1"
        shift
        export USER nobody
        nix-store --option store local --dump-db "$@" | NIX_STATE_DIR="$NSD" nix-store --load-db --option store local
      '';

  # This repository's Python projects, built by pyproject.nix. `self` and not
  # `pkgs`, because the set lifts `kr8s` and `pynixd` from this overlay. There
  # is no cycle: the set reads those two and none of the applications below.
  pythonSet = import ../nix/python-set.nix {
    inherit (self) lib;
    inherit sources;
    pkgs = self;
  };

  mkApp = self.callPackage ../nix/mk-app.nix {
    inherit ((import sources.nanopynix { inherit pkgs sources; }).lib) mkApp;
  };

  nixkube = self.mkApp {
    name = "nixkube";
    inherit (self) pythonSet;
    # Found with `shutil.which`, every one of them. `coreutils` is the static
    # multicall binary: the NRI hook chroots into the host store and runs it
    # there, so it cannot depend on the container's loader.
    pathInputs = [
      pkgs.pkgsStatic.coreutils
      pkgs.gitMinimal
      (pkgs.lib.getBin self.nix)
      self.nix_init_db
      (pkgs.lib.getBin pkgs.openssh)
      (pkgs.lib.getBin pkgs.util-linuxMinimal)
      self.nri-wait
      # mkcomposefs and mount.composefs: a CSI volume as one mount.
      pkgs.composefs
    ];
    # `startup.py` hardlinks all three into the container root.
    env = {
      SETUP_BINSH = pkgs.dockerTools.binSh;
      SETUP_CACERTS = pkgs.dockerTools.caCertificates;
      SETUP_USRBINENV = pkgs.dockerTools.usrBinEnv;
      # Copied into a VM container's /nix; see src/nri/vm.py.
      NRI_VM_BUSYBOX = "${pkgs.pkgsStatic.busybox}/bin/busybox";
      # The tools of nix/composefs.nix, as `composefsImage` passes them, so a
      # node writes CI's derivation and finds CI's image. src/csi/composefs.py.
      NIXKUBE_COMPOSEFS = pkgs.composefs;
      NIXKUBE_NIX = self.nix;
    };
    selfEnv = "NIXKUBE_PACKAGE";
  };

  # kluctl = pkgs.kluctl.override {
  #   python310 = pkgs.python3;
  # };

  # Nix 2.35, not nixpkgs' default 2.34. A 2.34 store refuses every
  # content-addressed path with a self-reference that 2.35 built: its
  # RewritingSink calls `rewriteStrings(s, rewrites)` without `&matches`, so
  # HashModuloSink hashes no `|offset` and the hash differs. pydd's Python
  # outputs are such paths, and sg1's nodes run 2.35.3. Stock, so the binary
  # cache still answers for it.
  nix = pkgs.nixVersions.nix_2_35;

  # No override on the derivation of `nix`.
  #
  # This used to be `pkgs.nix.overrideAttrs { doCheck = false;
  # doInstallCheck = false; }`, to skip Nix's own test suite. It changed the
  # derivation hash, so cache.nixos.org could never answer for it:
  #
  #   overridden  bq05h82hrbjar92g6fm64g56vj3msgy6-nix-2.34.8   404
  #   stock       j02vvifyzjw52xqrvyj1bhd8s45yn1fl-nix-2.34.8   200
  #
  # Every CI runner then built Nix from source, on both architectures, and
  # `build-amd64` and `build-arm64` both hit GitHub's six-hour job limit. That
  # skipped `build-manifests`, which publishes the image manifest and pushes
  # to cachix, so nodes could not find `nodeEnv` to boot with. Issue #9, now
  # closed.
  #
  # Read the paragraph above as the trap, not as the current state. It said
  # "nothing has published since", which stayed true for a later and
  # unrelated CI break and sent someone to a fixed issue. An outage of this
  # shape looks the same whatever stopped the publish, so diagnose it from
  # the run list rather than from here.
  #
  # Skipping the test suite saves minutes on a build nobody should be doing.
  # Paying for it with a source build of Nix, twice per run, is the wrong
  # trade. Anything that wants to skip those checks locally has to do it
  # without changing the hash of what CI publishes.

  # The libraries the applications import are members of `pythonSet`, not
  # attributes here. They have no consumer outside that set, and a nixpkgs
  # build of one beside its venv build would be a second package that nothing
  # keeps in step.
  python-jsonpath = pkgs.python3Packages.callPackage ./python-jsonpath.nix { };
  kr8s = pkgs.python3Packages.callPackage ./kr8s.nix { inherit (self) python-jsonpath; };

  # NRI wait Python application for OCI hooks
  # Runs inside chroot(/var/lib/nix-csi), uses pyzmq for communication
  nri-wait = self.mkApp {
    name = "nri-wait";
    inherit (self) pythonSet;
  };

  # **Its own PATH, and a short one.** This is what runs when the version the
  # deployment asks for cannot be fetched, so it carries only the tools it
  # calls itself. `gitMinimal` is here because Nix shells out to it for a
  # flake reference, not because appstarter does.
  appstarter = self.mkApp {
    name = "appstarter";
    inherit (self) pythonSet;
    pathInputs = [
      (pkgs.lib.getBin self.nix)
      (pkgs.lib.getBin pkgs.openssh)
      pkgs.gitMinimal
    ];
    # `setup.py` installs these three files into the container root, so that
    # `nix build` resolves a user at all.
    env.FAKE_NSS = self.appstarter-fake-nss;
    passthru.fakeNss = self.appstarter-fake-nss;
  };

  # The group alone, with no `nixbld` users under it. `appstarter init`
  # builds with `--option sandbox false`, so every build runs as root.
  appstarter-fake-nss = pkgs.dockerTools.fakeNss.override {
    extraGroupLines = [ "nixbld:x:30000:" ];
  };

  # The NixOSRollout controller, which evaluates and builds sources. It brings
  # nix, git and ssh along, and installs the fake NSS files first: ssh exits
  # when its user has no passwd entry. It brings its CA bundle and /bin/sh
  # too, because the scratch image has neither (measured on sg1): git's
  # https fetch fails with "unable to get local issuer certificate", and the
  # ssh-ng store's `LocalCommand=echo started` never runs, so nix waits for
  # "started" while pynixd waits for nix's handshake, forever. $SHELL does
  # not help: nix's createSSHEnv sets SHELL=/bin/sh for ssh itself.
  nixkube-rollout-controller = self.writeShellApplication {
    name = "nixkube-rollout-controller";
    runtimeInputs = [
      self.coreutils
      self.gitMinimal
      self.nix
      self.rsync
      self.openssh
      self.kubectl
    ];
    text = ''
      mkdir -p /tmp
      rsync --archive ${self.appstarter-fake-nss}/ /
      export SSL_CERT_FILE=${self.cacert}/etc/ssl/certs/ca-bundle.crt
      export NIX_SSL_CERT_FILE=$SSL_CERT_FILE GIT_SSL_CAINFO=$SSL_CERT_FILE
      mkdir -p /bin
      ln -sfn ${self.runtimeShell} /bin/sh
      exec ${self.nixkube}/bin/nixkube-nixos-rollout "$@"
    '';
  };

  # What the controller's pod mounts through the node's CSI driver, as a
  # pynixd builder does: the controller runs out of the store, so a change to
  # it reaches a cluster with an apply and no new image.
  nixkube-rollout-env = self.buildEnv {
    name = "nixkube-rollout-env";
    paths = [
      self.nixkube-rollout-controller
      self.tini
    ];
  };

  # The switch Job's program. It copies its own closure into the host's store
  # and runs on from there, so that closure is python3Minimal and one file:
  # every switched host carries it.
  nixkube-nixos-switch = self.writeTextFile {
    name = "nixkube-nixos-switch";
    destination = "/bin/nixkube-nixos-switch";
    executable = true;
    text = "#!${self.python3Minimal}/bin/python3\n" + builtins.readFile ./nixkube/src/nixos_switch.py;
  };

  # What the switch Job's pod mounts through the CSI driver. Only the program
  # goes on to the host; nix, ssh, bash and the fake NSS serve the copy in
  # the pod.
  nixkube-nixos-switch-env = self.buildEnv {
    name = "nixkube-nixos-switch-env";
    paths = [
      self.nixkube-nixos-switch
      self.nix
      self.openssh
      self.bash
      self.appstarter-fake-nss
    ];
  };

  ci-debug = pkgs.callPackage ./ci-debug { inherit pkgs; };

  # The two kubernetes-csi sidecars the node DaemonSet runs, and images for
  # them. Built here so a node with no route to registry.k8s.io can still run
  # the driver -- which is every node inside a Nix build sandbox. See
  # ./csi-sidecars.
  csi-sidecars = pkgs.callPackage ./csi-sidecars { };

  # From the caller's sources, like every other source, and never a sibling
  # directory or a branch name: either one builds a pynixd that no lock
  # records, so CI and a working copy ship different `cacheEnv`s and a node
  # asks its substituters for a path nobody built.
  pynixd = (import sources.pynixd { inherit pkgs sources; }).packages.pynixd-lib;
  pynixd-nixkube-fake-nss = pkgs.callPackage ./pynixd-nixkube/fake-nss.nix { };

  pynixd-nixkube = self.mkApp {
    name = "pynixd-nixkube";
    inherit (self) pythonSet;
    # Three programs, so pyproject.nix names none of them.
    mainProgram = "pynixd-nixkube";
    # `pynixd_nixkube/setup.py` reads both at import time, so an unset one is
    # a `KeyError` on the first import rather than a missing file later.
    env = {
      FAKE_NSS = self.pynixd-nixkube-fake-nss;
      CA_CERTS = pkgs.dockerTools.caCertificates;
    };
    # `environments/cache` installs the same passwd and group files into the
    # container root, and reads them off the application so the two cannot
    # name different ones.
    passthru.fakeNss = self.pynixd-nixkube-fake-nss;
  };
}
