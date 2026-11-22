"""What every phase of the node test shares: waits, probes, checks.

The phases are `nix/uml/phases/*.py` and the chaos scenarios are
`nix/uml/chaos/`; this module is on their `pythonPath`. The manifest
applies, the node DaemonSet reaches Ready, and a workload gets the store
path it asked for through a CSI ephemeral volume.

What is being proved is narrow and load-bearing. The DaemonSet's init
container fills the node's Nix store by substituting into it, and a build
sandbox has no network. It works here because the guest's own /nix/store is
the sandbox's, over hostfs, so `local?trusted=true` already holds every path
the manifest names -- and `vivarium.nixDatabase` is what makes Nix agree they
are real rather than go looking for them.

The API server is reached from the host through kr8s, at
`<out>/access/kubeconfig`, which the cluster phase's `bring_up` writes. What
is done to the node itself (crictl, systemctl, its files) goes through the
guest's agent.
"""

from __future__ import annotations

import json
import re
import shlex
from collections.abc import Awaitable, Callable
from typing import Any, cast

import anyio
import httpx
import kr8s
import kr8s.asyncio
import yaml
from kr8s.asyncio.objects import (
    APIObject,
    DaemonSet,
    Deployment,
    Event,
    Job,
    Pod,
    Service,
    StatefulSet,
    new_class,
    object_from_spec,
)
from vivarium_k8s import until
from vivarium_runner import Machine, MachineError, Machines

# The test's settings: JSON from ./default.nix, read by key.
type Settings = dict[str, Any]
# An object as the API server returns it: its shape is the kind's.
type Manifest = dict[str, Any]

NAMESPACE = "nixkube"
STATEFULSET = "pynixd"

# What `appstarter/seed.py` adds to "store holds X" when the fetch failed and
# it seeded from the image instead. The successful line is a prefix of the
# fallback line, so only this substring tells the two apart.
FALLBACK_MARK = ", and the deployment asks for "

# The jobs ./workloads.nix adds, by name.
WORKLOADS = ("csi-path", "csi-shared")

# The probes ./probe.nix renders, as (what it wants of /nix, settings key).
# Created after the driver is Ready, never applied with the manifest -- an
# NRI pod waits for nothing, so an applied one starts before the plugin
# exists. See ./probe.nix.
PROBES = (("ro", "probeRo"), ("rw", "probeRw"))

# These are stuck-detectors, not patience.
#
# Nothing here waits on anything far away. The images are already on the
# node, and the closure the init container copies comes from the guest's own
# store on the same disk. Five minutes is generous. Anything past that is not
# slow, it is stuck -- waiting longer turns a bug into a delay and hides it.
#
# So when one of these fires, it means something is wrong and the report has
# to say what. `until` dumps the pod list, the events, kubelet, containerd
# and the pod logs.
APPLY_TIMEOUT = 5 * 60
READY_TIMEOUT = 5 * 60
# One request. A chaos scenario can take the API server down, and a request
# with no bound then hangs the check that `until` polls, so `until` never
# gets to report what it saw.
CALL_TIMEOUT = 30

# What a request to an API server that is not there raises.
UNREACHABLE_API = (kr8s.ServerError, kr8s.APITimeoutError, httpx.HTTPError, OSError)
# What one section of the report can fail with: the above, an object that is
# gone, a Job with no pod, a section past its time.
UNREPORTABLE = (*UNREACHABLE_API, kr8s.NotFoundError, MachineError, TimeoutError)

# kr8s guesses a plural by adding "s" to a kind it does not know.
CSIDriver = new_class("CSIDriver", version="storage.k8s.io/v1", namespaced=False, plural="csidrivers")
PriorityClass = new_class("PriorityClass", version="scheduling.k8s.io/v1", namespaced=False, plural="priorityclasses")
NixOSRollout = new_class(
    "NixOSRollout", version="nixkube.nixidae.org/v1alpha1", namespaced=False, plural="nixosrollouts"
)

# Applied first, in this order: everything else may live in one of them or
# be one of them.
APPLY_FIRST = ("Namespace", "CustomResourceDefinition")


async def cluster(vms: Machines) -> kr8s.asyncio.Api:
    """The API server, from the host."""
    api = await kr8s.asyncio.api(kubeconfig=str(vms.access / "kubeconfig"), namespace=NAMESPACE)
    api.timeout = CALL_TIMEOUT
    return api


def raw(obj: APIObject) -> Manifest:
    # kr8s types `raw` as a Box, whose methods carry no types.
    return cast(Manifest, obj.raw)


async def listed[T: APIObject](kind: type[T], api: kr8s.asyncio.Api, **kwargs: Any) -> list[T]:  # noqa: ANN401 -- kr8s' own `get` keywords
    return [cast(T, obj) async for obj in kind.list(api=api, **kwargs)]


async def as_object(api: kr8s.asyncio.Api, spec: Manifest) -> APIObject:
    """The object *spec* describes, with the plural and scope the server gives."""
    try:
        return object_from_spec(spec, api=api)
    except KeyError:
        pass
    kind, version = spec["kind"], spec["apiVersion"]
    # The shape `lookup_kind` parses: a group after a dot, a bare version after a slash.
    _, plural, namespaced = await api.lookup_kind(f"{kind}.{version}" if "/" in version else f"{kind}/{version}")
    cls = new_class(kind, version=version, namespaced=namespaced, plural=plural)
    return cls(spec, api=api)


async def apply(api: kr8s.asyncio.Api, spec: Manifest) -> APIObject:
    """Server-side apply, as one field manager.

    Server-side so that a field the next apply leaves out goes: a rollout's
    `spec.systems` when it moves to `spec.source`. A merge patch keeps it.
    """
    obj = await as_object(api, spec)
    await obj.apply(server_side=True, force_conflicts=True)
    return obj


async def delete(obj: APIObject, cp: Machine, timeout: int = APPLY_TIMEOUT) -> None:
    """Delete *obj* and wait until the API server no longer has it, as `kubectl delete` does.

    `Background` propagation: the API's default for a Job orphans its pods,
    and an orphaned pod keeps its volumes mounted.
    """
    await obj.delete(propagation_policy="Background")

    async def gone() -> tuple[bool, str]:
        try:
            await obj.refresh()
        except kr8s.NotFoundError:
            return True, "gone"
        return False, f"{obj.kind}/{obj.name} is still there"

    await until(f"{obj.kind}/{obj.name} to be deleted", gone, timeout, cp)


