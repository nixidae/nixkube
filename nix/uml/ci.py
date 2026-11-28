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
from datetime import UTC, datetime

import kr8s.asyncio
from kr8s.asyncio.objects import DaemonSet, Event, Job, Node, PersistentVolumeClaim, Pod
from nixkube_uml import (
    NAMESPACE,
    Manifest,
    cluster,
    delete,
    listed,
    logs,
    pod_ready,
    pod_state,
    pod_summary,
    raw,
    rolled_out,
)
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


async def check_store_outage(api: kr8s.asyncio.Api, cp: Machine, template: Manifest) -> None:
    """A store server that does not answer costs the calls made meanwhile, not every call after.

    The socket moves away while its server runs, so each connect fails for
    as long as the test wants. One huggorm store per process refused every
    call after the first failure ("previously failed", nixidae/nixidae#1670).
    """
    pods = await listed(Pod, api, namespace=NAMESPACE, label_selector={"app.kubernetes.io/component": "node"})
    [pod] = [p for p in pods if pod_state(raw(p)) == "Running"]
    env = {
        e["name"]: e.get("value")
        for c in raw(pod)["spec"]["containers"]
        if c["name"] == "nix-node"
        for e in c.get("env", [])
    }
    socket = env.get("PYNIXD_UNIX_PATH") or env.get("NIX_DAEMON_SOCKET_PATH") or "/nix/var/nix/daemon-socket/socket"

    async def sh(command: str) -> str:
        return (await pod.exec(["sh", "-c", command], container="nix-node")).stdout.decode()

    readyz = f"curl -s http://127.0.0.1:{env['READINESS_PORT']}/readyz"
    await sh(f"mv {socket} {socket}.away")
    try:
        for _ in range(2):
            body = await sh(readyz)
            assert "store: " in body and "cannot connect" in body, f"with the socket away, /readyz said {body!r}"
    finally:
        await sh(f"mv {socket}.away {socket}")

    async def store_answers() -> tuple[bool, str]:
        body = await sh(readyz)
        return "store: " not in body, body

    await until("/readyz to find the store again", store_answers, 60, cp)

    job = Job(
        {
            "apiVersion": "batch/v1",
            "kind": "Job",
            "metadata": {"name": "after-outage", "namespace": NAMESPACE},
            "spec": {"backoffLimit": 0, "template": again(template)},
        },
        api=api,
    )
    await job.create()

    async def complete() -> tuple[bool, str]:
        return "after-outage" in await succeeded(api), await job_table(api)

    await until("a publish after the outage", complete, 300, cp)
    await delete(job, cp)
    print(f"[test] with {socket} away, /readyz named the store; back, it answered, and a publish mounted")


# A nodeEnv no substituter has, so `appstarter init` seeds the image's copy.
UNFETCHABLE = "/nix/store/" + "0" * 32 + "-nixkube-node-env-unfetchable"

# The livenessProbe's initialDelaySeconds + failureThreshold x periodSeconds in
# kubenix/daemonset.nix, and a period more: a restart would have come by then.
LIVENESS_WINDOW = 30 + 5 * 10 + 10

NODE_PODS = {"app.kubernetes.io/component": "node"}


def wanted_patch(ds: Manifest) -> list[Manifest]:
    """JSON patch ops that point every `APPSTARTER_WANTED` at `UNFETCHABLE`."""
    spec = ds["spec"]["template"]["spec"]
    return [
        {"op": "replace", "path": f"/spec/template/spec/{kind}/{i}/env/{j}/value", "value": UNFETCHABLE}
        for kind in ("initContainers", "containers")
        for i, container in enumerate(spec.get(kind, []))
        for j, env in enumerate(container.get("env", []))
        if env["name"] == "APPSTARTER_WANTED"
    ]


def container_status(pod: Manifest, name: str) -> Manifest:
    statuses = pod.get("status", {}).get("containerStatuses", [])
    return next((s for s in statuses if s["name"] == name), {})


def running_seconds(status: Manifest) -> float:
    started = status.get("state", {}).get("running", {}).get("startedAt")
    if not started:
        return 0.0
    return (datetime.now(UTC) - datetime.fromisoformat(started)).total_seconds()


def asks_unfetchable(pod: Manifest) -> bool:
    return any(
        e.get("value") == UNFETCHABLE
        for c in pod["spec"]["containers"]
        if c["name"] == "nix-node"
        for e in c.get("env", [])
    )


