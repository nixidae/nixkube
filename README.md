# nixkube / nix + kube / nix-csi / nix-nri

[![Documentation](https://img.shields.io/badge/docs-nixidae.github.io-blue)](https://nixidae.github.io/nixkube/)

Mount /nix into Kubernetes pods using the CSI ephemeral volumes or NRI(Node Resource Interface). Volumes
share lifetime with Pods and are embedded into the Podspec.

[![Ask DeepWiki](https://deepwiki.com/badge.svg)](https://deepwiki.com/nixidae/nixkube)

## Deploying nixkube

Stick your pubkeys in ./keys and they will be imported into the module system
then run the following command and you'll have nixkube deployed.
```bash
nix run --file . internal.kubenixApply.deploymentScript -- --yes --prune
```

If you'd rather mangle YAML yourself you can use
```bash
nix build --file . internal.kubenixApply.manifestYAMLFile
```
and stuff the result into Kustomize, a blender or your Kubernetes cluster

### Which commit to deploy

Deploy the `tested` branch, or a release. CI moves `tested` to a commit of
`main` once every test of it passed, and by then its images are on ghcr.io.
`main` itself can name an image that CI has not pushed yet.

The manifest names each image by content: the init image by a tag from its
store hash, and `scratch` by digest. A release pins the init image by
digest too. To run an image this checkout did not build, set
`nixkube.images.nix` (for example to `ghcr.io/nixidae/nixkube/nix:main`)
and `nixkube.imagePullPolicy = "Always"`. To publish your own build of the
init image, run `nix run --file . internal.nixImage.pushArch.x86_64-linux`; it skips
a tag that already holds the same image.

## Deploying workloads

nixkube supports two methods for injecting Nix stores into pods:

### CSI Ephemeral Volumes (Explicit)

Request Nix stores explicitly via CSI volumeAttributes. Specify one or more:
- `storePath` - Direct nix store path (highest priority)
- `flakeRef` - Flake reference to build
- `nixExpr` - Nix expression to evaluate and build

```yaml
apiVersion: v1
kind: Pod
metadata:
  name: hello-csi
spec:
  containers:
  - name: hello
    image: nixos/nix:latest
    volumeMounts:
    - name: nix
      mountPath: /nix
  volumes:
  - name: nix
    ephemeral:
      volumeClaimTemplate:
        spec:
          accessModes: ["ReadOnlyMany"]
          storageClassName: ephemeral-storage
          resources:
            requests:
              storage: 1Gi
          csi:
            driver: nixkube
            volumeAttributes:
              x86_64-linux: /nix/store/hello-......
              aarch64-linux: /nix/store/hello-......
              flakeRef: github:nixos/nixpkgs/nixos-unstable#hello
              nixExpr: |
                let
                  nixpkgs = builtins.fetchTree {
                    type = "github";
                    owner = "nixos";
                    repo = "nixpkgs";
                    ref = "nixos-unstable";
                  };
                  pkgs = import nixpkgs { };
                in
                pkgs.hello
```

The first successful option by priority wins.

**Tip**: For command arrays and environment variables, use `lib.getExe` to reference executables without managing full store paths:

```nix
command = [ (lib.getExe pkgs.hello) ]
env = {
  name = "HELLO_CONFIG";
  value = pkgs.hello-config;
}
```

### NRI Plugin (Automatic via Annotations)

Use pod annotations to automatically inject Nix stores without explicit volume requests:

```yaml
apiVersion: v1
kind: Pod
metadata:
  name: hello-nri
  annotations:
    nix-nri/pod: |
      /nix/store/hello-......
    nix-nri/pod@aarch64-linux: |
      /nix/store/hello-aarch64-......
spec:
  containers:
  - name: hello
    image: nixos/nix:latest
    # /nix is automatically mounted by NRI plugin
```

### Runtime compatibility

What each mount path gives a pod, by container runtime (CRI) and OCI runtime
(RuntimeClass). `ro` and `rw` are the NRI modes: `nixkube/pod-rw` asks for a
writable /nix.

| | runc | crun | youki | gVisor (runsc) | Kata Containers |
|---|---|---|---|---|---|
| **containerd** | CSI, NRI ro/rw | CSI, NRI ro/rw | CSI, NRI ro/rw | CSI; NRI refused¹ | CSI, NRI ro², ³ |
| **CRI-O** | CSI, NRI ro/rw | CSI, NRI ro/rw | CSI, NRI ro/rw | no container runs⁴ | CSI, NRI ro², ³ |

1. gVisor keeps the container's rootfs out of the node's reach. The
   container fails with a message that says so and names CSI instead.
2. A VM gets /nix as a read-only virtio-fs share, and waits in the VM until
   the build is done. A writable /nix and `nixkube/pod-path` mounts are
   refused with the reason: the tree is hardlinks into the node's store.
   Set `nixkube.nri.vmRuntimeHandlers` if your Kata handler is not `kata`.
   The wait is a static busybox that NRI puts in front of the container's
   args, because Kata checks the command exists before any OCI hook runs;
   `crictl inspect` shows the changed args. A container that also mounts
   something under /nix fails with EROFS, because its mount point would be
   inside the read-only share.
3. Needs Kata 4.0.0 or later. Earlier guest kernels have a use-after-free
   in virtio-fs when a container exits (kata-containers#12589): the VM's
   agent dies and every container in the pod exits with 255. This happens
   without nixkube too. nixpkgs still packages 3.32.0; vivarium
   carries 4.2.0 until nixpkgs moves.
4. Measured with CRI-O 1.36.5 and runsc 20260406, on a plain busybox pod
   without nixkube.

CRI-O with Kata needs `skip_mount_home = "true"` in containers-storage's
overlay options, or the VM gets an empty rootfs. CRI-O refuses a Kata pod on
the host network ("Host networking requested, not supported by runtime");
containerd accepts one. CRI-O creates a container when an NRI plugin returns
an error, without the plugin's changes.

A pod with `hostUsers: false` gets both mounts under CRI-O. Under
containerd (2.3.4 to 2.4.1 and main) no user-namespaced container starts
from an image with zero layers, such as a `FROM scratch` image that
nixkube fills: "mkdirat rootfs/proc: permission denied", with or without
nixkube. Lillecarl/containerd#1 has the cause and #2 a fix. An image with
one layer, even one without /proc, is not affected.

Tested by `nix/uml` on Kubernetes 1.37, containerd 2.3.4, CRI-O 1.36.5,
runc 1.4.3, crun 1.29.1, youki 0.7.0, gVisor 20260406 and Kata 4.2.0 on
nested QEMU. `umlMatrix.<backend>-<cri>[-kata]` is each combination as an
attribute, by hand and not in CI:

    nix run --file . internal.umlMatrix.uml-crio.driver -- --out ./o

Examples:
* [multi-system example](https://github.com/Lillecarl/hetzkube/blob/4ed76ec77bfb104d1c2307b1ba178efa61dd34e2/kubenix/modules/cheapam.nix#L113)
* [single-system ci example(s)](https://github.com/Lillecarl/nix-csi/blob/3179e5f8383e760bbef313300a224e44f18722c7/kubenix/ci/default.nix)

[Documentation](https://nixidae.github.io/nixkube/)

---

*This project is made possible by*

[![Dynamist](.assets/dynamist-logo.png)](https://dynamist.se/)