async def created_job(api: kr8s.asyncio.Api, source: str, edit: Callable[[Manifest], None] | None = None) -> Job:
    """A Job from the JSON at *source*, after *edit* changes it in place."""
    spec = cast(Manifest, json.loads(await anyio.Path(source).read_text()))
    if edit is not None:
        edit(spec)
    spec.setdefault("metadata", {}).setdefault("namespace", NAMESPACE)
    job = Job(spec, api=api)
    await job.create()
    return job


async def job_pods(api: kr8s.asyncio.Api, name: str) -> list[Pod]:
    return await listed(Pod, api, namespace=NAMESPACE, label_selector={"job-name": name})


async def logs(pod: Pod, container: str | None = None, **kwargs: Any) -> str:  # noqa: ANN401 -- `Pod.logs` keywords
    return "\n".join([line async for line in pod.logs(container=container, **kwargs)])


async def job_logs(api: kr8s.asyncio.Api, name: str, container: str | None = None, **kwargs: Any) -> str:  # noqa: ANN401 -- `Pod.logs` keywords
    """The logs of a Job's first pod, as `kubectl logs job/<name>` gives them."""
    pods = await job_pods(api, name)
    if not pods:
        raise MachineError(f"[cp] job {name} has no pod to read logs from")
    return await logs(pods[0], container, **kwargs)


def pod_state(pod: Manifest) -> str:
    """The STATUS column of `kubectl get pods`: a pod being deleted, a reason, or the phase."""
    if pod["metadata"].get("deletionTimestamp"):
        return "Terminating"
    status = pod.get("status", {})
    for s in status.get("initContainerStatuses", []):
        state = s.get("state", {})
        if reason := state.get("waiting", {}).get("reason"):
            return f"Init:{reason}"
        if state.get("terminated", {}).get("exitCode", 0) != 0:
            return f"Init:{state['terminated'].get('reason', 'Error')}"
    for s in status.get("containerStatuses", []):
        state = s.get("state", {})
        if reason := state.get("waiting", {}).get("reason") or state.get("terminated", {}).get("reason"):
            return str(reason)
    return str(status.get("phase", "Unknown"))


def pod_ready(pod: Manifest) -> str:
    """The READY column: containers ready, of all of them."""
    statuses = pod.get("status", {}).get("containerStatuses", [])
    return f"{sum(1 for s in statuses if s.get('ready'))}/{len(pod['spec']['containers'])}"


def pod_summary(pods: list[Pod]) -> str:
    """The pods as `kubectl get pods` lists them, with the clocks taken out.

    `until` reports how long its evidence has been *unchanged*, and that
    figure is the one that separates slow from stuck. AGE and RESTARTS both
    carry a time, so evidence built from them changes every poll and the
    answer is always "0s unchanged" -- a stuck-detector that cannot detect
    anything.

    Name, ready count and status. Those change when something happens, and
    only then.
    """
    rows = [raw(p) for p in pods]
    return " | ".join(f"{p['metadata']['name']}={pod_ready(p)} {pod_state(p)}" for p in rows) or "no pods"


def nix_mount(mountinfo: str) -> tuple[str, str] | None:
    """The line for the container's own /nix, from /proc/self/mountinfo.

    Fields up to the `-` separator are fixed: id, parent, device, root,
    mount point, options. Everything after it is the filesystem, whose
    fields vary. Returns (options, fstype) or None.

    A mount point of exactly `/nix` is what says the NRI plugin ran. The
    base runtime spec binds `/nix/store` into every container here, and
    never `/nix`.
    """
    for line in mountinfo.splitlines():
        fields = line.split()
        if len(fields) < 7 or fields[4] != "/nix":
            continue
        sep = fields.index("-")
        return fields[5], fields[sep + 1]
    return None


def nix_is_composefs(mountinfo: str) -> bool:
    """Whether the container's own /nix is a composefs overlay: a
    data-only lower layer (`::`) in its superblock options, which neither
    the bind farm nor a read-write hardlink overlay has."""
    for line in mountinfo.splitlines():
        fields = line.split()
        if len(fields) < 7 or fields[4] != "/nix":
            continue
        after = fields[fields.index("-") + 1 :]
        return after[0] == "overlay" and len(after) > 2 and "::" in after[2]
    return False


def own_user_namespace(mountinfo: str) -> bool:
    """Whether a container's `/proc/self/uid_map` maps anything but the
    host's whole range to itself. The lines are `inside outside count`;
    mountinfo lines around them have more fields and are skipped."""
    maps = [line.split() for line in mountinfo.splitlines() if len(line.split()) == 3]
    return any(m != ["0", "0", "4294967295"] for m in maps if all(f.isdigit() for f in m))


def rolled_out(obj: Manifest) -> bool:
    """`kubectl rollout status` for a Deployment or a DaemonSet: every pod is the new one, and available."""
    status = obj.get("status", {})
    if status.get("observedGeneration", 0) < obj["metadata"]["generation"]:
        return False
    if obj["kind"] == "DaemonSet":
        want = status.get("desiredNumberScheduled", 0)
        return bool(status.get("updatedNumberScheduled", 0) == want and status.get("numberAvailable", 0) == want)
    want = obj["spec"].get("replicas", 1)
    updated = status.get("updatedReplicas", 0)
    return bool(
        updated == want and status.get("replicas", 0) == updated and status.get("availableReplicas", 0) == updated
    )


async def wait_rolled_out(obj: DaemonSet | Deployment, cp: Machine, timeout: int = READY_TIMEOUT) -> None:
    async def done() -> tuple[bool, str]:
        await obj.refresh()
        return rolled_out(raw(obj)), json.dumps(raw(obj).get("status", {}))

    await until(f"{obj.kind}/{obj.name} to roll out", done, timeout, cp)


async def deploy(api: kr8s.asyncio.Api, settings: Settings) -> None:
    """Apply the manifest, server-side.

    One object at a time, in the file's order after `APPLY_FIRST`, as
    kubectl does. kr8s' own `apply` of a list sends them all at once, and a
    namespaced object then races its Namespace.
    """
    print("[nixkube] applying the manifest", flush=True)
    # pyyaml ships no types.
    load = cast(Callable[[str], Manifest], yaml.safe_load)
    items: list[Manifest] = load(await anyio.Path(settings["manifest"]).read_text())["items"]
    items.sort(key=lambda o: APPLY_FIRST.index(o["kind"]) if o["kind"] in APPLY_FIRST else len(APPLY_FIRST))
    with anyio.fail_after(APPLY_TIMEOUT):
        for spec in items:
            obj = await apply(api, spec)
            print(f"[nixkube] {obj.kind}/{obj.name} applied", flush=True)