async def check_behind(api: kr8s.asyncio.Api, cp: Machine) -> None:
    """A node that cannot fetch its nodeEnv serves the image's, and is not Ready.

    nixidae/nixidae#1220: such a node reported Ready, so the RollingUpdate
    replaced every node with one that was behind. It must keep running
    (issues #27, #55) and must not restart: liveness stays on the sidecar's
    /healthz, and only readiness reads nixkube's /readyz.
    """
    daemonsets = await listed(DaemonSet, api, namespace=NAMESPACE, label_selector=NODE_PODS)
    for ds in daemonsets:
        ops = wanted_patch(raw(ds))
        assert len(ops) == 2, f"{ds.name}: APPSTARTER_WANTED on appstarter-init and nix-node, found {ops}"
        await ds.patch(ops, type="json")
    print(f"[test] the node DaemonSet asks for {UNFETCHABLE}")

    async def behind_pod() -> tuple[bool, Pod | str]:
        pods = [
            p for p in await listed(Pod, api, namespace=NAMESPACE, label_selector=NODE_PODS) if asks_unfetchable(raw(p))
        ]
        for pod in pods:
            if not running_seconds(container_status(raw(pod), "nix-node")):
                continue
            events = log_events(await logs(pod, container="nix-node"))
            if any(e.get("event") == "node_env_behind" and e.get("wanted") == UNFETCHABLE for e in events):
                return True, pod
        return False, pod_summary(pods) if pods else "(no pod with the new spec yet)"

    pod = await until("a node pod that says it is behind", behind_pod, 300, cp)
    assert isinstance(pod, Pod), pod
    print(f"[test] {pod.name} logged node_env_behind")

    async def window_passed() -> tuple[bool, str]:
        await pod.refresh()
        status = container_status(raw(pod), "nix-node")
        seconds, restarts = running_seconds(status), status.get("restartCount", 0)
        return seconds > LIVENESS_WINDOW or restarts > 0, f"{restarts} restarts"

    await until("the liveness window", window_passed, 300, cp)
    status = container_status(raw(pod), "nix-node")
    assert status.get("restartCount") == 0, f"{pod.name} restarted while behind: {status}"
    assert status.get("ready") is False, f"{pod.name} is behind and reports Ready: {status}"
    assert pod_state(raw(pod)) == "Running", f"{pod.name} stopped serving: {pod_summary([pod])}"

    for ds in daemonsets:
        await ds.refresh()
    held = [ds.name for ds in daemonsets if not rolled_out(raw(ds))]
    assert held, f"the rollout finished onto a node that is behind: {[raw(d).get('status') for d in daemonsets]}"
    print(f"[test] {pod.name} runs, is not Ready, did not restart; {', '.join(held)} held its rollout")


NODE_SOCKET = "/nix/var/nix-csi/daemon-socket/socket"
COEXIST = "/tmp/coexist"
NIX = "nix --extra-experimental-features nix-command"

# No lock either writer takes lasts past one SQLite transaction or one path's
# `.lock`. A write that waits this long waits on a lock held for the life of a
# process: the big lock, or `gc.lock`.
WRITE_BOUND = 10.0

COEXIST_JOBS = ("path-hello", "env-ssl")


async def timed(cp: Machine, command: str) -> tuple[str, float]:
    """*command*'s stdout and its wall time in the guest, refused past `WRITE_BOUND`."""
    done = await cp.succeed(
        f"start=$(date +%s%N); out=$(timeout {WRITE_BOUND * 3:.0f} {command}); end=$(date +%s%N);"
        ' echo "$out"; echo $(( (end - start) / 1000000 ))'
    )
    *out, millis = done.stdout.strip().splitlines()
    seconds = int(millis) / 1000
    assert seconds < WRITE_BOUND, f"`{command}` took {seconds:.1f} s, past the {WRITE_BOUND:.0f} s bound"
    return "\n".join(out).strip(), seconds


async def garbage(cp: Machine, name: str) -> str:
    """A new path the host's daemon adds and nothing roots."""
    content = os.urandom(8).hex()
    path, _ = await timed(cp, f'{NIX} eval --store daemon --raw --expr \'builtins.toFile "{name}" "{content}"\'')
    dead = (await cp.succeed(f"nix-store --store daemon --gc --print-dead | grep -c -F {path} || true")).stdout
    assert dead.strip() == "1", f"{path} is not garbage to the host's collector before the test holds it"
    return path


