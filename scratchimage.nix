# SPDX-License-Identifier: Apache-2.0

{ pkgs }:
let
  version = "1.0.1";

  mkEmptyImage =
    GOARCH:
    pkgs.runCommand "scratch-${GOARCH}-${version}"
      {
        nativeBuildInputs = [ pkgs.umoci ];
      }
      ''
        umoci init --layout $out
        umoci new --image $out:${GOARCH}
        umoci config --image $out:${GOARCH} \
          --config.env PATH=/nix/var/result/bin \
          --architecture ${GOARCH} \
          --os linux
      '';

  x86Image = mkEmptyImage "amd64";
  armImage = mkEmptyImage "arm64";

  /**
    The multi-arch index that `push` publishes as `scratch:${version}`, and
    its digest, built here rather than read back from the registry.

    **The bytes are the ones `crane index append` writes**, key order included,
    so this digest is the published one. umoci honours `SOURCE_DATE_EPOCH`, so
    both images are reproducible, and the index is a function of their two
    manifests. `push` fails when the registry disagrees.
  */
  index =
    pkgs.runCommand "scratch-index-${version}"
      {
        nativeBuildInputs = [ pkgs.jq ];
      }
      ''
        entry() {
          jq --join-output --arg arch "$2" \
            '.manifests[0] | "{\"mediaType\":\"\(.mediaType)\",\"size\":\(.size),\"digest\":\"\(.digest)\",\"platform\":{\"architecture\":\"\($arch)\",\"os\":\"linux\"},\"artifactType\":\"application/vnd.oci.image.config.v1+json\"}"' \
            "$1/index.json"
        }
        mkdir $out
        printf '{"schemaVersion":2,"mediaType":"application/vnd.oci.image.index.v1+json","manifests":[%s,%s]}' \
          "$(entry ${x86Image} amd64)" "$(entry ${armImage} arm64)" > $out/index.json
        printf 'sha256:%s' "$(sha256sum < $out/index.json | cut -d' ' -f1)" > $out/digest
      '';

  push =
    let
      registry = "ghcr.io/nixidae/nixkube/scratch";
      x86Tag = "${registry}:${builtins.baseNameOf x86Image}";
      armTag = "${registry}:${builtins.baseNameOf armImage}";
    in
    pkgs.writeShellApplication {
      name = "push";
      runtimeInputs = [ pkgs.crane ];
      text = ''
        set -euo pipefail

        crane auth login --username "$REPO_USERNAME" --password "$REPO_TOKEN" ghcr.io

        crane push ${x86Image} ${x86Tag}
        crane push ${armImage} ${armTag}

        crane index append \
          --manifest ${x86Tag} \
          --manifest ${armTag} \
          --tag ${registry}:${version}

        crane index append \
          --manifest ${x86Tag} \
          --manifest ${armTag} \
          --tag ${registry}:latest

        expected="$(cat ${index}/digest)"
        for tag in ${version} latest; do
          published="$(crane digest ${registry}:$tag)"
          if [ "$published" != "$expected" ]; then
            echo "${registry}:$tag is $published, and the index built here is $expected" >&2
            exit 1
          fi
        done
      '';
    };

in
{
  inherit
    index
    push
    x86Image
    armImage
    ;
}