async def wait_for_driver(api: kr8s.asyncio.Api, cp: Machine, settings: Settings) -> None:
    """The DaemonSet's pod reaches Ready, and kubelet knows the driver.

    Reported together because either alone is misleading: a Ready pod whose
    registrar never reached kubelet cannot serve a volume, and a CSIDriver
    object exists from the moment the manifest applies.
    """

    async def ready() -> tuple[bool, str]:
        data = raw(await DaemonSet.get(settings["nodeDaemonSet"], namespace=NAMESPACE, api=api))
        status = data.get("status", {})
        want = status.get("desiredNumberScheduled", 0)
        got = status.get("numberReady", 0)
        # What says whether this is progressing, pulling, or stuck in a
        # crash loop -- and nothing that ticks on its own. See `pod_summary`.
        pods = await listed(Pod, api, namespace=NAMESPACE)
        return bool(want) and got == want, f"{got}/{want} ready, pods {pod_summary(pods)}"

    await until("the node DaemonSet", ready, READY_TIMEOUT, cp)
    print("[nixkube] the node DaemonSet is Ready", flush=True)

    drivers = [d.name for d in await listed(CSIDriver, api)]
    if "nixkube" not in drivers:
        raise MachineError(f"[cp] the API server serves no nixkube CSIDriver: {drivers}")
    print(f"[nixkube] csidrivers: {', '.join(drivers)}", flush=True)


async def wait_for_pynixd(api: kr8s.asyncio.Api, cp: Machine) -> None:
    """pynixd reaches Ready, out of the store its own initContainer filled.

    This is the only test of that path. `appstarter init` writes the PVC at
    /nix-volume, the pod mounts the same claim at /data and at /nix, and
    `appstarter run` execs out of it. Nothing built it here: the guest's own
    store holds `cacheEnv`, so the fetch is a local one.

    Ready means the startup probe answered on port 22, which is pynixd's own
    SSH server and therefore the process, not the container. Issue #49.
    """

    async def ready() -> tuple[bool, str]:
        data = raw(await StatefulSet.get(STATEFULSET, namespace=NAMESPACE, api=api))
        status = data.get("status", {})
        want = status.get("replicas", 0)
        got = status.get("readyReplicas", 0)
        pods = await listed(Pod, api, namespace=NAMESPACE, label_selector={"app.kubernetes.io/component": "pynixd"})
        return bool(want) and got == want, f"{got}/{want} ready, {pod_summary(pods)}"

    await until("the pynixd StatefulSet", ready, READY_TIMEOUT, cp)

    # Which version it started, and whether that is the one asked for. An
    # `appstarter init` that fell back to the image leaves these different,
    # and a pod that is Ready either way would not say so.
    said = await logs(await Pod.get(f"{STATEFULSET}-0", namespace=NAMESPACE, api=api), "appstarter-init")
    print(f"[nixkube] pynixd is Ready; appstarter-init said:\n{said}", flush=True)
    if "store holds" not in said:
        raise MachineError(f"[cp] pynixd started, and appstarter-init never said what it holds:\n{said}")
    # `appstarter/seed.py` writes "store holds X" when the fetch worked and
    # "store holds X, and the deployment asks for Y" when it fell back to the
    # image. Both carry "store holds", so the check above passes on the
    # fallback -- the one outcome this test exists to catch. Measured on
    # nixlab2 2026-09-22 against an unfetchable path: the pod reached 1/1
    # Running with exit 0 from the initContainer.
    if FALLBACK_MARK in said:
        raise MachineError(
            f"[cp] appstarter-init fell back to the image instead of fetching what the deployment asks for:\n{said}"
        )


async def check_workloads(api: kr8s.asyncio.Api, cp: Machine) -> None:
    """The driver mounts a store path where the pod asked for it.

    Both jobs run /mnt/csi/nix/store/...-hello/bin/hello -- through the
    volume, and not through /nix/store. That distinction is the whole
    assertion: vivarium bind-mounts the guest's store into every
    container, so a job that ran the same binary from /nix/store would
    complete with the driver switched off. Only the driver can put a file
    under /mnt/csi.

    They ask for the same store path at the same time, which is also the
    question a driver that shares one mount between volumes gets wrong.

    `backoffLimit = 0`, so a failure is final and this sees it at once
    rather than after six retries.
    """
    selector = {"app.kubernetes.io/component": "uml-test"}

    async def finished() -> tuple[bool, str]:
        jobs = await listed(Job, api, namespace=NAMESPACE, label_selector=selector)
        seen = {j.name: raw(j).get("status", {}) for j in jobs}
        failed = sorted(name for name, s in seen.items() if s.get("failed"))
        if failed:
            raise MachineError(
                f"[cp] the workload jobs failed: {', '.join(failed)}.\n"
                "The container's command is a path inside the CSI volume, so"
                " this says the driver did not mount it there."
            )
        done = sorted(name for name, s in seen.items() if s.get("succeeded"))
        summary = ", ".join(f"{name}={'ok' if name in done else '...'}" for name in sorted(seen))
        if len(done) != len(WORKLOADS):
            # Which pod, and in what state. "csi-path=..." for five minutes
            # says nothing about why.
            pods = await listed(Pod, api, namespace=NAMESPACE, label_selector=selector)
            summary = f"{summary} -- {pod_summary(pods)}"
        return len(done) == len(WORKLOADS), summary or "no jobs yet"

    await until("the workload jobs", finished, READY_TIMEOUT, cp)

    for job in WORKLOADS:
        out = await job_logs(api, job)
        if "Hello, world!" not in out:
            raise MachineError(f"[cp] {job} completed but said nothing:\n{out}")
    print(f"[nixkube] {', '.join(WORKLOADS)} ran out of the volume", flush=True)