async def hold(cp: Machine, unit: str, path: str) -> None:
    """A client session on the node's socket that holds *path* as a temporary root until `release`.

    `builtins.toFile` of the same name and content is an `AddToStore` of a valid
    path, which the server roots for the session (`local-store.cc:1264`). The
    session stays open while `builtins.exec` waits on a FIFO.
    """
    name = path.split("-", 1)[1]
    content = (await cp.succeed(f"cat {path}")).stdout
    nix = (await cp.succeed("command -v nix")).stdout.strip()
    await cp.succeed(f"mkfifo {COEXIST}/{unit}.release")
    await cp.succeed(
        f"cat > {COEXIST}/{unit}.nix <<'EOF'\n"
        f'builtins.seq (builtins.toFile "{name}" "{content}")'
        f' (builtins.exec ["/bin/sh" "-c" "read x < {COEXIST}/{unit}.release; echo null"])\n'
        "EOF"
    )
    await cp.succeed(
        f"systemd-run --unit={unit} --collect --property=StandardOutput=file:{COEXIST}/{unit}.log"
        f" --property=StandardError=file:{COEXIST}/{unit}.log"
        f" {nix} --extra-experimental-features nix-command eval --store unix://{NODE_SOCKET} --impure"
        f" --option allow-unsafe-native-code-during-evaluation true --file {COEXIST}/{unit}.nix"
    )
    # The roots file of the node's server, not the socket's answer: a server writes the file after the
    # collector took the root (`gc.cc:139`), so a root in a file is a root the collector has.
    # A file whose lock nobody holds is stale: the daemon worker that added the path left it.
    await cp.wait_until_succeeds(
        f'for f in /nix/var/nix/temproots/*; do grep -q -a -F {path} "$f" && ! flock -n "$f" true && exit 0; done; exit 1',
        timeout=30,
        poll=0.2,
    )


async def release(cp: Machine, unit: str) -> None:
    await cp.succeed(f"timeout 10 sh -c 'echo go > {COEXIST}/{unit}.release'")
    await cp.wait_until_fails(f"systemctl is-active --quiet {unit}", timeout=30)
    log = (await cp.succeed(f"cat {COEXIST}/{unit}.log")).stdout
    assert log.strip() == "null", f"the session that held a root ended with:\n{log}"


async def valid_to_both(cp: Machine, path: str, valid: bool) -> None:
    """Whether the host's daemon and the node's server each call *path* valid, asked at once."""
    for store in ("daemon", f"unix://{NODE_SOCKET}"):
        rc = (await cp.execute(f"{NIX} path-info --store {store} {path} >/dev/null 2>&1")).returncode
        assert (rc == 0) == valid, f"{store} calls {path} {'invalid' if valid else 'valid'}"
        rc = (await cp.execute(f"nix-store --store {store} --check-validity {path}")).returncode
        assert (rc == 0) == valid, f"{store} --check-validity calls {path} {'invalid' if valid else 'valid'}"
    on_disk = (await cp.execute(f"test -e {path}")).returncode == 0
    assert on_disk == valid, f"{path} {'is not' if valid else 'is still'} on disk"


async def check_writers(cp: Machine) -> None:
    """Each store writer writes while the other runs, and each sees the other's path at once."""
    content = os.urandom(8).hex()
    await cp.succeed(f"echo {content} > {COEXIST}/by-node")
    node_path, node_s = await timed(cp, f"nix-store --store unix://{NODE_SOCKET} --add {COEXIST}/by-node")
    await valid_to_both(cp, node_path, valid=True)
    print(f"[test] the node's server added {node_path} in {node_s:.2f} s; the host's daemon holds it at once")

    content = os.urandom(8).hex()
    await cp.succeed(f"echo {content} > {COEXIST}/by-host")
    # Asked of the node before the host adds it: an answer it kept would now be wrong.
    expected = (await cp.succeed(f"{NIX} store add --dry-run --store daemon {COEXIST}/by-host")).stdout.strip()
    await cp.fail(f"nix-store --store unix://{NODE_SOCKET} --check-validity {expected}")
    host_path, host_s = await timed(cp, f"nix-store --store daemon --add {COEXIST}/by-host")
    assert host_path == expected, f"the host added {host_path}, and add-file named {expected}"
    await valid_to_both(cp, host_path, valid=True)
    print(f"[test] the host's daemon added {host_path} in {host_s:.2f} s; the node's server holds it at once")


