# SPDX-License-Identifier: Apache-2.0

# Every image the node DaemonSet names, as a docker-archive tarball.
#
# There is no registry inside a Nix build sandbox, so a node gets an image one
# way: vivarium imports it into containerd before kubelet starts, from
# `services.vivarium-k8s.extraImages`. What matters is the *tag* inside each
# tarball -- containerd matches on it, and a pod whose image nobody imported
# under that exact name sits in `ErrImagePull` until the test times out.
#
# Four images, from three different shapes:
#
#   nix       `nix2container`, so a JSON manifest that only its own skopeo
#             reads. Its tag is the content tag of this system's image, which
#             is what `nixkube.images.nix` names for a one-system cluster.
#   scratch   an OCI layout directory built by umoci, tagged by architecture.
#             The DaemonSet pins the index by digest, which no tarball can
#             carry, so ./manifest.nix names this tag instead.
#   sidecars  ordinary `dockerTools.buildImage` output, already a
#             docker-archive with the tag the DaemonSet asks for.
#
# skopeo does every conversion and every retag, and none of it touches the
# network: `docker-archive:` and `oci:` are both local.
{
  pkgs,
  lib,
  nixImage,
  scratchImage,
}:
let
  system = pkgs.stdenv.hostPlatform.system;

  # `skopeo copy <source> docker-archive:$out:<tag>`, which is the whole job.
  # `--tmpdir` because skopeo unpacks into TMPDIR and the default is small
  # next to a Nix closure.
  convert =
    {
      name,
      source,
      tag,
    }:
    pkgs.runCommand "uml-image-${name}.tar"
      {
        nativeBuildInputs = [ pkgs.skopeo ];
      }
      ''
        skopeo --insecure-policy --tmpdir "$TMPDIR" copy \
          ${source} "docker-archive:$out:${tag}"
      '';

  # nix2container writes a JSON manifest naming store paths, and only its own
  # skopeo reads that. `convert` uses the stock one, so the image becomes a
  # docker-archive here first.
  nixTar =
    pkgs.runCommand "nix-image.tar"
      {
        nativeBuildInputs = [ nixImage.skopeo ];
      }
      ''
        skopeo --insecure-policy --tmpdir "$TMPDIR" copy \
          nix:${nixImage.images.${system}} \
          "docker-archive:$out:${nixImage.imageRef system}"
      '';
in
rec {
  # What the DaemonSet asks for. The check below compares both with the
  # manifest, so a change on either side fails there rather than at ImagePull.
  tags = {
    nix = nixImage.refFor [ system ];
    scratch = "ghcr.io/nixidae/nixkube/scratch:1.0.1";
  };

  nix = convert {
    name = "nix";
    source = "docker-archive:${nixTar}";
    tag = tags.nix;
  };

  scratch = convert {
    name = "scratch";
    source = "oci:${scratchImage.x86Image}:amd64";
    tag = tags.scratch;
  };

  # Already the right shape and the right tag; nothing to convert.
  inherit (pkgs.csi-sidecars.images) csi-node-driver-registrar livenessprobe;

  tarballs = [
    nix
    scratch
    csi-node-driver-registrar
    livenessprobe
  ];

  /*
    What the images point at, as real Nix references.

    A tarball carries none. `dockerTools.buildImage` gzips its layers, so
    the reference scanner sees compressed bytes and records nothing -- and
    then vivarium bind-mounts the guest's /nix/store over each
    container's, hiding the copy the image brought with it. The entrypoint
    is a store path that is not in the sandbox, and runc says so:

        exec: ".../bin/livenessprobe": no such file or directory

    So the packages have to be named where Nix will see them.
    `services.vivarium-k8s.extraImages` says this in its description, and
    modules/k8s.nix does the same thing for kubeadm's own images through
    `system.extraDependencies`. ./default.nix passes these to it.

    `nix` and `scratch` are not here. skopeo writes an uncompressed tar, so
    those two are scanned like any other file and carry their own
    references -- measured: `appstarter` and its whole closure resolve
    inside the guest.
  */
  runtimeInputs = [
    pkgs.csi-sidecars.csi-node-driver-registrar
    pkgs.csi-sidecars.livenessprobe
  ];

  /*
    Do the tarballs carry the tags the manifest names?

    Cheap, and the alternative is finding out from an ErrImagePull twenty
    minutes into a cluster test -- the same reason vivarium keeps
    `check-k8s-images` over kubeadm's own list.

    The manifest is the authority: this reads the image strings straight out
    of the rendered JSON rather than out of a list somebody has to remember
    to update.
  */
  check =
    manifestJSONFile:
    pkgs.runCommand "uml-images-match-manifest"
      {
        nativeBuildInputs = [
          pkgs.jq
          pkgs.gnutar
          pkgs.gzip
        ];
      }
      ''
        jq --raw-output '
          [ .. | objects | select(has("image")) | .image ] | unique | .[]
        ' ${manifestJSONFile} | sort > wanted

        : > got
        for tarball in ${lib.escapeShellArgs tarballs}; do
          zcat -f "$tarball" \
            | tar --extract --to-stdout manifest.json \
            | jq --raw-output '.[].RepoTags[]' >> got
        done
        sort -o got got

        if ! diff -u wanted got; then
          echo
          echo "the manifest names an image no tarball carries, or the other way round."
          echo "left is what kubenix/daemonset.nix asks for; right is what these tarballs hold."
          exit 1
        fi
        touch $out
      '';
}
