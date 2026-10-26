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
"""

from vivarium_runner import Machine, MachineError
from vivarium_runner.cluster import get_json, kubectl, until

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


def pod_summary(out: str) -> str:
    """`kubectl get pods --no-headers` with the clocks taken out.

    `until` reports how long its evidence has been *unchanged*, and that
    figure is the one that separates slow from stuck. Both AGE and RESTARTS
    carry a time in them, so evidence built from the whole line changes
    every poll and the answer is always "0s unchanged" -- a stuck-detector
    that cannot detect anything.

    Name, ready count and status. Those change when something happens, and
    only then.
    """
    rows = [
        line.split()
        for line in out.splitlines()
        if line.split() and not line.startswith("No resources found")
    ]
    return " | ".join(f"{r[0]}={r[1]} {r[2]}" for r in rows if len(r) > 2) or "no pods"


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
    return any(
        m != ["0", "0", "4294967295"] for m in maps if all(f.isdigit() for f in m)
    )


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


async def deploy(cp: Machine, settings: dict) -> None:
    """Apply the manifest, the way the NixOS test does."""
    print("[nixkube] applying the manifest", flush=True)
    out = await kubectl(
        cp,
        f"apply --server-side --filename {settings['manifest']}",
        timeout=APPLY_TIMEOUT,
    )
    print(out, flush=True)


async def wait_for_driver(cp: Machine, settings: dict) -> None:
    """The DaemonSet's pod reaches Ready, and kubelet knows the driver.

    Reported together because either alone is misleading: a Ready pod whose
    registrar never reached kubelet cannot serve a volume, and a CSIDriver
    object exists from the moment the manifest applies.
    """

    async def ready():
        daemonset = settings["nodeDaemonSet"]
        data = await get_json(cp, f"get daemonset {daemonset} --namespace {NAMESPACE}")
        status = data.get("status", {})
        want = status.get("desiredNumberScheduled", 0)
        got = status.get("numberReady", 0)
        pods = await kubectl(
            cp,
            f"get pods --namespace {NAMESPACE} --output wide --no-headers",
        )
        # What says whether this is progressing, pulling, or stuck in a
        # crash loop -- and nothing that ticks on its own. See `pod_summary`.
        return (
            bool(want) and got == want,
            f"{got}/{want} ready, pods {pod_summary(pods)}",
        )

    await until("the node DaemonSet", ready, READY_TIMEOUT, cp)
    print("[nixkube] the node DaemonSet is Ready", flush=True)

    drivers = await kubectl(cp, "get csidriver --no-headers")
    if "nixkube" not in drivers:
        raise MachineError(
            f"[cp] the API server serves no nixkube CSIDriver:\n{drivers}"
        )
    print(f"[nixkube] csidrivers:\n{drivers}", flush=True)


async def wait_for_pynixd(cp: Machine) -> None:
    """pynixd reaches Ready, out of the store its own initContainer filled.

    This is the only test of that path. `appstarter init` writes the PVC at
    /nix-volume, the pod mounts the same claim at /data and at /nix, and
    `appstarter run` execs out of it. Nothing built it here: the guest's own
    store holds `cacheEnv`, so the fetch is a local one.

    Ready means the startup probe answered on port 22, which is pynixd's own
    SSH server and therefore the process, not the container. Issue #49.
    """

    async def ready():
        data = await get_json(
            cp, f"get statefulset {STATEFULSET} --namespace {NAMESPACE}"
        )
        status = data.get("status", {})
        want = status.get("replicas", 0)
        got = status.get("readyReplicas", 0)
        pods = await kubectl(
            cp,
            f"get pods --namespace {NAMESPACE}"
            " --selector app.kubernetes.io/component=pynixd --no-headers",
        )
        return bool(want) and got == want, f"{got}/{want} ready, {pod_summary(pods)}"

    await until("the pynixd StatefulSet", ready, READY_TIMEOUT, cp)

    # Which version it started, and whether that is the one asked for. An
    # `appstarter init` that fell back to the image leaves these different,
    # and a pod that is Ready either way would not say so.
    logs = await kubectl(
        cp,
        f"logs --namespace {NAMESPACE} {STATEFULSET}-0 --container appstarter-init",
    )
    print(f"[nixkube] pynixd is Ready; appstarter-init said:\n{logs}", flush=True)
    if "store holds" not in logs:
        raise MachineError(
            f"[cp] pynixd started, and appstarter-init never said what it holds:\n{logs}"
        )
    # `appstarter/seed.py` writes "store holds X" when the fetch worked and
    # "store holds X, and the deployment asks for Y" when it fell back to the
    # image. Both carry "store holds", so the check above passes on the
    # fallback -- the one outcome this test exists to catch. Measured on
    # nixlab2 2026-09-22 against an unfetchable path: the pod reached 1/1
    # Running with exit 0 from the initContainer.
    if FALLBACK_MARK in logs:
        raise MachineError(
            f"[cp] appstarter-init fell back to the image instead of fetching"
            f" what the deployment asks for:\n{logs}"
        )


async def check_workloads(cp: Machine) -> None:
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

    async def finished():
        data = await get_json(
            cp,
            f"get jobs --namespace {NAMESPACE}"
            " --selector app.kubernetes.io/component=uml-test",
        )
        seen = {
            item["metadata"]["name"]: item.get("status", {})
            for item in data.get("items", [])
        }
        failed = sorted(name for name, s in seen.items() if s.get("failed"))
        if failed:
            raise MachineError(
                f"[cp] the workload jobs failed: {', '.join(failed)}.\n"
                "The container's command is a path inside the CSI volume, so"
                " this says the driver did not mount it there."
            )
        done = sorted(name for name, s in seen.items() if s.get("succeeded"))
        summary = ", ".join(
            f"{name}={'ok' if name in done else '...'}" for name in sorted(seen)
        )
        if len(done) != len(WORKLOADS):
            # Which pod, and in what state. "csi-path=..." for five minutes
            # says nothing about why.
            pods = await kubectl(
                cp,
                f"get pods --namespace {NAMESPACE}"
                " --selector app.kubernetes.io/component=uml-test --no-headers",
            )
            summary = f"{summary} -- {pod_summary(pods)}"
        return len(done) == len(WORKLOADS), summary or "no jobs yet"

    await until("the workload jobs", finished, READY_TIMEOUT, cp)

    for job in WORKLOADS:
        out = await kubectl(cp, f"logs --namespace {NAMESPACE} job/{job}")
        if "Hello, world!" not in out:
            raise MachineError(f"[cp] {job} completed but said nothing:\n{out}")
    print(f"[nixkube] {', '.join(WORKLOADS)} ran out of the volume", flush=True)


async def probe(
    cp: Machine,
    settings: dict,
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
    for want, key in PROBES:
        if want not in wants:
            continue
        # jq, then kubectl reading stdin: the agent's shell is /bin/sh.
        edits = ["."]
        if runtime_class is not None:
            edits.append(f'.spec.template.spec.runtimeClassName = "{runtime_class}"')
        if not host_users:
            edits.append(".spec.template.spec.hostUsers = false")
        created = await cp.succeed(
            f"jq '{' | '.join(edits)}' {settings[key]}"
            f" | kubectl create --namespace {NAMESPACE} --filename - --output name",
            timeout=APPLY_TIMEOUT,
        )
        name = created.strip().splitlines()[-1].split("/")[-1]

        async def done(name: str = name, want: str = want) -> tuple[bool, str]:
            data = await get_json(cp, f"get job {name} --namespace {NAMESPACE}")
            status = data.get("status", {})
            if status.get("failed"):
                pods = await kubectl(
                    cp,
                    f"get pods --namespace {NAMESPACE}"
                    f" --selector job-name={name} --output wide --no-headers",
                )
                raise MachineError(
                    f"[cp] a {want} probe failed after {why}. A pod that"
                    f" cannot start is the whole question here.\n{pods}"
                )
            pods = await kubectl(
                cp,
                f"get pods --namespace {NAMESPACE}"
                f" --selector job-name={name} --no-headers",
            )
            return bool(status.get("succeeded")), f"{name}: {pod_summary(pods)}"

        await until(f"a {want} probe after {why}", done, READY_TIMEOUT, cp)

        csi = await kubectl(
            cp, f"logs --namespace {NAMESPACE} job/{name} --container csi"
        )
        if "Hello, world!" not in csi:
            raise MachineError(
                f"[cp] the {want} probe's csi container said nothing after"
                f" {why}, so the volume is not what the pod asked for:\n{csi}"
            )

        nri = await kubectl(
            cp, f"logs --namespace {NAMESPACE} job/{name} --container nri"
        )
        mount = nix_mount(nri)
        if mount is None:
            raise MachineError(
                f"[cp] the {want} probe has no mount at /nix after {why}, so"
                f" the NRI plugin did not touch it. Its mount table was:\n{nri}"
            )
        options, fstype = mount
        # The API server drops `hostUsers` when its feature gate is off, and
        # the pod then passes on the host's ids. Asked of the container.
        if own_user_namespace(nri) == host_users:
            raise MachineError(
                f"[cp] the {want} probe asked for hostUsers={host_users} after"
                f" {why}, and its uid map says otherwise:\n{nri}"
            )
        if want not in options.split(","):
            raise MachineError(
                f"[cp] the {want} probe wanted a {want} /nix after {why} and"
                f" got `{options}` ({fstype})."
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

        await kubectl(
            cp,
            f"delete job {name} --namespace {NAMESPACE} --wait=true",
            timeout=APPLY_TIMEOUT,
        )
        under = "" if runtime_class is None else f" under {runtime_class}"
        shown = "composefs" if composefs else "not composefs"
        print(
            f"[nixkube] a {want} probe got both mounts{under} after {why}:"
            f" /nix {options} ({fstype}, {shown})",
            flush=True,
        )


# What `nri/server.py` says when a runtime keeps the container out of reach.
UNREACHABLE = "rootfs is not reachable through pid"


async def run_job(
    cp: Machine, source: str, jq_filter: str, what: str
) -> tuple[dict, str]:
    """Create a Job from *source* through *jq_filter*; wait until it ends.

    Returns its pod as JSON and its logs. The pod ends either way here, so
    nothing is raised on failure: the caller says which outcome it wanted.
    """
    created = await cp.succeed(
        f"jq '{jq_filter}' {source}"
        f" | kubectl create --namespace {NAMESPACE} --filename - --output name",
        timeout=APPLY_TIMEOUT,
    )
    name = created.strip().splitlines()[-1].split("/")[-1]
    selector = f"--namespace {NAMESPACE} --selector job-name={name}"

    async def ended() -> tuple[bool, str]:
        pods = (await get_json(cp, f"get pods {selector}")).get("items", [])
        statuses = [
            s for p in pods for s in p.get("status", {}).get("containerStatuses", [])
        ]
        done = bool(statuses) and all(
            "terminated" in s.get("state", {})
            or s.get("state", {}).get("waiting", {}).get("reason")
            in (
                "StartError",
                "RunContainerError",
                "CreateContainerError",
                "CrashLoopBackOff",
            )
            or "terminated" in s.get("lastState", {})
            for s in statuses
        )
        return done, pod_summary(await kubectl(cp, f"get pods {selector} --no-headers"))

    await until(what, ended, READY_TIMEOUT, cp)
    pod = (await get_json(cp, f"get pods {selector}"))["items"][0]
    _, logs = await cp.execute(f"kubectl logs {selector} --all-containers")
    await kubectl(cp, f"delete job {name} --namespace {NAMESPACE} --wait=true")
    return pod, logs


async def check_sandboxed(cp: Machine, settings: dict, runtime_class: str) -> None:
    """Under a sandboxed runtime: CSI works, and NRI refuses by name.

    Measured under gVisor (issue #74): a CSI volume reaches the sandbox
    through its gofer, and NRI cannot, because the pid the hook reports is
    the sandbox's and its root is not the host's.
    """
    csi = (
        f'.spec.template.spec.runtimeClassName = "{runtime_class}"'
        ' | .spec.template.metadata.annotations = {"nixkube/pod-exclude": "true"}'
        ' | .spec.template.spec.containers |= map(select(.name == "csi")'
        f' | .command = ["{settings["workloadStorePath"]}/bin/hello"]'
        ' | .volumeMounts = [{"name": "store", "mountPath": "/nix", "subPath": "nix"}])'
        f' | .metadata.generateName = "csi-{runtime_class}-"'
    )
    _, logs = await run_job(
        cp, settings["probeRo"], csi, f"a CSI pod under {runtime_class}"
    )
    if "Hello, world!" not in logs:
        raise MachineError(
            f"[cp] a CSI volume at /nix did not run hello under {runtime_class}:\n{logs}"
        )
    print(f"[nixkube] a CSI volume works under {runtime_class}", flush=True)

    nri = (
        f'.spec.template.spec.runtimeClassName = "{runtime_class}"'
        f' | .metadata.generateName = "nri-{runtime_class}-"'
    )
    pod, _ = await run_job(
        cp, settings["probeRo"], nri, f"an NRI pod under {runtime_class}"
    )
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


async def check_vm(cp: Machine, settings: dict, runtime_class: str) -> None:
    """Under a VM runtime: a read-only /nix through virtio-fs, and no rw.

    The ro probe asks what it asks under runc: both mounts, and a mount at
    exactly /nix. The rw probe must fail in its nri container with the
    reason, which the waiter prints before it exits.
    """
    await probe(
        cp, settings, "a clean start", runtime_class=runtime_class, wants=("ro",)
    )
    rw = (
        f'.spec.template.spec.runtimeClassName = "{runtime_class}"'
        f' | .metadata.generateName = "rw-{runtime_class}-"'
    )
    _, logs = await run_job(
        cp, settings["probeRw"], rw, f"an rw probe under {runtime_class}"
    )
    if VM_RW_REFUSED not in logs:
        raise MachineError(
            f"[cp] an rw probe under {runtime_class} was not refused with the"
            f" reason ({VM_RW_REFUSED!r}); its containers said:\n{logs}"
        )
    print(
        f"[nixkube] NRI refuses a writable /nix under {runtime_class} and says why",
        flush=True,
    )


def state_dirs(settings: dict) -> dict[str, str]:
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


# Everything the driver holds that the node no longer has. Empty is the pass.
#
# Not "the directories are empty". With NRI on, every container whose command
# names a store path gets an entry, and the control plane's own static pods
# are exactly that -- etcd, the API server, kube-proxy all run out of the
# store. The invariant is instead the one the driver's own garbage collection
# claims to keep: an entry for a container the CRI no longer lists, or a
# volume kubelet no longer records, is a leak.
def reconciled(settings: dict) -> str:
    """Everything the driver holds that the node no longer has.

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


async def check_reconciled(cp: Machine, settings: dict, what: str) -> None:
    """Nothing the driver kept outlives what the node still has."""

    async def clean():
        _, out = await cp.execute(reconciled(settings), timeout=120)
        left = [line.strip() for line in out.splitlines() if line.strip()]
        return not left, f"{len(left)} stale -- " + "; ".join(left[:6])

    await until(f"the node's state to settle after {what}", clean, READY_TIMEOUT, cp)
    print(f"[nixkube] nothing stale on the node after {what}", flush=True)


async def on_node(cp: Machine, command: str, timeout: int = 120) -> tuple[int, str]:
    """Do something to the node, and say what came of it.

    `execute` rather than `succeed`: several of these are expected to return
    non-zero, and the interesting failures are the ones that come afterwards.
    """
    rc, out = await cp.execute(command, timeout=timeout)
    print(f"[nixkube] $ {command}\n  rc={rc} {out.strip()[:300]}", flush=True)
    return rc, out


NODE_SELECTOR = "--selector app.kubernetes.io/component=node"

# The container id and the sandbox id of the running driver, for the tools
# that work below Kubernetes.
NODE_CONTAINER = "$(crictl ps --quiet --name '^nix-node$')"
NODE_SANDBOX = "$(crictl pods --quiet --name '^nix-node-')"


async def kubernetes_deletes_the_pod(cp: Machine, settings: dict) -> None:
    await kubectl(
        cp, f"delete pod --namespace {NAMESPACE} {NODE_SELECTOR} --wait=false"
    )


async def crictl_stops_the_container(cp: Machine, settings: dict) -> None:
    await on_node(cp, f"crictl stop {NODE_CONTAINER}")


async def crictl_removes_the_container(cp: Machine, settings: dict) -> None:
    await on_node(cp, f"crictl rm --force {NODE_CONTAINER}")


async def crictl_removes_the_sandbox(cp: Machine, settings: dict) -> None:
    await on_node(cp, f"crictl rmp --force {NODE_SANDBOX}")


async def the_process_is_killed(cp: Machine, settings: dict) -> None:
    # By pid from the CRI, not by name. The guest is the node, so a pattern
    # match would find the test's own tooling as readily as the driver.
    await on_node(
        cp,
        f"kill -9 $(crictl inspect {NODE_CONTAINER} | jq --raw-output .info.pid)",
    )


async def kubelet_restarts(cp: Machine, settings: dict) -> None:
    await on_node(cp, "systemctl restart kubelet")


# containerd or crio: the unit `services.vivarium-k8s.cri` puts behind this target.
RUNTIME_UNIT = "$(systemctl show --property Requires --value vivarium-k8s-cri.target)"


async def the_runtime_restarts(cp: Machine, settings: dict) -> None:
    # The big one. Every container on the node dies, including the control
    # plane, and the NRI connection the plugin holds goes with it. A
    # runtime upgrade does exactly this.
    await on_node(cp, f"systemctl restart {RUNTIME_UNIT}")


async def the_csi_socket_is_deleted(cp: Machine, settings: dict) -> None:
    await on_node(cp, "rm -f /var/lib/kubelet/plugins/nixkube/csi.sock")


async def the_state_is_wiped(cp: Machine, settings: dict) -> None:
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

SCENARIOS = (
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


async def wait_for_apiserver(cp: Machine) -> None:
    """The API server answers again.

    `kubectl` through `succeed` raises, and half of these scenarios take the
    control plane down with them, so the recovery wait cannot use it.
    """

    async def up():
        rc, out = await cp.execute("kubectl get --raw /healthz", timeout=60)
        return rc == 0, out.strip()[:80] or f"rc={rc}"

    await until("the api server to answer", up, READY_TIMEOUT, cp)


async def break_and_recover(cp: Machine, settings: dict, name: str) -> None:
    """Break the driver one way, and ask if a pod can still start.

    nixkube is not a component a node can do without: a pod that wants a
    store path does not start until the driver serves it. So the question
    after each of these is never "did it come back" alone -- it is whether a
    *new* pod gets both mounts, and whether the driver left anything behind.

    One scenario per call: `nix/uml/chaos/` parametrizes over SCENARIOS, so
    each is a test of its own and `-k` selects one.
    """
    action, resident = next((a, r) for n, a, r in SCENARIOS if n == name)
    await action(cp, settings)
    await wait_for_apiserver(cp)
    await wait_for_driver(cp, settings)
    await probe(cp, settings, name)
    if resident is REPLACED:
        # `--wait` by default, so the old pod is gone before
        # `check_resident` counts pods and finds the new one.
        await kubectl(cp, f"delete pod --namespace {NAMESPACE} {RESIDENT}")
    await check_resident(cp, settings, name)
    await check_reconciled(cp, settings, name)


RESIDENT = "--selector app.kubernetes.io/component=uml-resident"


async def check_resident(cp: Machine, settings: dict, why: str) -> None:
    """The pod that was already running still has what it was given.

    A driver that comes back but leaves the pods it was serving without
    their mounts has not recovered, it has restarted. A fresh probe cannot
    see that -- it gets its mounts from the driver that is up now.

    So this asks the pod itself. `kubectl exec` runs in the container's own
    mount namespace, so the mount table it prints is that container's, and
    the binary it runs only resolves through the CSI volume.
    """

    async def up():
        out = await kubectl(
            cp, f"get pods --namespace {NAMESPACE} {RESIDENT} --no-headers"
        )
        rows = [line.split() for line in out.splitlines() if line.split()]
        running = [
            r for r in rows if len(r) > 2 and r[2] == "Running" and r[1] == "1/1"
        ]
        return len(running) == 1, pod_summary(out)

    await until(f"the resident pod after {why}", up, READY_TIMEOUT, cp)

    name = (
        await kubectl(
            cp,
            f"get pods --namespace {NAMESPACE} {RESIDENT}"
            " --output jsonpath={.items[0].metadata.name}",
        )
    ).strip()

    hello = await kubectl(
        cp, f"exec --namespace {NAMESPACE} {name} -- {settings['residentHello']}"
    )
    if "Hello, world!" not in hello:
        raise MachineError(
            f"[cp] the resident pod lost its volume after {why}: its binary"
            f" under /mnt/csi said\n{hello}"
        )

    mounts = await kubectl(
        cp,
        f"exec --namespace {NAMESPACE} {name} --"
        f" {settings['residentCat']} /proc/self/mountinfo",
    )
    if nix_mount(mounts) is None:
        raise MachineError(
            f"[cp] the resident pod lost its /nix after {why}. Its mount"
            f" table was:\n{mounts}"
        )
    print(f"[nixkube] {name} still has both mounts after {why}", flush=True)


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
        found[point] = (
            "composefs" if fstype == "overlay" and "::" in options else "hardlinks"
        )
    return found


async def check_presentation(cp: Machine, settings: dict) -> None:
    """Every CSI volume on the node is presented the way the deployment
    asked. Without this a node that falls back to hardlinks -- a probe that
    fails, a kernel without EROFS -- passes every other check. Issue #68."""
    wanted = "composefs" if settings["csiComposefs"] else "hardlinks"
    seen = csi_presentations(await cp.succeed("cat /proc/1/mountinfo"))
    if not seen:
        raise MachineError("[cp] no CSI volume is mounted on the node to look at")
    wrong = {p: kind for p, kind in seen.items() if kind != wanted}
    if wrong:
        raise MachineError(f"[cp] CSI volumes not presented as {wanted}: {wrong}")
    print(f"[nixkube] {len(seen)} CSI volumes presented as {wanted}", flush=True)


async def check_unmount(cp: Machine, settings: dict) -> None:
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
    await kubectl(
        cp,
        f"delete job --namespace {NAMESPACE} {' '.join(WORKLOADS)} --wait=true",
        timeout=APPLY_TIMEOUT,
    )
    await check_reconciled(cp, settings, "the workload jobs are deleted")


async def report(cp: Machine) -> None:
    """What the driver's own containers said, pass or fail.

    This runs for an hour on a builder. A log that says only "passed" makes
    the next failure start from nothing.
    """
    for what in (
        f"get all --namespace {NAMESPACE}",
        f"get events --namespace {NAMESPACE} --sort-by=.lastTimestamp",
    ):
        rc, out = await cp.execute(f"kubectl {what}", timeout=120)
        print(f"[nixkube] kubectl {what} (rc={rc}):\n{out}", flush=True)

    # What the node container's /nix is made of, from the node's side.
    #
    # The init container writes an out-link at /nix/var/result and the node
    # container's PATH is /nix/var/result/bin. When that entrypoint does not
    # resolve, runc says only that it is "not found", so these three answer
    # the question runc will not: is the link there, does it point at
    # something, and did kubelet's subPath bind carry the store.
    rc, out = await cp.execute(
        "ls -l /nix/var/result;"
        " ls /nix/var/result/bin 2>&1 | head -n 5;"
        " findmnt --output TARGET,SOURCE,FSTYPE | grep -i nix;"
        " ls -d /var/lib/kubelet/pods/*/volume-subpaths/*/*/* 2>&1 | head -n 10",
        timeout=120,
    )
    print(f"[nixkube] the node's /nix (rc={rc}):\n{out}", flush=True)

    # What the NRI sweep did, and why a volume outlived its container: a
    # sweep that skipped it, failed on it, or never ran.
    rc, out = await cp.execute(
        f"kubectl logs --namespace {NAMESPACE} --selector app.kubernetes.io/component=node"
        " --container nix-node --tail 5000 --timestamps"
        " | grep --text -E '\"(gc_[a-z_]+|synchronize)\"|REMOVE_CONTAINER' | tail -n 80",
        timeout=120,
    )
    print(f"[nixkube] the NRI sweep (rc={rc}):\n{out}", flush=True)

    # What the nri-wait hook said before it gave up.
    #
    # The hook runs at createRuntime, so its stderr goes into runc's error,
    # into containerd's, and from there into a Kubernetes event. kubelet
    # cuts that event's message at about 1KB, which lands mid-sentence and
    # drops the one line that says which deadline the hook hit. containerd's
    # own journal keeps the whole thing. Measured: the event ended at
    # "[nri-wait] OCI state: id=hello pid=2081 bundle=..." and the verdict
    # was 900 characters further on.
    rc, out = await cp.execute(
        f"journalctl --unit {RUNTIME_UNIT} --no-pager --lines 4000"
        " | grep --text nri-wait | tail -n 20",
        timeout=120,
    )
    print(f"[nixkube] what nri-wait said (rc={rc}):\n{out}", flush=True)

    # Every container of the node's and pynixd's pods, by name, from the API
    # server.
    #
    # `diagnose` tails the log files off the node, which works but competes
    # for a line budget with everything else running. Asking kubelet for one
    # container's output is exact, and `--previous` is the only way to see a
    # container that has already exited -- which a crash-looping init
    # container always has by the time anybody looks.
    for component, containers in (
        ("node", ("appstarter-init", "nix-node")),
        ("pynixd", ("appstarter-init", "pynixd")),
    ):
        pods = await cp.execute(
            f"kubectl get pods --namespace {NAMESPACE}"
            f" --selector app.kubernetes.io/component={component}"
            " --output jsonpath={.items[*].metadata.name}",
            timeout=120,
        )
        for pod in pods[1].split():
            for container in containers:
                for flags in ("", " --previous"):
                    rc, out = await cp.execute(
                        f"kubectl logs --namespace {NAMESPACE} {pod}"
                        f" --container {container}{flags} --tail 60",
                        timeout=120,
                    )
                    if rc == 0 and out.strip():
                        print(
                            f"[nixkube] logs {pod} {container}{flags}:\n{out}",
                            flush=True,
                        )

    # What the workloads said. A job that fails here fails for one of two
    # reasons -- the driver did not mount, or the thing it mounted is not
    # what the pod asked for -- and the container's own output separates
    # them.
    for job in WORKLOADS:
        rc, out = await cp.execute(
            f"kubectl logs --namespace {NAMESPACE} job/{job} --tail 40",
            timeout=120,
        )
        print(f"[nixkube] logs job/{job} (rc={rc}):\n{out}", flush=True)