async def check_root_file(cp: Machine) -> None:
    """A root the node's server wrote before a collection starts: the host's collector keeps the path."""
    path = await garbage(cp, "coexist-file")
    await hold(cp, "coexist-hold-file", path)
    await cp.succeed(f"nix-store --store daemon --gc > {COEXIST}/gc-file.log 2>&1")
    await valid_to_both(cp, path, valid=True)
    refused = await cp.fail(f"nix-store --store daemon --delete {path}")
    assert "still alive" in refused.stderr, refused.stderr
    print(f"[test] the host's collector kept {path}, held in the node server's roots file")

    await release(cp, "coexist-hold-file")
    await cp.succeed(f"nix-store --store daemon --delete {path}")
    await valid_to_both(cp, path, valid=False)
    print(f"[test] released, the host's collector deleted {path}, and neither store calls it valid")


async def check_root_socket(cp: Machine) -> None:
    """A root the node's server sends while the host's collector runs, after it read the roots files.

    The collector waits at Nix's own synchronisation point for this,
    `_NIX_TEST_GC_SYNC_2` (`gc.cc:537`), in a `--store local` process: the
    variable reaches no daemon. Waiting there it holds `gc.lock`, so the node's
    server cannot take it shared and must use `gc-socket/socket` (`gc.cc:107`).
    """
    path = await garbage(cp, "coexist-socket")
    nix_store = (await cp.succeed("command -v nix-store")).stdout.strip()
    await cp.succeed(f"mkfifo {COEXIST}/gc-sync {COEXIST}/gc-sync.release")
    await cp.succeed(
        f"systemd-run --unit=coexist-gc --collect --setenv=_NIX_TEST_GC_SYNC_2={COEXIST}/gc-sync"
        f" --property=StandardOutput=file:{COEXIST}/gc-socket.log --property=StandardError=file:{COEXIST}/gc-socket.log"
        f" {nix_store} --store local --gc --debug"
    )
    # The open of a FIFO's write end returns once the collector opened its read end. Shell builtins
    # only: a transient unit's PATH holds nothing on NixOS.
    await cp.succeed(
        "systemd-run --unit=coexist-gc-sync --collect /bin/sh -c"
        f" 'exec 3>{COEXIST}/gc-sync; : > {COEXIST}/gc-waits; read x < {COEXIST}/gc-sync.release'"
    )
    await cp.wait_until_succeeds(f"test -e {COEXIST}/gc-waits", timeout=60, poll=0.2)
    await cp.fail("flock --nonblock --shared /nix/var/nix/gc.lock true")
    await hold(cp, "coexist-hold-socket", path)
    await cp.succeed(f"timeout 10 sh -c 'echo go > {COEXIST}/gc-sync.release'")
    await cp.wait_until_fails("systemctl is-active --quiet coexist-gc", timeout=300)
    gc_log = (await cp.succeed(f"cat {COEXIST}/gc-socket.log")).stdout
    assert f"got new GC root '{path}'" in gc_log, f"the collector got no root for {path} over its socket"
    assert f"deleting '{path}'" not in gc_log, f"the collector deleted {path}, which the node's server held"
    await valid_to_both(cp, path, valid=True)
    print(f"[test] the host's collector took {path} over its socket from the node's server, and kept it")

    await release(cp, "coexist-hold-socket")
    await cp.succeed(f"nix-store --store daemon --delete {path}")
    await valid_to_both(cp, path, valid=False)
    print(f"[test] released, the host's collector deleted {path}")


def again(template: Manifest) -> Manifest:
    """A Job's pod template without the labels its controller gave it, for a Job of another name."""
    template = json.loads(json.dumps(template))
    labels = template.get("metadata", {}).get("labels", {})
    for key in [k for k in labels if "controller-uid" in k or k.endswith("job-name")]:
        del labels[key]
    return template


