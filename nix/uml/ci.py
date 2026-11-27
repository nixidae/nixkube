"""The kind jobs, on a guest.

Deploy nixkube with the same `kubenixDeploy` CI runs, deploy the test
workloads with the same one, wait for the jobs, then delete them and check
the CSI cleanup -- the steps of `test-kind-cache` and `test-kind-nocache`,
in order.

kluctl runs here, on the host, against the guest's API server through a
passt forward. That is where kind runs it too: on the runner, not inside a
node. It also keeps `kubenixCI1`'s `cachix push` pre-deploy step working,
because the token is in the host's environment and not the guest's.

Outside the build sandbox only -- the node pulls from registry.k8s.io and
what it deploys comes from ghcr.io:

    nix run --file . internal.ciTest.driver -- --out ./o
"""

import json
import os
import subprocess
import tempfile

import kr8s.asyncio
from kr8s.asyncio.objects import DaemonSet, Event, Job, Node, PersistentVolumeClaim, Pod
from nixkube_uml import NAMESPACE, cluster, delete, listed, logs, pod_ready, pod_state, pod_summary, raw, rolled_out
from vivarium_k8s import KUBE_DNS, KUBE_PROXY, bring_up, until
from vivarium_runner import Machine, Machines

# kubeadm gives the API server a certificate for the addresses the node
# has. The host reaches it on a 127.0.0.x the runner picks when the guest
# boots, which is not knowable when the certificate is made -- so verify
# nothing. The connection is loopback on this machine either way.
INSECURE = True


async def host_kubeconfig(cp: Machine) -> str:
    """The guest's admin kubeconfig, pointed at where the host can reach it.

    Rewritten by the guest's own kubectl rather than by parsing YAML here:
    a test's interpreter carries the runner and nothing else, and kubectl
    is on every one of these nodes by definition.
    """
    reachable = cp.reachable(6443)
    assert reachable, (
        "6443 is not forwarded, so the host cannot reach the API server -- see vivarium.inbound in nix/uml/ci.nix"
    )
    # kubeadm always names it `kubernetes`, but ask rather than assume.
    cluster = (await cp.succeed("kubectl config view --output jsonpath='{.clusters[0].name}'")).stdout.strip()
    conf = "/tmp/host-kubeconfig"
    await cp.succeed(f"cp /etc/kubernetes/admin.conf {conf}")
    await cp.succeed(f"kubectl --kubeconfig {conf} config set-cluster {cluster} --server=https://{reachable[0]}")
    if INSECURE:
        await cp.succeed(f"kubectl --kubeconfig {conf} config unset clusters.{cluster}.certificate-authority-data")
        await cp.succeed(f"kubectl --kubeconfig {conf} config set clusters.{cluster}.insecure-skip-tls-verify true")
    return (await cp.succeed(f"cat {conf}")).stdout


def write(path: str, text: str) -> None:
    """Here rather than inline, because the caller is a coroutine.

    A blocking `open` in an async function stalls the event loop, which is
    what runs the other guests -- and ruff refuses one (ASYNC230).
    """
    with open(path, "w") as handle:
        handle.write(text)


def run(script: str, kubeconfig: str, *args: str) -> None:
    """One of CI's deployment scripts, run the way CI runs it."""
    env = dict(os.environ, KUBECONFIG=kubeconfig)
    done = subprocess.run(
        [f"{script}/bin/kubenixDeploy", *args],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    if done.returncode != 0:
        raise AssertionError(
            f"{script}/bin/kubenixDeploy exited {done.returncode}\n"
            f"--- stdout ---\n{done.stdout[-4000:]}\n"
            f"--- stderr ---\n{done.stderr[-4000:]}"
        )


async def check_store_split(api: kr8s.asyncio.Api, cp: Machine, nixos: bool) -> None:
    """With `nixkube.hostStore.enable`, the node's OS picks its DaemonSet."""
    node = raw(await Node.get("cp", api=api))
    label = node["metadata"].get("labels", {}).get("nixkube/host")
    assert label == ("nixos" if nixos else None), (
        f"the node's nixkube/host label is {label!r} on a {'NixOS' if nixos else 'non-NixOS'} node"
    )
    serving = "nix-node-host" if nixos else "nix-node-separate"
    idle = "nix-node-separate" if nixos else "nix-node-host"

    # Waited for, not read once: the node moves only after the other
    # DaemonSet's pod has relabelled it, so `rollout status` can pass while
    # the serving pod is seconds old. Measured: 0.4 s, and the check failed.
    async def settled() -> tuple[bool, list[str]]:
        seen = []
        for name, want in ((serving, 1), (idle, 0)):
            status = raw(await DaemonSet.get(name, namespace=NAMESPACE, api=api))["status"]
            ready = status.get("numberReady", 0)
            scheduled = status.get("desiredNumberScheduled", 0)
            seen.append(f"{name} has {ready} ready of {scheduled} scheduled, not {want}")
            if (ready, scheduled) != (want, want):
                return False, seen
        return True, seen

    await until(f"{serving} to serve the node", settled, 300, cp)
    print(f"[test] {serving} serves the node, {idle} schedules nothing")
    if nixos:
        # ./ci.nix leaves the guest's own daemon no substituters, so every
        # path the workloads get comes through nixkube's own daemon.
        nix = "nix --extra-experimental-features nix-command"
        subs = (await cp.succeed(f"{nix} config show substituters")).stdout.strip()
        assert not subs, f"the guest's daemon substitutes from {subs!r}, which hides who fetched"
        own = "/nix/var/nix-csi/daemon-socket/socket"
        await cp.succeed(f"test -S {own} && {nix} store info --store unix://{own}")
        print(f"[test] the guest's daemon substitutes from nowhere; nixkube's serves {own}")


def log_events(text: str) -> list[dict[str, object]]:
    """The structured events in a container's log. nixkube and pynixd both
    write one JSON object per line; anything else is not an event."""
    events = []
    for line in text.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict):
            events.append(event)
    return events


