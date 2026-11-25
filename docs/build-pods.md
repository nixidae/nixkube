# Build pods

With `nixkube.buildPods.enable`, pynixd's central can run a build in a pod
of its own. The pod starts for one build and ends after it. This page says
what such a pod costs, and which builds to keep out of them.

A build pod is a spike (nixidae/nixidae#1560). pynixd's design document,
`docs/design/build-pods.md`, has the full design and every measurement.

## The fixed cost

A build pod costs time that is not the build: Kubernetes starts the pod,
pynixd starts in it, central copies the inputs in, and the kubelet reports
the end. For a small build this cost is larger than the build.

Measured on a QEMU node, a busybox input (37 MiB), 5.8 s in all:

| part | seconds | who pays it |
| --- | --- | --- |
| create to container running | 1.6 | Kubernetes: scheduling, volumes, container start |
| container start to first log line | 1.1 | Python's start in the pod |
| server start | 1.0 | pynixd in the pod |
| inputs | 0.26 | central to the pod, over SSH |
| build | 0.68 | the build |
| last log line to result | 1.0 | the kubelet, which checks for the container's end once a second |

The inputs grow with the closure: 1.5 s for 210 MiB, 3.3 s for 378 MiB.
About 5.4 s stays the same for every build.

A runner pod (`nixkube.buildPods.runner`) costs more: it starts two
containers, and the builder waits for the controller. One measured busybox
build took 7.4 s, and 3.2 s of that was the time to container running. The
farm volume took 0.6 s of it. The runner then starts about 3 s after the
controller: the controller's `postStart` hook waits until pynixd in the
controller has started and prepared the build, so this part is pynixd's own
start.

## Send small builds elsewhere

A build that takes a few seconds on central or on a long-lived builder
takes several times as long in a build pod. Keep build pods for builds that
are long, or that need the pod's own CPU and memory.

Central does part of this itself. It ranks each store by the time it
expects a build to take there. For a build pod store, that time includes the
fixed cost, which the store learns from each pod that ends. Until the first
pod ends, central counts 1 s (pynixd's `ranking.remote_setup_seconds`). So a short build
goes to central's own build slots (`max-jobs`, 5 by default) or to a
long-lived builder when one has room. When those are busy, the build can
still go to a pod.

To keep a build out of pods completely:

- Set `nixkube.buildPods.mandatoryFeatures`, for example to
  `[ "big-parallel" ]`. Then only a build that asks for that feature goes to
  a pod.
- A derivation with `preferLocalBuild = true` waits for a slot on central.
  It goes elsewhere only when that wait is more than ten times the time
  elsewhere (pynixd's `ranking.prefer_local_weight`).
- `nixkube.buildPods.maxBuilds` limits how many pods of one system run at
  once.
