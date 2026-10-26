# SPDX-License-Identifier: Apache-2.0

# The nixpkgs revision the `flake-hello` and `expr-hello` jobs evaluate on
# the node, and what that costs the node: the one place both the jobs and
# the guest test's cache guest read it, so the cache holds exactly what the
# node asks for.
#
# A revision and not a branch. The node evaluates the job's string, so a
# branch asks for whatever it is that morning, fails for reasons that have
# nothing to do with nixkube, and cannot fail the same way twice.
rec {
  owner = "nixos";
  repo = "nixpkgs";
  rev = "07e1d92cdc0ed416cfa11ff3ca40d17e61cfba7a";

  flakeRef = "github:${owner}/${repo}/${rev}";

  # The nixpkgs of that revision, as the node imports it.
  import' =
    args:
    import (builtins.fetchTree {
      type = "github";
      inherit owner repo rev;
    }) args;
}