async def probe(  # noqa: PLR0913 -- each keyword is one way a scenario varies the pod
    api: kr8s.asyncio.Api,
    cp: Machine,
    settings: Settings,
    why: str,
    runtime_class: str | None = None,
    wants: tuple[str, ...] = ("ro", "rw"),
    host_users: bool = True,
) -> None:
    """Create one pod that wants both mount paths, and see that it gets them.

    This is the question every chaos scenario asks: after that, can a pod
    still start? A pod that was already running cannot answer it, so each
    scenario makes a new one.

    Two containers, two answers. `csi` runs a binary that only the driver
    can put where the pod asked. `nri` prints its own mount table, because
    running a binary proves nothing here -- /nix/store is bound into every
    container, so it would run with the plugin switched off. A mount point
    of exactly `/nix` is the plugin's work and nobody else's.

    Read-only and read-write are told apart by the mount options, not the
    filesystem type: the plugin builds the writable one as an overlay, and
    the guest's own /nix is an overlay too, so the type says nothing.

    *runtime_class* runs the pod under that RuntimeClass instead of the
    default runc. Issue #74. *wants* limits it to some of `PROBES`.
    *host_users* false runs the pod in a user namespace of its own, so
    every mount reaches it through an idmapped mount.
    """

    def edit(job: Manifest) -> None:
        pod = job["spec"]["template"]["spec"]
        if runtime_class is not None:
            pod["runtimeClassName"] = runtime_class
        if not host_users:
            pod["hostUsers"] = False

    for want, key in PROBES:
        if want not in wants:
            continue
        job = await created_job(api, settings[key], edit)

        async def done(job: Job = job, want: str = want) -> tuple[bool, str]:
            await job.refresh()
            status = raw(job).get("status", {})
            pods = await job_pods(api, job.name)
            if status.get("failed"):
                raise MachineError(
                    f"[cp] a {want} probe failed after {why}. A pod that"
                    f" cannot start is the whole question here.\n{pod_summary(pods)}"
                )
            return bool(status.get("succeeded")), f"{job.name}: {pod_summary(pods)}"

        await until(f"a {want} probe after {why}", done, READY_TIMEOUT, cp)

        csi = await job_logs(api, job.name, "csi")
        if "Hello, world!" not in csi:
            raise MachineError(
                f"[cp] the {want} probe's csi container said nothing after"
                f" {why}, so the volume is not what the pod asked for:\n{csi}"
            )

        nri = await job_logs(api, job.name, "nri")
        mount = nix_mount(nri)
        if mount is None:
            raise MachineError(
                f"[cp] the {want} probe has no mount at /nix after {why}, so"
                f" the NRI plugin did not touch it. Its mount table was:\n{nri}"
            )
        options, fstype = mount
        # ./probe.nix's `wroteMarker`: the rw probe's root wrote into /nix.
        if want == "rw" and "nixkube-probe-wrote-nix" not in nri:
            raise MachineError(
                f"[cp] the rw probe's root could not write into /nix after {why}"
                f" (hostUsers={host_users}). Its output was:\n{nri}"
            )
        # The API server drops `hostUsers` when its feature gate is off, and
        # the pod then passes on the host's ids. Asked of the container.
        if own_user_namespace(nri) == host_users:
            raise MachineError(
                f"[cp] the {want} probe asked for hostUsers={host_users} after"
                f" {why}, and its uid map says otherwise:\n{nri}"
            )
        if want not in options.split(","):
            raise MachineError(
                f"[cp] the {want} probe wanted a {want} /nix after {why} and got `{options}` ({fstype})."
            )
        # Without this a node that falls back to the farm -- a probe that
        # fails, a kernel without EROFS -- passes every other check.
        composefs = nix_is_composefs(nri)
        if composefs != settings["nriComposefs"]:
            raise MachineError(
                f"[cp] the {want} probe's /nix is"
                f" {'' if composefs else 'not '}composefs after {why}, and"
                f" nriComposefs is {settings['nriComposefs']}:\n{nri}"
            )

        await delete(job, cp)
        under = "" if runtime_class is None else f" under {runtime_class}"
        shown = "composefs" if composefs else "not composefs"
        print(
            f"[nixkube] a {want} probe got both mounts{under} after {why}: /nix {options} ({fstype}, {shown})",
            flush=True,
        )


# What `nri/server.py` says when a runtime keeps the container out of reach.
UNREACHABLE = "rootfs is not reachable through pid"

# A container that will not start, as its waiting reason says.
NEVER_STARTS = frozenset({"StartError", "RunContainerError", "CreateContainerError", "CrashLoopBackOff"})


def ended(pod: Manifest) -> bool:
    """Whether every container of *pod* ran to an end, or will not start."""
    statuses = pod.get("status", {}).get("containerStatuses", [])
    return bool(statuses) and all(
        "terminated" in s.get("state", {})
        or s.get("state", {}).get("waiting", {}).get("reason") in NEVER_STARTS
        or "terminated" in s.get("lastState", {})
        for s in statuses
    )


async def all_logs(pod: Pod) -> str:
    """Every container's logs, as `kubectl logs --all-containers` prints them with stderr.

    A container that never started has no log, and the API server says so;
    that answer goes in the text, where a caller that wanted the log sees why.
    """
    said = []
    spec = raw(pod)["spec"]
    for container in spec.get("initContainers", []) + spec["containers"]:
        try:
            said.append(await logs(pod, container["name"]))
        except kr8s.ServerError as e:
            said.append(f"{container['name']}: {e}")
    return "\n".join(said)


async def run_job(
    api: kr8s.asyncio.Api, cp: Machine, source: str, edit: Callable[[Manifest], None], what: str
) -> tuple[Manifest, str]:
    """Create a Job from *source* through *edit*; wait until it ends.

    Returns its pod as the API server has it, and its logs. The pod ends
    either way here, so nothing is raised on failure: the caller says which
    outcome it wanted.
    """
    job = await created_job(api, source, edit)

    async def done() -> tuple[bool, str]:
        pods = await job_pods(api, job.name)
        return bool(pods) and all(ended(raw(p)) for p in pods), pod_summary(pods)

    await until(what, done, READY_TIMEOUT, cp)
    pod = (await job_pods(api, job.name))[0]
    said = await all_logs(pod)
    await delete(job, cp)
    return raw(pod), said


async def check_sandboxed(api: kr8s.asyncio.Api, cp: Machine, settings: Settings, runtime_class: str) -> None:
    """Under a sandboxed runtime: CSI works, and NRI refuses by name.

    Measured under gVisor (issue #74): a CSI volume reaches the sandbox
    through its gofer, and NRI cannot, because the pid the hook reports is
    the sandbox's and its root is not the host's.
    """

    def csi(job: Manifest) -> None:
        template = job["spec"]["template"]
        template["spec"]["runtimeClassName"] = runtime_class
        template.setdefault("metadata", {})["annotations"] = {"nixkube/pod-exclude": "true"}
        template["spec"]["containers"] = [
            {
                **c,
                "command": [f"{settings['workloadStorePath']}/bin/hello"],
                "volumeMounts": [{"name": "store", "mountPath": "/nix", "subPath": "nix"}],
            }
            for c in template["spec"]["containers"]
            if c["name"] == "csi"
        ]
        job["metadata"]["generateName"] = f"csi-{runtime_class}-"

    _, said = await run_job(api, cp, settings["probeRo"], csi, f"a CSI pod under {runtime_class}")
    if "Hello, world!" not in said:
        raise MachineError(f"[cp] a CSI volume at /nix did not run hello under {runtime_class}:\n{said}")
    print(f"[nixkube] a CSI volume works under {runtime_class}", flush=True)

    def nri(job: Manifest) -> None:
        job["spec"]["template"]["spec"]["runtimeClassName"] = runtime_class
        job["metadata"]["generateName"] = f"nri-{runtime_class}-"

    pod, _ = await run_job(api, cp, settings["probeRo"], nri, f"an NRI pod under {runtime_class}")
    messages = " ".join(
        s.get("state", {}).get("waiting", {}).get("message", "")
        + s.get("state", {}).get("terminated", {}).get("message", "")
        + s.get("lastState", {}).get("terminated", {}).get("message", "")
        for s in pod["status"].get("containerStatuses", [])
    )
    if UNREACHABLE not in messages:
        raise MachineError(
            f"[cp] an NRI pod under {runtime_class} did not fail with the reason"
            f" ({UNREACHABLE!r}); its containers said:\n{messages}"
        )
    print(f"[nixkube] NRI refuses {runtime_class} and says why", flush=True)


