# Compatibility

This page lists what nixkube needs from a node and a cluster, and what it
does when a node does not have it.

nixkube detects most of this at run time. It does not ask the operator to
declare what the cluster supports. A probe tests the feature on each node.
The result is a node label, a Kubernetes event or a metric, and nixkube takes
a fallback where one exists. Options exist only where nixkube cannot detect
the answer, or where the operator chooses between two working routes.

"Required" means the part named does not work without it. "Fallback" names
what nixkube does instead and what selects it.

## Kernel: the mount API

| needs | Linux | used by | without it |
| --- | --- | --- | --- |
| `open_tree`, `move_mount` | 5.2 | NRI: every mount into a container | Required. The NRI plugin does not start, event `KernelIncompatible`. CSI does not need them. |
| `fsopen`, `fsconfig`, `fsmount` for an overlay | 6.5 (the code's claim) | NRI: a writable `/nix` (`nixkube/pod-rw`) | Fallback: NRI serves read-only only, event `KernelLimited`. A writable request fails. |
| `mount_setattr` with `MOUNT_ATTR_RDONLY` | 5.12 | NRI: the bind farm (the default NRI route) | Fallback: the NRI plugin probes it at start and serves hardlink trees, event `KernelLimited`. A host-store node has only the farm, so there the plugin does not start, event `KernelIncompatible`. nixkube never remounts a farm: a remount would leave the binds writable into the node's store. |
| `mount_setattr` with `MOUNT_ATTR_RDONLY` | 5.12 | CSI: a read-only bind | Fallback: `MS_BIND\|MS_REMOUNT\|MS_RDONLY`. |
| `MOUNT_ATTR_IDMAP` | 5.12, per filesystem | NRI: a writable `/nix` in a pod with `hostUsers: false` | Optional. The kernel refuses to idmap an overlay, so nixkube idmaps the lower layer and chowns the upper directory. |
| recursive bind (`MS_REC`) | any | CSI, NRI | Required. Without it a pod sees none of the store paths (measured: 0 of 3 readable). |
| `setns` into a container's mount namespace | any | NRI | Required. A sandboxed runtime (gVisor) does not have one; see the runtimes. |

nixkube calls these syscalls by number (428 to 432, 442). x86_64 and
aarch64 share these numbers.

## Kernel: filesystems

| needs | Linux | used by | without it |
| --- | --- | --- | --- |
| EROFS, overlayfs data-only lower layers (`lowerdir=A::B`), `metacopy`, `redirect_dir` | 6.5 | composefs presentation, CSI and NRI | Fallback, by a probe at start. CSI uses a hardlink tree, or refuses on a host-store node. NRI uses the bind farm. Metric `nixkube_composefs_available`. Off by hand: `nixkube.csi.composefs`, `nixkube.nri.composefs`. |
| EROFS mounted from a file | 6.12 | composefs | Fallback: a loop device (`mount -o ro,loop`). |
| hard links on the store's filesystem | any | CSI hardlink tree | A host-store (NixOS) node cannot link out of the host's store (`EXDEV`). CSI refuses there; NRI uses composefs or the farm. |
| overlayfs | any | CSI: a writable volume | Required for a writable CSI volume. |
| tmpfs | any | CSI farm | Required for the farm. |

Kernels known to have no EROFS: Talos 1.11 to 1.13, GKE COS 125 and 129.
nixkube uses the fallbacks there.

## Kernel: limits

| limit | used by | without room |
| --- | --- | --- |
| `fs.mount-max` (default 100,000 per mount namespace) | the bind farm: one mount per store path | Fallback: above 90 % of the budget, a farm becomes a hardlink tree. A host-store node refuses. If the sysctl cannot be read, nixkube counts 100,000. A closure of about 2,400 paths gives about 40 farm containers per namespace. |
| disk | the hardlink tree: one directory entry per file | No check. 375,582 files took about 8 s. The composefs image is stored once per closure. |
| a build pod's ephemeral storage | build outputs | The kubelet evicts the pod. The inputs do not count; the outputs and Nix's 8 MiB `reserved` file do. |

## Build pods: the sandbox

These apply only with `nixkube.buildPods.enable`.

| needs | without it |
| --- | --- |
| Unprivileged user namespaces in a pod | Fallback, by node label. The DaemonSet's `userns-probe` makes the sandbox's namespaces with the sandbox container's security context. It labels the node `nixkube/userns=true` or `false`, with the error in the annotation `nixkube/userns-error`. A node labelled `false` takes the runner template (`nixkube.buildPods.runner`), where NRI makes the container Nix's sandbox. The probe tests behaviour; it reads no sysctl. |
| The seccomp profile `nixkube/pynixd-builder.json` under the kubelet's seccomp directory (`/var/lib/kubelet/seccomp`) | Required for the sandbox container. RuntimeDefault refuses `unshare` (measured). The node DaemonSet writes the profile. |
| No AppArmor profile named | A pod that names an AppArmor profile is refused on a node without AppArmor, so nixkube names none. Ubuntu's restriction on unprivileged user namespaces is not measured; the probe decides. |
| `procMount: Unmasked` with `hostUsers: false` | Optional. Without it the sandbox binds the container's masked `/proc`. A probe decides. |
| Landlock ABI 6 | Optional. Below ABI 6 the sandbox does not scope abstract sockets. |
| cgroup v2 with a delegated memory controller | Optional for per-build cgroups (`build_cgroups_off`). pynixd's pressure monitor requires cgroup v2. |
| A CNI that enforces NetworkPolicy | Optional in effect. The policy limits a build pod to central's SSH port and the cluster DNS. Without enforcement the pod's network is open. |

The runner template needs the NRI items below, and an NRI with containerd/nri
193a7fdfa4 (after v0.12.5) to remove variables. Without that fix the build
sees the eight `KUBERNETES_*` variables and `HOSTNAME`. The plugin labels each
node `nixkube/build-runner`; set it to `"false"` by hand to keep runner pods
off a runtime without the fix.

## Container runtimes

What each mount path gives a pod. `ro` and `rw` are the NRI modes.

| | runc | crun | youki | gVisor (runsc) | Kata Containers |
|---|---|---|---|---|---|
| **containerd** | CSI, NRI ro/rw | CSI, NRI ro/rw | CSI, NRI ro/rw | CSI; NRI refused¹ | CSI, NRI ro², ³ |
| **CRI-O** | CSI, NRI ro/rw | CSI, NRI ro/rw | CSI, NRI ro/rw | no container runs⁴ | CSI, NRI ro², ³ |

1. gVisor keeps the container's rootfs out of the node's reach. The
   container fails with a message that says so and names CSI.
2. A VM gets `/nix` as a read-only virtio-fs share, and waits in the VM
   until the mount is done. A writable `/nix` and `nixkube/pod-path` are
   refused: the tree is hard links into the node's store. nixkube detects a
   VM by its RuntimeClass handler: `nixkube.nri.vmRuntimeHandlers`, default
   `[ "kata" ]`. The wait is a static busybox that NRI puts in front of the
   container's args, because Kata checks that the command exists before any
   OCI hook runs; `crictl inspect` shows the changed args. A container that
   also mounts something under `/nix` fails with EROFS.
3. Needs Kata 4.0.0 or later. Earlier guest kernels have a use-after-free in
   virtio-fs (kata-containers#12589), without nixkube too. nixpkgs packages
   3.32.0; vivarium carries 4.2.0.
4. Measured with CRI-O 1.36.5 and runsc 20260406, on a plain busybox pod.

- CRI-O with Kata needs `skip_mount_home = "true"` in containers-storage's
  overlay options. CRI-O refuses a Kata pod on the host network.
- CRI-O creates a container without the plugin's changes when an NRI plugin
  returns an error. containerd does the same when the plugin does not answer
  in its 2 s request timeout.
- containerd 2.3.4 to 2.4.1 starts no user-namespaced container from an image
  with zero layers ("mkdirat rootfs/proc"), with or without nixkube.
  Lillecarl/containerd#2 fixes it. CRI-O is not affected.
- nixkube finds the runtime's socket from the kubelet's `configz`
  (`containerRuntimeEndpoint`), through the API server. It needs RBAC on
  `nodes/proxy`.

## NRI

| needs | without it |
| --- | --- |
| NRI on in the runtime, socket `/var/run/nri/nri.sock` | The plugin waits, and **pods start without `/nix` and with no error**. Check that NRI is on before relying on it. |
| NRI API `v1alpha1` over ttrpc | Required. The bindings are generated from NRI 0.11.0; interop is measured with containerd's NRI v0.12.3. |
| The OCI `createRuntime` hook (`nri-wait`) | Required outside a VM. It gives up after 30 s. |
| containerd's NRI validator off, or without `reject_namespace_adjustment` and `reject_custom_seccomp_adjustment` | The runner template's adjustments are refused. |
| `required_plugins` in containerd's NRI validator | Optional. With `nixkube` listed, containerd refuses a pod the plugin did not see, in place of starting it without `/nix`. |

## Kubernetes and the kubelet

| needs | without it |
| --- | --- |
| CSI ephemeral inline volumes | Required. nixkube serves no persistent volume. |
| The CSIDriver with `fsGroupPolicy: None` | nixkube renders it. With `File`, the kubelet chowns the whole closure: about 3.4 s per pod (measured). |
| The kubelet's root at `/var/lib/kubelet` | Required. The DaemonSet mounts it as a hostPath. |
| A privileged node DaemonSet with `mountPropagation: Bidirectional` | Required. Pod Security admission must allow it in nixkube's namespace. |
| `subPath: nix` on a CSI volume that is not a farm | Required. A farm volume takes no `subPath`: the kubelet's bind drops its submounts. |
| A kubelet in its own mount namespace (Talos) | Supported: the store is a plain hostPath. Measured on Talos 1.13.9. |
| `hostUsers: false` | CSI uses a hardlink tree, or refuses on a host-store node. NRI works, composefs and rw included. `storeVolume` refuses it. |
| Build pods: PriorityClasses -2 and -1, `activeDeadlineSeconds`, owner references, `DisruptionTarget` conditions | Required. Native sidecars are not needed. |
| The nixkube.nixidae.org/v1alpha1 CRD, Leases and `events.k8s.io/v1` | Required for the NixOS host rollout. |
| `net.ipv6.bindv6only=0` | Fallback: the metrics listener binds IPv4. |
| The compat driver `nix.csi.store` | Optional and deprecated (`nixkube.node.compat`). |

Central reads a build pod's result from its log. The kubelet checks a
followed log for the container's end once a second, so the pod's status is
only the fallback.

## Host OS and Nix

| needs | without it |
| --- | --- |
| A NixOS host for `nixkube.hostStore.enable` | Optional. `nixkube-host-check` labels a node with `/etc/NIXOS` `nixkube/host=nixos`, and the host-store DaemonSet takes it. Other nodes keep a store of their own. |
| On a host-store node, nixkube's Nix not newer than the host daemon's minor version | nixkube refuses to start its daemon. An unknown host version gives a warning. |
| systemd-boot and `systemd-run` on the host | Required for the NixOS host switch. |

## Architectures

`nixkube.systems` defaults to `x86_64-linux` and `aarch64-linux`. nixkube
reads a node's architecture from `kubernetes.io/arch`. The seccomp profiles
cover x86_64 and aarch64.

The cluster tests run on x86_64 only. CI builds the aarch64 images, and no
test runs them.

## Tested versions

The runtime table was measured with `nix/uml` on Kubernetes 1.37, containerd
2.3.4, CRI-O 1.36.5, runc 1.4.3, crun 1.29.1, youki 0.7.0, gVisor 20260406
and Kata 4.2.0 on nested QEMU.

The pins today are Kubernetes 1.37.1, containerd 2.4.1, CRI-O 1.37.2, runc
1.5.2, crun 1.30.1, youki 0.7.0 and gVisor 20260406, with Linux 7.2.9 in the
QEMU node. The build pod and NRI sandbox measurements use these. The runtime
table has not been measured again on them. Each combination is an attribute,
run by hand and not in CI:

    nix run --file . internal.umlMatrix.uml-crio.driver -- --out ./o

## Not measured

- Ubuntu's kernel with its restriction on unprivileged user namespaces.
- The idmap of farm binds in a pod with `hostUsers: false`.
- Recursive Nix in a build pod on nixkube's farm.
- `hostMountPath = "/"`.
- A kubelet root other than `/var/lib/kubelet`.
- Any node on aarch64.