def daemon_switches(state: dict[str, object]) -> dict[str, object]:
    """The local store's daemon experiments in a `pynixd state` report:
    `daemon` and `pynixd_writes`, each only when the store turned it on."""
    experiments = state.get("experiments")
    assert isinstance(experiments, list), f"pynixd state has no experiments section: {state}"
    return {
        str(e["switch"]): e["value"]
        for e in experiments
        if isinstance(e, dict) and e.get("store") == "local" and e.get("switch") in ("daemon", "pynixd_writes")
    }


async def check_store_server(api: kr8s.asyncio.Api, server: str) -> None:
    """The server `nixkube.node.storeServer` names is the one that answers.

    Two witnesses. nixkube's supervisor logs the server it started. pynixd
    itself answers its own operation (`PynixdState`) on the node's socket,
    which a Nix daemon refuses, and says whether a Nix daemon runs under it.
    """
    pods = await listed(Pod, api, namespace=NAMESPACE, label_selector={"app.kubernetes.io/component": "node"})
    serving = [p for p in pods if pod_state(raw(p)) == "Running"]
    assert len(serving) == 1, f"one node pod should serve the node: {pod_summary(pods)}"
    pod = serving[0]
    events = log_events(await logs(pod, container="nix-node"))
    started = {str(e.get("server")) for e in events if e.get("event") == "store_server_starting"}
    assert started == {server}, f"{pod.name} started {sorted(started) or 'no store server'}, not {server}"

    if server != "nix-daemon":
        env = {
            e["name"]: e.get("value")
            for c in raw(pod)["spec"]["containers"]
            if c["name"] == "nix-node"
            for e in c.get("env", [])
        }
        socket = env["PYNIXD_UNIX_PATH"]
        done = await pod.exec(
            ["pynixd", "state", "--section", "stores", "--section", "experiments", "--json", "--socket", socket],
            container="nix-node",
        )
        state = json.loads(done.stdout.decode())
        stores = state.get("stores")
        local = stores.get("local") if isinstance(stores, dict) else None
        assert isinstance(local, dict) and local.get("healthy"), (
            f"pynixd on {socket} has no healthy local store: {stores}"
        )
        want = {"daemon": False, "pynixd_writes": True} if server == "pynixd-daemonless" else {}
        switches = daemon_switches(state)
        assert switches == want, f"pynixd on {socket} runs its local store with {switches}, not {want}"
    print(f"[test] {server} serves the node's store, in {pod.name}")


def node_line(node: Node) -> str:
    conditions = raw(node).get("status", {}).get("conditions", [])
    ready = any(c["type"] == "Ready" and c["status"] == "True" for c in conditions)
    return f"{node.name} {'Ready' if ready else 'NotReady'}"


async def wait_for_pods(api: kr8s.asyncio.Api, cp: Machine, selector: dict[str, str]) -> None:
    """Every pod the selector matches runs, and is ready."""

    def up(pod: Pod) -> bool:
        ready, total = pod_ready(raw(pod)).split("/")
        return pod_state(raw(pod)) == "Running" and ready == total

    async def ready() -> tuple[bool, str]:
        pods = await listed(Pod, api, namespace=NAMESPACE, label_selector=selector)
        return bool(pods) and all(up(p) for p in pods), pod_summary(pods)

    await until(f"pods {selector}", ready, 300, cp)


async def succeeded(api: kr8s.asyncio.Api) -> set[str]:
    return {j.name for j in await listed(Job, api, namespace=NAMESPACE) if raw(j).get("status", {}).get("succeeded")}


async def job_table(api: kr8s.asyncio.Api) -> str:
    return "\n".join(
        f"{j.name} succeeded={raw(j).get('status', {}).get('succeeded', 0)} failed={raw(j).get('status', {}).get('failed', 0)}"
        for j in await listed(Job, api, namespace=NAMESPACE)
    )