# What `nri/vm.py` says when a VM pod asks for a writable /nix.
VM_RW_REFUSED = "a writable /nix is not offered under a VM runtime"


async def check_vm(api: kr8s.asyncio.Api, cp: Machine, settings: Settings, runtime_class: str) -> None:
    """Under a VM runtime: a read-only /nix through virtio-fs, and no rw.

    The ro probe asks what it asks under runc: both mounts, and a mount at
    exactly /nix. The rw probe must fail in its nri container with the
    reason, which the waiter prints before it exits.
    """
    await probe(api, cp, settings, "a clean start", runtime_class=runtime_class, wants=("ro",))

    def rw(job: Manifest) -> None:
        job["spec"]["template"]["spec"]["runtimeClassName"] = runtime_class
        job["metadata"]["generateName"] = f"rw-{runtime_class}-"

    _, said = await run_job(api, cp, settings["probeRw"], rw, f"an rw probe under {runtime_class}")
    if VM_RW_REFUSED not in said:
        raise MachineError(
            f"[cp] an rw probe under {runtime_class} was not refused with the"
            f" reason ({VM_RW_REFUSED!r}); its containers said:\n{said}"
        )
    print(f"[nixkube] NRI refuses a writable /nix under {runtime_class} and says why", flush=True)


def state_dirs(settings: Settings) -> dict[str, str]:
    """Where the driver's own state lands on the node.

    The driver's constants are pod paths -- CSI_ROOT is `/nix/var/nix-csi`
    inside the node container -- and the container's /nix is the host's
    `<hostMountPath>/nix`. So on the node they sit under that.
    """
    # A host-store node's /nix is the host's own.
    root = "" if settings["hostStore"] else settings["hostMountPath"].rstrip("/")
    return {
        "volumes": f"{root}/nix/var/nix-csi/volumes",
        "containers": f"{root}/nix/var/nix-csi/containers",
        "gcroots": f"{root}/nix/var/nix/gcroots/nix-csi",
    }


def reconciled(settings: Settings) -> str:
    """Everything the driver holds that the node no longer has. Empty is the pass.

    Not "the directories are empty". With NRI on, every container whose
    command names a store path gets an entry, and the control plane's own
    static pods are exactly that -- etcd, the API server and kube-proxy all
    run out of the store. The invariant is instead the one the driver's own
    garbage collection claims to keep: an entry for a container the CRI no
    longer lists, or a volume kubelet no longer records, is a leak.

    The CRI is asked twice, before and after reading the directory, and an
    entry counts as stale only if it is in neither answer. A container
    created or removed while this runs would otherwise look like a leak,
    and does: measured, with a different container id each poll.

    `findmnt --list`, because the default output draws a tree and the
    box-drawing characters end up in the path.
    """
    d = state_dirs(settings)
    return f"""
    live=$(mktemp); have_c=$(mktemp); have_v=$(mktemp)
    crictl ps --all --quiet 2>/dev/null > "$live"
    ls -1 {d["containers"]} 2>/dev/null | sort > "$have_c"
    ls -1 {d["volumes"]} 2>/dev/null | sort > "$have_v"
    ls -1 {d["gcroots"]} 2>/dev/null | sort > "$have_v.g"
    crictl ps --all --quiet 2>/dev/null >> "$live"
    sort -u -o "$live" "$live"

    cat /var/lib/kubelet/pods/*/volumes/kubernetes.io~csi/*/vol_data.json 2>/dev/null \
      | jq --raw-output .volumeHandle 2>/dev/null | sort -u > "$live.v"

    comm -23 "$have_c" "$live"   | sed 's|^|stale container |'
    comm -23 "$have_v" "$live.v" | sed 's|^|stale volume |'
    comm -23 "$have_v.g" "$live.v" | sed 's|^|stale gcroot |'
    findmnt --list --noheadings --output TARGET | grep kubernetes.io~csi | while read -r t; do
      [ -f "$(dirname "$t")/vol_data.json" ] || echo "stale mount $t"
    done
    rm -f "$live" "$live.v" "$have_c" "$have_v" "$have_v.g"
"""


async def check_reconciled(cp: Machine, settings: Settings, what: str) -> None:
    """Nothing the driver kept outlives what the node still has."""

    async def clean() -> tuple[bool, str]:
        out = (await cp.execute(reconciled(settings), timeout=120)).stdout
        left = [line.strip() for line in out.splitlines() if line.strip()]
        return not left, f"{len(left)} stale -- " + "; ".join(left[:6])

    await until(f"the node's state to settle after {what}", clean, READY_TIMEOUT, cp)
    print(f"[nixkube] nothing stale on the node after {what}", flush=True)


async def on_node(cp: Machine, command: str, timeout: int = 120) -> None:
    """Do something to the node, and say what came of it.

    `execute` rather than `succeed`: several of these are expected to return
    non-zero, and the interesting failures are the ones that come afterwards.
    """
    done = await cp.execute(command, timeout=timeout)
    text = " ".join(filter(None, [done.stdout.strip()[:300], done.stderr.strip()[:300]]))
    print(f"[nixkube] $ {command}\n  rc={done.returncode} {text}", flush=True)


NODE_LABELS = {"app.kubernetes.io/component": "node"}

# The container id and the sandbox id of the running driver, for the tools
# that work below Kubernetes.
NODE_CONTAINER = "$(crictl ps --quiet --name '^nix-node$')"
NODE_SANDBOX = "$(crictl pods --quiet --name '^nix-node-')"

type Scenario = Callable[[kr8s.asyncio.Api, Machine, Settings], Awaitable[None]]


