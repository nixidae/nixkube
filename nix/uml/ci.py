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

    nix run --file . ciTest.driver -- --out ./o
"""

import os
import subprocess
import tempfile

from vivarium_runner import Machine, Machines
from vivarium_runner.cluster import (
    KUBE_DNS,
    KUBE_PROXY,
    bring_up,
    get_json,
    kubectl,
    until,
    wait_for_pods,
)

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
        "6443 is not forwarded, so the host cannot reach the API server -- see vivarium.forward in nix/uml/ci.nix"
    )
    # kubeadm always names it `kubernetes`, but ask rather than assume.
    cluster = (await cp.succeed("kubectl config view --output jsonpath='{.clusters[0].name}'")).strip()
    conf = "/tmp/host-kubeconfig"
    await cp.succeed(f"cp /etc/kubernetes/admin.conf {conf}")
    await cp.succeed(f"kubectl --kubeconfig {conf} config set-cluster {cluster} --server=https://{reachable[0]}")
    if INSECURE:
        await cp.succeed(f"kubectl --kubeconfig {conf} config unset clusters.{cluster}.certificate-authority-data")
        await cp.succeed(f"kubectl --kubeconfig {conf} config set clusters.{cluster}.insecure-skip-tls-verify true")
    return await cp.succeed(f"cat {conf}")


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


async def check_store_split(cp: Machine, nixos: bool) -> None:
    """With `nixkube.hostStore.enable`, the node's OS picks its DaemonSet."""
    node = await get_json(cp, "get node cp")
    label = node["metadata"].get("labels", {}).get("nixkube/host")
    assert label == ("nixos" if nixos else None), (
        f"the node's nixkube/host label is {label!r} on a {'NixOS' if nixos else 'non-NixOS'} node"
    )
    serving = "nix-node-host" if nixos else "nix-node-separate"
    idle = "nix-node-separate" if nixos else "nix-node-host"
    for name, want in ((serving, 1), (idle, 0)):
        status = (await get_json(cp, f"get daemonset {name} --namespace nixkube"))["status"]
        ready = status.get("numberReady", 0)
        scheduled = status.get("desiredNumberScheduled", 0)
        assert (ready, scheduled) == (want, want), f"{name} has {ready} ready of {scheduled} scheduled, not {want}"
    print(f"[test] {serving} serves the node, {idle} schedules nothing")


async def test(vms: Machines) -> None:
    settings = vms.settings
    cp = await bring_up(vms, nix_images=False, addons=(KUBE_PROXY, KUBE_DNS))

    with tempfile.TemporaryDirectory(prefix="nixkube-ci-") as work:
        kubeconfig = os.path.join(work, "kubeconfig")
        write(kubeconfig, await host_kubeconfig(cp))
        print(f"[test] the host reaches the API server on {cp.reachable(6443)[0]}")
        print(await kubectl(cp, "get nodes"))

        if settings["foreignHost"]:
            await cp.succeed("rm /etc/NIXOS")
            print("[test] /etc/NIXOS removed: the node is not NixOS to nixkube")

        run(settings["deploy"], kubeconfig, "--yes")
        print("[test] nixkube deployed through kubenixDeploy")

        async def rolled_out():
            rc, out = await cp.execute(
                "kubectl rollout status daemonset"
                " --selector app.kubernetes.io/component=node"
                " --namespace nixkube --timeout=10s"
            )
            return rc == 0, out.strip().splitlines()[-1:] or ["(no answer yet)"]

        await until("the node DaemonSet", rolled_out, 300, cp)
        print("[test] the node DaemonSet rolled out")

        if settings["hostStore"]:
            await check_store_split(cp, nixos=not settings["foreignHost"])

        if settings["pynixd"]:
            # The `Wait for nix-csi cache pod` step of `test-kind-cache`.
            #
            # pynixd is a StatefulSet whose claim names no StorageClass, so
            # it only runs on a cluster with a default one -- see
            # `services.vivarium-k8s.persistentVolumes` in nix/uml/ci.nix. The
            # claim is reported beside the pod because a Pending pod and an
            # unbound claim look the same from here.
            await wait_for_pods(
                cp,
                "--selector app.kubernetes.io/component=pynixd",
                namespace="nixkube",
            )
            claims = await kubectl(cp, "get pvc --namespace nixkube --no-headers")
            print(f"[test] the pynixd cache is ready, on {claims.strip()}")

        run(settings["deployWorkloads"], kubeconfig, "--yes")
        print("[test] the test workloads are deployed")

        asserted = settings["assertedJobs"]

        async def succeeded() -> set[str]:
            out = await kubectl(
                cp,
                "get jobs --namespace nixkube --no-headers"
                " --output custom-columns=NAME:.metadata.name,OK:.status.succeeded",
            )
            return {
                row.split()[0]
                for row in out.splitlines()
                if len(row.split()) > 1 and row.split()[1] not in ("<none>", "0")
            }

        async def complete():
            done = await succeeded()
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
        mounts = await get_json(
            cp,
            "get events --namespace nixkube --field-selector reason=NixVolumeMount",
        )
        assert mounts["items"], "the jobs completed and the CSI driver reported no NixVolumeMount"
        print(f"[test] the CSI driver reported {len(mounts['items'])} NixVolumeMount events")

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
        wrong = sorted(await succeeded() & set(rejected))
        assert not wrong, (
            f"{', '.join(wrong)} succeeded, and each of them asks for"
            " something that cannot be built:\n" + await kubectl(cp, "get jobs --namespace nixkube")
        )
        print(f"[test] {', '.join(rejected)} were all refused")

        deployed = " ".join(settings["deployedJobs"])
        await kubectl(cp, f"delete job --namespace nixkube {deployed}", timeout=300)
        selector = ",".join(settings["deployedJobs"])
        await kubectl(
            cp,
            f'wait --for=delete pod --selector "job-name in ({selector})" --namespace nixkube --timeout=120s',
            timeout=180,
        )
        print("[test] the jobs are gone and their pods with them")

        # What `vivarium.diskSize` has to cover, measured rather than
        # guessed: the images this node pulled, every store path the jobs
        # asked for, and pynixd's volume when there is one. A runner has
        # 14 GB for the whole guest.
        print("[test] node disk\n" + await cp.succeed("df -h /; du -sh /var/lib/vivarium-storage 2>/dev/null || true"))