async def test(vms: Machines) -> None:
    settings = vms.settings
    cp = await bring_up(vms, addons=(KUBE_PROXY, KUBE_DNS))
    api = await cluster(vms)

    with tempfile.TemporaryDirectory(prefix="nixkube-ci-") as work:
        kubeconfig = os.path.join(work, "kubeconfig")
        write(kubeconfig, await host_kubeconfig(cp))
        print(f"[test] the host reaches the API server on {cp.reachable(6443)[0]}")
        print("\n".join(node_line(n) for n in await listed(Node, api)))

        if settings["foreignHost"]:
            await cp.succeed("rm /etc/NIXOS")
            print("[test] /etc/NIXOS removed: the node is not NixOS to nixkube")

        run(settings["deploy"], kubeconfig, "--yes")
        print("[test] nixkube deployed through kubenixDeploy")

        async def node_rolled_out() -> tuple[bool, str]:
            daemonsets = await listed(
                DaemonSet, api, namespace=NAMESPACE, label_selector={"app.kubernetes.io/component": "node"}
            )
            seen = "; ".join(f"{d.name} {raw(d).get('status', {})}" for d in daemonsets)
            return bool(daemonsets) and all(rolled_out(raw(d)) for d in daemonsets), seen or "(no DaemonSet yet)"

        await until("the node DaemonSet", node_rolled_out, 300, cp)
        print("[test] the node DaemonSet rolled out")

        if settings["hostStore"]:
            await check_store_split(api, cp, nixos=not settings["foreignHost"])

        if settings["pynixd"]:
            # The `Wait for nix-csi cache pod` step of `test-kind-cache`.
            #
            # pynixd is a StatefulSet whose claim names no StorageClass, so
            # it only runs on a cluster with a default one -- see
            # `services.vivarium-k8s.persistentVolumes` in nix/uml/ci.nix. The
            # claim is reported beside the pod because a Pending pod and an
            # unbound claim look the same from here.
            await wait_for_pods(api, cp, {"app.kubernetes.io/component": "pynixd"})
            claims = ", ".join(
                f"{c.name} {raw(c).get('status', {}).get('phase')} {raw(c)['spec'].get('volumeName')}"
                for c in await listed(PersistentVolumeClaim, api, namespace=NAMESPACE)
            )
            print(f"[test] the pynixd cache is ready, on {claims}")

        run(settings["deployWorkloads"], kubeconfig, "--yes")
        print("[test] the test workloads are deployed")

        asserted = settings["assertedJobs"]

        async def complete() -> tuple[bool, str]:
            done = await succeeded(api)
            missing = [job for job in asserted if job not in done]
            return (
                not missing,
                f"still waiting on {', '.join(missing)}" if missing else "all",
            )

        await until(f"{len(asserted)} jobs to complete", complete, 600, cp)
        print(f"[test] {', '.join(asserted)} all completed")

        # The driver reports each publish on the pod it served. A job can
        # complete with its store from somewhere else, and then there is
        # no such event. `report_event` prefixes every reason with `Nix`,
        # so the code's "VolumeMount" is NixVolumeMount on the cluster.
        mounts = await listed(Event, api, namespace=NAMESPACE, field_selector={"reason": "NixVolumeMount"})
        assert mounts, "the jobs completed and the CSI driver reported no NixVolumeMount"
        print(f"[test] the CSI driver reported {len(mounts)} NixVolumeMount events")

        await check_store_server(api, settings["storeServer"])

        # And the ones that must not finish -- see ci/test-jobs.nix.
        #
        # Each asks the driver for something that cannot be built, so a
        # success here is a driver that mounted the wrong thing quietly.
        #
        # Asked once, after the others are done, rather than waited for:
        # "has not succeeded" is true of a Job that failed and of one that
        # is still trying, so there is nothing to poll. It can miss a
        # success that arrives later; it cannot report one that never came.
        rejected = settings["rejectedJobs"]
        wrong = sorted(await succeeded(api) & set(rejected))
        assert not wrong, (
            f"{', '.join(wrong)} succeeded, and each of them asks for"
            " something that cannot be built:\n" + await job_table(api)
        )
        print(f"[test] {', '.join(rejected)} were all refused")

        for name in settings["deployedJobs"]:
            await delete(await Job.get(name, namespace=NAMESPACE, api=api), cp)
        selector = f"job-name in ({','.join(settings['deployedJobs'])})"

        async def pods_gone() -> tuple[bool, str]:
            pods = await listed(Pod, api, namespace=NAMESPACE, label_selector=selector)
            return not pods, pod_summary(pods)

        await until("the jobs' pods to go", pods_gone, 120, cp)
        print("[test] the jobs are gone and their pods with them")

        # What `vivarium.diskSize` has to cover, measured rather than
        # guessed: the images this node pulled, every store path the jobs
        # asked for, and pynixd's volume when there is one. A runner has
        # 14 GB for the whole guest.
        print(
            "[test] node disk\n"
            + (await cp.succeed("df -h /; du -sh /var/lib/vivarium-storage 2>/dev/null || true")).stdout
        )