async def kubernetes_deletes_the_pod(api: kr8s.asyncio.Api, cp: Machine, settings: Settings) -> None:
    # No wait: the DaemonSet's replacement is what the scenario is about.
    for pod in await listed(Pod, api, namespace=NAMESPACE, label_selector=NODE_LABELS):
        await pod.delete()


async def crictl_stops_the_container(api: kr8s.asyncio.Api, cp: Machine, settings: Settings) -> None:
    await on_node(cp, f"crictl stop {NODE_CONTAINER}")


async def crictl_removes_the_container(api: kr8s.asyncio.Api, cp: Machine, settings: Settings) -> None:
    await on_node(cp, f"crictl rm --force {NODE_CONTAINER}")


async def crictl_removes_the_sandbox(api: kr8s.asyncio.Api, cp: Machine, settings: Settings) -> None:
    await on_node(cp, f"crictl rmp --force {NODE_SANDBOX}")


async def the_process_is_killed(api: kr8s.asyncio.Api, cp: Machine, settings: Settings) -> None:
    # By pid from the CRI, not by name. The guest is the node, so a pattern
    # match would find the test's own tooling as readily as the driver.
    await on_node(cp, f"kill -9 $(crictl inspect {NODE_CONTAINER} | jq --raw-output .info.pid)")


async def kubelet_restarts(api: kr8s.asyncio.Api, cp: Machine, settings: Settings) -> None:
    await on_node(cp, "systemctl restart kubelet")


# containerd or crio: the unit `services.vivarium-k8s.cri` puts behind this target.
RUNTIME_UNIT = "$(systemctl show --property Requires --value vivarium-k8s-cri.target)"


async def the_runtime_restarts(api: kr8s.asyncio.Api, cp: Machine, settings: Settings) -> None:
    # The big one. Every container on the node dies, including the control
    # plane, and the NRI connection the plugin holds goes with it. A
    # runtime upgrade does exactly this.
    await on_node(cp, f"systemctl restart {RUNTIME_UNIT}")


async def the_csi_socket_is_deleted(api: kr8s.asyncio.Api, cp: Machine, settings: Settings) -> None:
    await on_node(cp, "rm -f /var/lib/kubelet/plugins/nixkube/csi.sock")


async def the_state_is_wiped(api: kr8s.asyncio.Api, cp: Machine, settings: Settings) -> None:
    d = state_dirs(settings)
    await on_node(cp, f"rm -rf {d['volumes']}/* {d['containers']}/*")


# What the pod that was already running is entitled to afterwards.
#
# KEEPS is the normal answer, and it is the strong one: the scenario breaks
# the driver and leaves the node's files alone, so a pod that was already
# served must still hold its mounts.
#
# REPLACED is for the one scenario that deletes those files. The resident's
# CSI volume root is among them, and a bind mount whose source is unlinked
# goes on serving the unlinked directory -- the kernel marks it "//deleted"
# and nothing outside the pod's mount namespace can repair it. Asking that
# pod to still work would be asking for the impossible, so the question
# becomes the honest one: does the Deployment's replacement come up whole?
KEEPS = "keeps its mounts"
REPLACED = "is replaced"

SCENARIOS: tuple[tuple[str, Scenario, str], ...] = (
    ("kubernetes deletes the pod", kubernetes_deletes_the_pod, KEEPS),
    ("crictl stops the container", crictl_stops_the_container, KEEPS),
    ("crictl removes the container", crictl_removes_the_container, KEEPS),
    ("crictl removes the sandbox", crictl_removes_the_sandbox, KEEPS),
    ("the process is killed outright", the_process_is_killed, KEEPS),
    ("kubelet restarts", kubelet_restarts, KEEPS),
    ("the container runtime restarts", the_runtime_restarts, KEEPS),
    ("its csi socket is deleted", the_csi_socket_is_deleted, KEEPS),
    ("its state directories are wiped", the_state_is_wiped, REPLACED),
)


async def wait_for_apiserver(api: kr8s.asyncio.Api, cp: Machine) -> None:
    """The API server answers again.

    Half of these scenarios take the control plane down with them, so a
    refused or dropped request here is the wait going on, not a failure.
    """

    async def up() -> tuple[bool, str]:
        try:
            version = await api.version()
        except UNREACHABLE_API as e:
            return False, f"{type(e).__name__}: {e}"[:120]
        return True, str(version.get("gitVersion"))

    await until("the api server to answer", up, READY_TIMEOUT, cp)


async def break_and_recover(api: kr8s.asyncio.Api, cp: Machine, settings: Settings, name: str) -> None:
    """Break the driver one way, and ask if a pod can still start.

    nixkube is not a component a node can do without: a pod that wants a
    store path does not start until the driver serves it. So the question
    after each of these is never "did it come back" alone -- it is whether a
    *new* pod gets both mounts, and whether the driver left anything behind.

    One scenario per call: `nix/uml/chaos/` parametrizes over SCENARIOS, so
    each is a test of its own and `-k` selects one.
    """
    action, resident = next((a, r) for n, a, r in SCENARIOS if n == name)
    await action(api, cp, settings)
    await wait_for_apiserver(api, cp)
    await wait_for_driver(api, cp, settings)
    await probe(api, cp, settings, name)
    if resident is REPLACED:
        await replace_served_pods(api, cp)
    await check_resident(api, cp, settings, name)
    await check_reconciled(cp, settings, name)


RESIDENT = {"app.kubernetes.io/component": "uml-resident"}


async def replace_served_pods(api: kr8s.asyncio.Api, cp: Machine) -> None:
    """Delete every pod on the node that a nixkube volume serves.

    The wipe breaks all of them, not the resident alone. The rollout
    controller is one: its rw store loses its overlay's upper and work
    directories, and every build after that fails with ENOENT on
    `/nix/store/.links` -- the host phase, minutes later.

    Kubelet's own record of the pod's volumes names them, so no node name
    is needed. Each delete waits, so the old resident is gone before
    `check_resident` counts pods and finds the new one.
    """
    out = (
        await cp.execute(
            'grep --files-with-matches \'"driverName":"nixkube"\' '
            "/var/lib/kubelet/pods/*/volumes/kubernetes.io~csi/*/vol_data.json | cut -d/ -f6 | sort -u",
            timeout=60,
        )
    ).stdout
    uids = set(out.split())
    for pod in await listed(Pod, api, namespace=kr8s.ALL):
        if raw(pod)["metadata"]["uid"] in uids:
            await delete(pod, cp)