async def check_publish_under_gc(api: kr8s.asyncio.Api, cp: Machine, templates: dict[str, Manifest]) -> None:
    """Pods whose closures the host lacks start while the host's collector runs again and again.

    Each publish substitutes through the node's server, and each collection
    runs beside it. A path the publish needs and holds no root for is a path
    a collection deletes: the publish then fails, and the kubelet reports
    FailedMount before it tries again.
    """
    hello = next(
        str(value)
        for volume in templates["path-hello"]["spec"]["volumes"]
        for value in volume.get("csi", {}).get("volumeAttributes", {}).values()
        if str(value).startswith("/nix/store/")
    )
    # A volume's GC root goes when the driver unpublishes it, seconds after its pod: measured, the first
    # collection after the pods went kept `hello`.
    await cp.wait_until_succeeds(
        f"nix-store --store daemon --gc >> {COEXIST}/gc-before.log 2>&1;"
        f" ! nix-store --store daemon --check-validity {hello} 2>/dev/null",
        timeout=120,
        poll=5,
    )
    print(f"[test] the host's collector deleted {hello}, so the pods' closures come from substitution")

    nix_store = (await cp.succeed("command -v nix-store")).stdout.strip()
    await cp.succeed(
        f"systemd-run --unit=coexist-gc-loop --collect /bin/sh -c"
        f" 'while [ ! -e {COEXIST}/stop ]; do echo run; {nix_store} --store daemon --gc 2>&1; done"
        f" > {COEXIST}/gc-loop.log 2>&1'"
    )
    names = [f"coexist-{name}" for name in templates]
    for name, template in templates.items():
        job = Job(
            {
                "apiVersion": "batch/v1",
                "kind": "Job",
                "metadata": {"name": f"coexist-{name}", "namespace": NAMESPACE},
                "spec": {"backoffLimit": 0, "template": again(template)},
            },
            api=api,
        )
        await job.create()

    async def complete() -> tuple[bool, str]:
        done = await succeeded(api)
        return all(n in done for n in names), await job_table(api)

    try:
        await until("the jobs to complete under collection", complete, 300, cp)
    finally:
        await cp.succeed(f"touch {COEXIST}/stop")
        await cp.wait_until_fails("systemctl is-active --quiet coexist-gc-loop", timeout=300)
    runs = (await cp.succeed(f"grep -c '^run$' {COEXIST}/gc-loop.log")).stdout.strip()
    pods = [p for n in names for p in await listed(Pod, api, namespace=NAMESPACE, label_selector={"job-name": n})]
    ours = {p.name for p in pods}
    failed = [
        f"{raw(e)['involvedObject']['name']}: {raw(e).get('message')}"
        for e in await listed(Event, api, namespace=NAMESPACE, field_selector={"reason": "FailedMount"})
        if raw(e)["involvedObject"]["name"] in ours
    ]
    assert not failed, "a publish failed while the host collected garbage:\n" + "\n".join(failed)
    assert int(runs) > 1, f"the host's collector ran {runs} times while the pods started"
    print(f"[test] {', '.join(names)} completed while the host's collector ran {runs} times, with no FailedMount")
    for name in names:
        await delete(await Job.get(name, namespace=NAMESPACE, api=api), cp)


async def check_coexistence(api: kr8s.asyncio.Api, cp: Machine, templates: dict[str, Manifest]) -> None:
    """The node's store server and the host's daemon share /nix: locks and temporary roots, both ways."""
    await cp.succeed(f"rm -rf {COEXIST} && mkdir -p {COEXIST}")
    await check_writers(cp)
    await check_root_file(cp)
    await check_root_socket(cp)
    await check_publish_under_gc(api, cp, templates)


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

        # Two writers on one /nix: the node's server and the NixOS host's daemon.
        coexist = settings["hostStore"] and not settings["foreignHost"]
        templates = {
            name: raw(await Job.get(name, namespace=NAMESPACE, api=api))["spec"]["template"]
            for name in (COEXIST_JOBS if coexist else ())
        }
        outage = raw(await Job.get("path-hello", namespace=NAMESPACE, api=api))["spec"]["template"]

        for name in settings["deployedJobs"]:
            await delete(await Job.get(name, namespace=NAMESPACE, api=api), cp)
        selector = f"job-name in ({','.join(settings['deployedJobs'])})"

        async def pods_gone() -> tuple[bool, str]:
            pods = await listed(Pod, api, namespace=NAMESPACE, label_selector=selector)
            return not pods, pod_summary(pods)

        await until("the jobs' pods to go", pods_gone, 120, cp)
        print("[test] the jobs are gone and their pods with them")

        await check_store_outage(api, cp, outage)

        if coexist:
            await check_coexistence(api, cp, templates)

        # What `vivarium.diskSize` has to cover, measured rather than
        # guessed: the images this node pulled, every store path the jobs
        # asked for, and pynixd's volume when there is one. A runner has
        # 14 GB for the whole guest.
        print(
            "[test] node disk\n"
            + (await cp.succeed("df -h /; du -sh /var/lib/vivarium-storage 2>/dev/null || true")).stdout
        )

        # Last: it leaves the node on a nodeEnv the deployment did not ask for.
        await check_behind(api, cp)
