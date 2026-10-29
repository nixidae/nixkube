# Getting started

nixkube is an easykubenix module, `easykubenixModules.default`. It puts a
node DaemonSet on each node, which gives pods a Nix store through a CSI
volume or an NRI annotation, and a cache that the nodes substitute from.

## Write a deployment

Put this in `deployment.nix`:

```nix
{ pkgs ? import <nixpkgs> { } }:
let
  fetch = repo: builtins.fetchTarball "https://github.com/nixidae/${repo}/archive/main.tar.gz";
  easykubenix = (import (fetch "easykubenix") { inherit pkgs; }).lib.easykubenix;
  nixkube = import (fetch "nixkube") { inherit pkgs; };
in
easykubenix {
  modules = [
    nixkube.easykubenixModules.default
    {
      nixkube.enable = true;
      nixkube.hostSystem = "x86_64-linux";
      nixkube.node.compat = false;
    }
  ];
}
```

`hostSystem` is the architecture that renders the manifest. `nixkube.systems`
names the architectures of the nodes. `node.compat` registers the old driver
name `nix.csi.store`, which a new deployment does not need.

## Render it

```console
$ nix shell --file https://github.com/nixidae/easykubenix/archive/main.tar.gz packages.ekn
$ ekn --file deployment.nix render > nixkube.yaml
```

The [easykubenix getting-started page](https://nixidae.github.io/easykubenix/getting-started.html)
shows how `ekn kubeapply` applies it to a cluster.

## Give a pod a store

A pod asks for a store path in a CSI volume. The node mounts the closure of
that path at `/nix`:

```yaml
volumes:
  - name: nix
    csi:
      driver: nixkube
      volumeAttributes:
        x86_64-linux: /nix/store/...-hello-2.12.2
```

The [README](https://github.com/nixidae/nixkube#deploying-workloads) shows
the NRI annotation and the other volume attributes. [Module options](options.md)
lists every `nixkube` option.