async def check_resident(api: kr8s.asyncio.Api, cp: Machine, settings: Settings, why: str) -> None:
    """The pod that was already running still has what it was given.

    A driver that comes back but leaves the pods it was serving without
    their mounts has not recovered, it has restarted. A fresh probe cannot
    see that -- it gets its mounts from the driver that is up now.

    So this asks the pod itself. An exec runs in the container's own mount
    namespace, so the mount table it prints is that container's, and the
    binary it runs only resolves through the CSI volume.
    """
    found: list[Pod] = []

    async def up() -> tuple[bool, str]:
        pods = await listed(Pod, api, namespace=NAMESPACE, label_selector=RESIDENT)
        running = [p for p in pods if pod_state(raw(p)) == "Running" and pod_ready(raw(p)) == "1/1"]
        found[:] = running
        return len(running) == 1, pod_summary(pods)

    await until(f"the resident pod after {why}", up, READY_TIMEOUT, cp)
    pod = found[0]

    hello = (await pod.exec(shlex.split(settings["residentHello"]), timeout=CALL_TIMEOUT)).stdout.decode()
    if "Hello, world!" not in hello:
        raise MachineError(
            f"[cp] the resident pod lost its volume after {why}: its binary under /mnt/csi said\n{hello}"
        )

    command = [*shlex.split(settings["residentCat"]), "/proc/self/mountinfo"]
    mounts = (await pod.exec(command, timeout=CALL_TIMEOUT)).stdout.decode()
    if nix_mount(mounts) is None:
        raise MachineError(f"[cp] the resident pod lost its /nix after {why}. Its mount table was:\n{mounts}")
    print(f"[nixkube] {pod.name} still has both mounts after {why}", flush=True)


def csi_presentations(mountinfo: str) -> dict[str, str]:
    """Each CSI volume's target, and how it is presented: "composefs" for
    an overlay with a data-only lower layer (`::`), "hardlinks" for anything
    else, which is a bind of a hardlink tree."""
    found = {}
    for line in mountinfo.splitlines():
        fields = line.split()
        point = fields[4]
        if "/volumes/kubernetes.io~csi/" not in point or not point.endswith("/mount"):
            continue
        after = fields[fields.index("-") + 1 :]
        fstype, options = after[0], after[2] if len(after) > 2 else ""
        found[point] = "composefs" if fstype == "overlay" and "::" in options else "hardlinks"
    return found


async def check_presentation(cp: Machine, settings: Settings) -> None:
    """Every CSI volume on the node is presented the way the deployment
    asked. Without this a node that falls back to hardlinks -- a probe that
    fails, a kernel without EROFS -- passes every other check. Issue #68."""
    wanted = "composefs" if settings["csiComposefs"] else "hardlinks"
    seen = csi_presentations((await cp.succeed("cat /proc/1/mountinfo")).stdout)
    if not seen:
        raise MachineError("[cp] no CSI volume is mounted on the node to look at")
    wrong = {p: kind for p, kind in seen.items() if kind != wanted}
    if wrong:
        raise MachineError(f"[cp] CSI volumes not presented as {wanted}: {wrong}")
    print(f"[nixkube] {len(seen)} CSI volumes presented as {wanted}", flush=True)


async def check_unmount(api: kr8s.asyncio.Api, cp: Machine, settings: Settings) -> None:
    """The mounts go away when the pods do.

    A driver that mounts and never unmounts looks exactly like a working
    one, until a node has run for a week. `check_reconciled` is the same
    question the chaos scenarios ask, so it is the same answer here: once
    the jobs are gone, nothing of theirs may be left.

    The jobs are gone by the end of this, which is why it runs last among
    the clean-start checks -- `report` can no longer read their logs, and
    `check_workloads` has already read them. The resident Deployment stays
    up, and its volume is not stale, so it does not confuse this.
    """
    for name in WORKLOADS:
        await delete(await Job.get(name, namespace=NAMESPACE, api=api), cp)
    await check_reconciled(cp, settings, "the workload jobs are deleted")


async def said(cp: Machine, command: str) -> tuple[int, str]:
    """*command*'s exit status and all it printed, stdout and stderr in order."""
    done = await cp.execute(f"{{ {command}\n}} 2>&1", timeout=120)
    return done.returncode, done.stdout


async def reported(what: str, section: Callable[[], Awaitable[str]]) -> None:
    """Print one section of the report, or why it could not be read.

    The report runs after a failure, when the API server may be the thing
    that failed. One section that cannot be read must not cost the others.
    """
    try:
        with anyio.fail_after(2 * CALL_TIMEOUT):
            text = await section()
    except UNREPORTABLE as e:
        text = f"could not read it: {type(e).__name__}: {e}"
    print(f"[nixkube] {what}:\n{text}", flush=True)


def described(obj: APIObject) -> str:
    if obj.kind == "Pod":
        return f"pod/{obj.name} {pod_ready(raw(obj))} {pod_state(raw(obj))}"
    return f"{obj.kind.lower()}/{obj.name} {json.dumps(raw(obj).get('status', {}))[:300]}"


async def namespace_objects(api: kr8s.asyncio.Api) -> str:
    """What `kubectl get all` lists, a line each."""
    lines = []
    for kind in (Pod, Service, DaemonSet, Deployment, StatefulSet, Job):
        lines += [described(o) for o in await listed(kind, api, namespace=NAMESPACE)]
    return "\n".join(lines)


async def namespace_events(api: kr8s.asyncio.Api) -> str:
    """The namespace's events, oldest first."""
    events = [raw(e) for e in await listed(Event, api, namespace=NAMESPACE)]
    events.sort(key=lambda e: e.get("lastTimestamp") or e.get("eventTime") or "")
    return "\n".join(
        f"{e.get('lastTimestamp') or e.get('eventTime')} {e.get('type')} {e.get('reason')}"
        f" {e['involvedObject'].get('kind')}/{e['involvedObject'].get('name')}: {e.get('message', '')}"
        for e in events
    )


# The NRI sweep's own events, and the runtime's removal of a container.
SWEEP = re.compile(r'"(gc_[a-z_]+|synchronize)"|REMOVE_CONTAINER')


async def node_lines(api: kr8s.asyncio.Api, keep: Callable[[str], bool], **kwargs: Any) -> list[str]:  # noqa: ANN401 -- `Pod.logs` keywords
    """The lines of every node pod's nix-node log that *keep* accepts."""
    lines = []
    for pod in await listed(Pod, api, namespace=NAMESPACE, label_selector=NODE_LABELS):
        lines += [line for line in (await logs(pod, "nix-node", timestamps=True, **kwargs)).splitlines() if keep(line)]
    return lines


