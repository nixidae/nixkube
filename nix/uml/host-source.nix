# SPDX-License-Identifier: Apache-2.0

/*
  The git source a NixOSRollout builds the hosts from (#359), served by the
  cache guest over dumb HTTP with basic auth.

  The repository's one file evaluates this run's own guests, plus
  ./host-built.nix. The file pins each source as `fetchTarball` with its
  NAR hash, as a real repository pins its inputs. Nix substitutes a pinned
  tarball from the cache guest before it downloads, and the URLs answer
  404, so a source the cache does not serve fails by name. The pod's
  environment is empty, so the file carries the knobs this run resolved:
  another backend or CRI is another system.
*/
{
  pkgs,
  lib,
  sources,
  # Where `nixkube/default.nix` keeps this run, such as [ "umlTest" ].
  attrPath,
  knobs,
}:
let
  facts = import (sources.vivarium + "/modules/recipes/cache-facts.nix");
  port = 8080;
  paths = lib.mapAttrs (_: source: "${source}") sources;
  pins = lib.mapAttrs (name: path: {
    url = "http://${facts.address}:${toString port}/sources/${name}.tar.gz";
    sha256 =
      (builtins.fetchTree {
        type = "path";
        inherit path;
      }).narHash;
  }) paths;

  expression = pkgs.writeText "default.nix" ''
    { name, system, ... }:
    let
      sources = builtins.mapAttrs (_: builtins.fetchTarball) (
        builtins.fromJSON ${builtins.toJSON (builtins.toJSON pins)}
      );
      run = builtins.foldl' (set: name: set.''${name}) (import sources.nixkube { inherit sources system; }) (
        builtins.fromJSON ${builtins.toJSON (builtins.toJSON attrPath)}
      );
      knobs = builtins.fromJSON ${builtins.toJSON (builtins.toJSON knobs)};
      test = run.extend {
        modules = [
          (
            { lib, ... }:
            {
              knobs = builtins.mapAttrs (_: value: { default = lib.mkForce value; }) knobs;
            }
          )
        ];
      };
    in
    test.systems.''${name}.extendModules { modules = [ "''${sources.nixkube}/nix/uml/host-built.nix" ]; }
  '';

  user = "nixkube";
  password = "uml-source";
in
{
  inherit user password port;
  url = "http://${facts.address}:${toString port}/nixkube.git";

  # Every source, for the cache guest to serve. Git compresses its objects,
  # so the repository's own closure names none of them.
  roots = lib.attrValues paths;

  # A fixed author and date, so the same file is the same commit.
  repository = pkgs.runCommand "nixkube-host-source" { nativeBuildInputs = [ pkgs.gitMinimal ]; } ''
    export HOME=$TMPDIR GIT_AUTHOR_NAME=nixkube GIT_AUTHOR_EMAIL=nixkube@uml.test
    export GIT_COMMITTER_NAME=nixkube GIT_COMMITTER_EMAIL=nixkube@uml.test
    export GIT_AUTHOR_DATE=2026-01-01T00:00:00Z GIT_COMMITTER_DATE=2026-01-01T00:00:00Z
    git init --quiet --initial-branch=main work
    cp ${expression} work/default.nix
    git -C work add default.nix
    git -C work commit --quiet --message "the uml hosts"
    mkdir $out
    git -C work rev-parse HEAD > $out/rev
    git clone --quiet --bare work $out/nixkube.git
    git -C $out/nixkube.git update-server-info
  '';

  # nginx reads `{PLAIN}` entries.
  htpasswd = pkgs.writeText "htpasswd" "${user}:{PLAIN}${password}\n";
}
