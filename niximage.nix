# SPDX-License-Identifier: Apache-2.0

{
  pkgs,
  lib,
  sources ? import ./nix/sources.nix,
  # The cache environment is a dinit tree, so it needs the same `dinix` the
  # deployment builds it with. `kubenix/options.nix` holds the option; this
  # takes the source directly, because nothing here evaluates that module.
  dinix ? sources.dinix,
  nixkubeVersion ?
    (builtins.fromTOML (builtins.readFile ./pkgs/nixkube/pyproject.toml)).project.version,
}:
rec {
  server = "ghcr.io";
  repo = "${server}/nixidae/nixkube";

  images = lib.genAttrs [ "aarch64-linux" "x86_64-linux" ] (
    system:
    let
      sysPkgs = import pkgs.path {
        inherit system;
        overlays = [
          (final: prev: { nixkubeSources = sources; })
          (import ./pkgs)
        ];
      };
      inherit (sysPkgs) lib;

      # The same `/etc` `appstarter init` installs for itself. One definition,
      # so the two containers of a pod resolve a user the same way.
      fakeNss = sysPkgs.appstarter-fake-nss;

      # The fallback the image carries, one per role. `appstarter init` takes
      # it when it cannot fetch what the deployment asks for, and it is useful
      # precisely because it lags: it is whatever was current at build time.
      #
      # Naming these in the pod spec instead would not work. A pod spec comes
      # out of the same evaluation as `APPSTARTER_WANTED`, so the fallback
      # would be the identical path.
      nodeEnv = sysPkgs.callPackage ./environments/node { };
      cacheEnv = sysPkgs.callPackage ./environments/cache { inherit dinix; };

      runtimeInputs = [
        sysPkgs.coreutils
        sysPkgs.gitMinimal
        sysPkgs.nix
        sysPkgs.rsync
        sysPkgs.openssh
        sysPkgs.kubectl
      ];

      init-secrets = sysPkgs.writeShellApplication {
        name = "init-secrets";
        inherit runtimeInputs;
        text = # bash
          ''
            set -euo pipefail
            set -x
            mkdir /tmp
            rsync --archive ${fakeNss}/ /
            # shellcheck source=/dev/null
            source /opt/bin/init-secrets
          '';
      };

      # The image's PATH has no nix: the node and pynixd get theirs from the
      # environment appstarter starts.

      # The NixOSRollout controller, which evaluates and builds sources. The
      # Deployment runs it out of the store; the image keeps it for one that
      # still names it by PATH.
      rollout-controller = sysPkgs.nixkube-rollout-controller;

      n2c = (import sources.nix2container { pkgs = sysPkgs; }).nix2container;

      policy = import ./nix/image-layers.nix {
        pkgs = sysPkgs;
        inherit nodeEnv cacheEnv;
      };

      /*
        Base first, each link holding every link before it. `layers` is the
        dedupe: a path an earlier layer already carries is not repeated, and
        the Go side needs every parent's `layers.json` to know that, so the
        whole prefix has to go in.

        `nestedLayers = built` is not cosmetic. nix2container computes it as
        `flatten (map (l: l.nestedLayers) layers) ++ layers`, which counts
        every layer once per path through the chain: 2^(k-1) - 1 entries for
        link k. `buildImage` then puts the lot in one shell argument, and 13
        links come to 8191 entries and about 450 KB -- past Linux's 128 KB
        limit for a single argument:

          jq: Argument list too long

        `built` is that same set of layers with the duplicates gone, so the
        chain stays linear. Measured: 91 entries for the 13 links here.
      */
      chain = builtins.foldl' (
        built: group:
        built
        ++ [
          (
            n2c.buildLayer {
              inherit (group) deps;
              layers = built;
            }
            // {
              nestedLayers = built;
            }
          )
        ]
      ) [ ] policy.groups;
    in
    n2c.buildImage {
      name = "${repo}/nix";
      tag = "${nixkubeVersion}-${sysPkgs.nix.version}-${sysPkgs.stdenv.hostPlatform.system}";
      arch = sysPkgs.go.GOARCH;

      layers = chain;
      # The remainder only. `maxLayers` does not apply to the named layers.
      maxLayers = policy.restLayers;

      # `nix` runs against the image's own store in the initContainer, and a
      # store with no database is one Nix declines to read.
      initializeNixDatabase = true;

      # One env, not the three paths. nix2container leaves every
      # `copyToRoot` path out of the image's database and disallows it in
      # the closure graph (nix2container e63a2619). These three are also in
      # the closure of `nixkube` and `pynixd-nixkube`, which name them, so
      # the graph refers to them and the build failed. Only the env is left
      # out now; the three stay in the store and the database (#80).
      #
      # `symlinkJoin`, not `buildEnv`: buildEnv turns a directory only one
      # path provides into one symlink, so `/etc` pointed into the
      # `ca-certificates` store path, appstarter wrote `/etc/passwd` into
      # it, and seeding from the image failed on a hash mismatch.
      copyToRoot = [
        (sysPkgs.symlinkJoin {
          name = "nix-image-root";
          paths = [
            sysPkgs.dockerTools.binSh
            sysPkgs.dockerTools.caCertificates
            sysPkgs.dockerTools.usrBinEnv
          ];
        })
      ];
      config = {
        Entrypoint = [
          (lib.getExe sysPkgs.appstarter)
          "init"
        ];
        Env = [
          "PATH=${
            lib.makeBinPath [
              sysPkgs.appstarter
              init-secrets
              rollout-controller
              # `nixkube-host-check`, the first init container when
              # `nixkube.hostStore.enable` is on.
              sysPkgs.nixkube
            ]
          }"
          "APPSTARTER_FALLBACK_NODE=${nodeEnv}"
          "APPSTARTER_FALLBACK_CACHE=${cacheEnv}"
        ];
      };
    }
  );

  imageRef = system: "${images.${system}.imageName}:${images.${system}.imageTag}";

  /**
    The tag of one system's image, from its store hash.

    **The image is input-addressed, so this tag names one content.** It is
    known at evaluation, and the context is discarded, so a manifest that
    names it builds no image: the aarch64 one would need an aarch64 machine.
  */
  contentTag =
    system:
    builtins.substring 0 32 (
      builtins.baseNameOf (builtins.unsafeDiscardStringContext images.${system}.outPath)
    );

  /**
    What a cluster of `systems` names: the one image of a single system, or
    the index over several, tagged from the tags it lists.
  */
  refFor =
    systems:
    let
      tags = map contentTag (lib.sort lib.lessThan systems);
    in
    "${repo}/nix:${
      if lib.length tags == 1 then
        lib.head tags
      else
        builtins.substring 0 32 (builtins.hashString "sha256" (lib.concatStringsSep "-" tags))
    }";

  # skopeo, with nix2container's `nix:` transport. It reads the image's JSON
  # manifest and sends the store paths it names straight out of the store,
  # instead of piping a tarball through gzip.
  skopeo = (import sources.nix2container { inherit pkgs; }).skopeo-nix2container;

  # Per-arch push scripts (one per system, used by per-arch CI jobs)
  pushArch = lib.mapAttrs (
    system: image:
    pkgs.writeShellApplication {
      name = "push-nix-${system}";
      runtimeInputs = [
        skopeo
        pkgs.cachix
        pkgs.jq
      ];
      # **A content tag is pushed once, by whoever gets there first.** A dev
      # and CI pushing one commit push one content, so the second push is a
      # no-op. The config digest is compared rather than the manifest's:
      # skopeo gzips each layer on the way up, so the manifest digest also
      # depends on the compressor.
      text = # bash
        ''
          skopeo login -u="$REPO_USERNAME" -p="$REPO_TOKEN" ${server}
          tag=${repo}/nix:${contentTag system}
          want="$(skopeo inspect --raw nix:${image} | jq -r .config.digest)"
          if have="$(skopeo inspect --raw "docker://$tag" 2>/dev/null | jq -r .config.digest)"; then
            if [ "$have" != "$want" ]; then
              echo "$tag holds config $have, and this image is $want" >&2
              exit 1
            fi
            echo "present: $tag"
          else
            skopeo --insecure-policy copy nix:${image} "docker://$tag"
          fi
          # The manifest, not a tarball. Its closure is every layer, so a
          # node can fetch what the image holds without the registry.
          cachix push nixkube ${image}
        '';
    }
  ) images;

  # Manifest creation (used by dependent CI job after both arches are pushed)
  pushManifest = pkgs.writeShellApplication {
    name = "push-nix-manifest";
    runtimeInputs = [ pkgs.regctl ];
    text = # bash
      ''
        set -euo pipefail

        regctl registry login -u="$REPO_USERNAME" -p="$REPO_TOKEN" ${server}

        # The two tags come from two jobs on two runners. Say which one is
        # missing, rather than letting `index create` fail against a name
        # that reads like a typo.
        for ref in ${repo}/nix:${contentTag "aarch64-linux"} ${repo}/nix:${contentTag "x86_64-linux"}; do
          if ! regctl manifest head "$ref" >/dev/null 2>&1; then
            echo "missing: $ref" >&2
            echo "Its build job did not publish. An index over it would be wrong." >&2
            exit 1
          fi
          echo "present: $ref"
        done

        regctl index create ${bothRef} \
          --ref ${repo}/nix:${contentTag "aarch64-linux"} \
          --ref ${repo}/nix:${contentTag "x86_64-linux"}
        # The one moving tag, for a dev cluster that sets
        # `nixkube.images.nix` to it. No manifest names it by default.
        regctl index create ${repo}/nix:main \
          --ref ${repo}/nix:${contentTag "aarch64-linux"} \
          --ref ${repo}/nix:${contentTag "x86_64-linux"}
      '';
  };

  # What a two-system cluster names, and what a release pins by digest.
  bothRef = refFor [
    "aarch64-linux"
    "x86_64-linux"
  ];

  /**
    Give the release its version tags, and print the digest it pins.

    **A version tag is written once.** One that exists with another digest
    fails the release by name: it is the #29 shape, a released manifest whose
    image changed under it.
  */
  tagRelease = pkgs.writeShellApplication {
    name = "tag-nix-release";
    runtimeInputs = [ pkgs.regctl ];
    text = # bash
      ''
        regctl registry login -u="$REPO_USERNAME" -p="$REPO_TOKEN" ${server} >&2
        digest="$(regctl image digest ${bothRef})"
        version=${repo}/nix:${nixkubeVersion}-${pkgs.nix.version}
        if have="$(regctl image digest "$version" 2>/dev/null)"; then
          if [ "$have" != "$digest" ]; then
            echo "$version is $have, and this release is $digest." >&2
            echo "A released version tag is written once: bump version in pkgs/nixkube/pyproject.toml." >&2
            exit 1
          fi
        else
          regctl image copy ${bothRef} "$version" >&2
        fi
        regctl image copy ${bothRef} ${repo}/nix:latest >&2
        echo "$digest"
      '';
  };
}