async def selected_logs(api: kr8s.asyncio.Api, selector: dict[str, str], tail: int) -> str:
    """`kubectl logs deploy/<name>`: the first pod the selector matches."""
    pods = await listed(Pod, api, namespace=NAMESPACE, label_selector=selector)
    return await logs(pods[0], tail_lines=tail) if pods else "no pod"


# What the controller's /nix is made of, from the node: its mounts, its
# overlay's upper and work directories, what the kernel said about overlays,
# and whether it can write into its store. The last line names its volume.
CONTROLLER_NIX = (
    "id=$(crictl ps --name controller --quiet | head -n 1);"
    " pid=$(crictl inspect --output go-template --template '{{.info.pid}}' $id);"
    " root=/proc/$pid/root;"
    " findmnt --task $pid --list --noheadings --output TARGET,FSTYPE,OPTIONS | grep nix;"
    " opts=$(findmnt --task $pid --list --noheadings --output OPTIONS --mountpoint /nix);"
    " for d in upperdir workdir; do"
    '   p=$(echo "$opts" | tr , \'\\n\' | sed -n "s/^$d=//p");'
    '   [ -n "$p" ] && { echo "$d $p:"; ls -la "$p" 2>&1 | head -n 4; };'
    " done;"
    " dmesg | grep -i overlay | tail -n 20;"
    " ls -la $root/nix $root/nix/store | head -n 12;"
    " ls -la $root/nix/store/.links 2>&1 | head -n 3;"
    " mkdir $root/nix/store/.uml-probe && echo 'mkdir in /nix/store: ok';"
    " rmdir $root/nix/store/.uml-probe 2>/dev/null;"
    " echo \"volume=$(echo \"$opts\" | tr , '\\n' | sed -n 's|^upperdir=.*/\\(csi-[0-9a-f]*\\)/upper$|\\1|p')\""
)


async def report(api: kr8s.asyncio.Api, cp: Machine) -> None:
    """What the driver's own containers said, pass or fail.

    This runs for an hour on a builder. A log that says only "passed" makes
    the next failure start from nothing.
    """
    await reported(f"the objects in {NAMESPACE}", lambda: namespace_objects(api))
    await reported(f"the events in {NAMESPACE}", lambda: namespace_events(api))

    # What the node container's /nix is made of, from the node's side.
    #
    # The init container writes an out-link at /nix/var/result and the node
    # container's PATH is /nix/var/result/bin. When that entrypoint does not
    # resolve, runc says only that it is "not found", so these three answer
    # the question runc will not: is the link there, does it point at
    # something, and did kubelet's subPath bind carry the store.
    rc, out = await said(
        cp,
        "ls -l /nix/var/result;"
        " ls /nix/var/result/bin 2>&1 | head -n 5;"
        " findmnt --output TARGET,SOURCE,FSTYPE | grep -i nix;"
        " ls -d /var/lib/kubelet/pods/*/volume-subpaths/*/*/* 2>&1 | head -n 10",
    )
    print(f"[nixkube] the node's /nix (rc={rc}):\n{out}", flush=True)

    # What the NRI sweep did, and why a volume outlived its container: a
    # sweep that skipped it, failed on it, or never ran.
    async def sweep() -> str:
        return "\n".join((await node_lines(api, lambda line: bool(SWEEP.search(line)), tail_lines=5000))[-80:])

    await reported("the NRI sweep", sweep)

    # What the nri-wait hook said before it gave up.
    #
    # The hook runs at createRuntime, so its stderr goes into runc's error,
    # into containerd's, and from there into a Kubernetes event. kubelet
    # cuts that event's message at about 1KB, which lands mid-sentence and
    # drops the one line that says which deadline the hook hit. containerd's
    # own journal keeps the whole thing. Measured: the event ended at
    # "[nri-wait] OCI state: id=hello pid=2081 bundle=..." and the verdict
    # was 900 characters further on.
    rc, out = await said(
        cp,
        f"journalctl --unit {RUNTIME_UNIT} --no-pager --lines 4000 | grep --text nri-wait | tail -n 20",
    )
    print(f"[nixkube] what nri-wait said (rc={rc}):\n{out}", flush=True)

    # The NixOSRollout controller: what its builds said, and its /nix as the
    # container sees it, read through /proc/<pid>/root from the node. A
    # source build writes .drv files into that /nix, through the CSI volume.
    async def controller() -> str:
        deployment = raw(await Deployment.get("nixkube-nixos-rollout", namespace=NAMESPACE, api=api))
        return await selected_logs(api, deployment["spec"]["selector"]["matchLabels"], 80)

    await reported("logs deploy/nixkube-nixos-rollout", controller)
    rc, out = await said(cp, CONTROLLER_NIX)
    print(f"[nixkube] the controller's /nix (rc={rc}):\n{out}", flush=True)
    volume = next((line.removeprefix("volume=") for line in out.splitlines() if line.startswith("volume=")), "")
    if volume:

        async def node_did() -> str:
            lines = await node_lines(api, lambda line: volume in line and "gc_active" not in line)
            return "\n".join(lines[-25:])

        await reported(f"what the node did with {volume}", node_did)

    # Every container of the node's and pynixd's pods, by name.
    #
    # `diagnose` tails the log files off the node, which works but competes
    # for a line budget with everything else running. Asking kubelet for one
    # container's output is exact, and `previous` is the only way to see a
    # container that has already exited -- which a crash-looping init
    # container always has by the time anybody looks.
    for component, containers in (
        ("node", ("appstarter-init", "nix-node")),
        ("pynixd", ("appstarter-init", "pynixd")),
    ):
        try:
            pods = await listed(
                Pod, api, namespace=NAMESPACE, label_selector={"app.kubernetes.io/component": component}
            )
        except UNREPORTABLE as e:
            print(f"[nixkube] the {component} pods: could not list them: {e}", flush=True)
            continue
        for pod in pods:
            for container in containers:
                for previous in (False, True):
                    # The API server refuses the log of a container that has
                    # not run, or has no previous run: nothing to show.
                    try:
                        out = await logs(pod, container, previous=previous, tail_lines=60)
                    except UNREPORTABLE:
                        continue
                    if out.strip():
                        flags = " --previous" if previous else ""
                        print(f"[nixkube] logs {pod.name} {container}{flags}:\n{out}", flush=True)

    # What the workloads said. A job that fails here fails for one of two
    # reasons -- the driver did not mount, or the thing it mounted is not
    # what the pod asked for -- and the container's own output separates
    # them.
    for job in WORKLOADS:
        await reported(f"logs job/{job}", lambda job=job: job_logs(api, job, tail_lines=40))
