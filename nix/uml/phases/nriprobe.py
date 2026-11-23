"""Can an NRI plugin make a container into Nix's sandbox? A measurement for nixidae/nixidae#1560.

`../nri-probe.py` runs as a second NRI plugin on the control plane and changes the container `rewritten` of
one pod. The pod's other container, `control`, has no change. Each one reports from inside itself, and
the phase asserts that each change shows in `rewritten` and not in `control`:

  - its own network, IPC, UTS and cgroup namespace
  - a seccomp filter that refuses a setuid chmod
  - no kubelet file mount and no service account token

A second pod is the chosen build pod's shape: a controller as a native sidecar, then `builder` on an empty
image as a build uid. The phase asserts that the builder starts after the controller fills `/work`, runs as
that uid, gets the same namespaces and filter, and that the plugin's hook sets its host name to `localhost`.

It reports, and does not assert, what NRI has no field for: the hostname, and whether loopback is up in
the new network namespace. It also reports the `KUBERNETES_*` variables, which the plugin asks to remove
and this NRI keeps (see `_check`).
"""

import json

import kr8s.asyncio
from kr8s.asyncio.objects import Pod
from nixkube_uml import Settings, cluster, logs as pod_logs, raw
from vivarium_k8s import until
from vivarium_runner import Machine, MachineError, Machines

LABEL = "nixkube.test/nri-probe"
READY = "/run/nri-probe.ready"
POD_TIMEOUT = 3 * 60
DONE = "nri-probe-done"
SCRATCH = "ghcr.io/nixidae/nixkube/scratch:1.0.2"
BUILD_UID = 30001
NAMESPACES = ("net", "ipc", "uts", "cgroup")
MOUNTS = (
    "/etc/hosts",
    "/etc/hostname",
    "/etc/resolv.conf",
    "/dev/termination-log",
    "/var/run/secrets/kubernetes.io/serviceaccount",
)


def _report(busybox: str, name: str = "f", before: str = "") -> str:
    b = f"{busybox}/bin"
    namespaces = "; ".join(f'echo "ns.{n}=$({b}/readlink /proc/self/ns/{n})"' for n in (*NAMESPACES, "pid", "mnt"))
    return f"""
{before}
echo "uid=$({b}/id -u)"
echo "root=$({b}/ls -A / | {b}/tr '\\n' ' ')"
echo "ready=$({b}/cat /work/ready 2>&1)"
{namespaces}
echo "seccomp=$({b}/awk '/^Seccomp:/ {{print $2}}' /proc/self/status)"
echo "hostname=$({b}/hostname)"
echo "links=$({b}/ls /sys/class/net | {b}/tr '\\n' ' ')"
echo "lo=$({b}/cat /sys/class/net/lo/operstate)"
echo "lo_addr=$({b}/ip -o addr show lo 2>&1 | {b}/tr '\\n' ' ')"
{b}/nc -l -s 127.0.0.1 -p 9999 </dev/null >/dev/null 2>&1 &
{b}/sleep 1
if {b}/kill -0 $! 2>/dev/null; then echo bind=ok; {b}/kill $!; else echo bind=failed; fi
{b}/touch /work/{name}
{b}/chmod 4755 /work/{name} 2>/dev/null; echo "setuid_chmod=$?"
{b}/chmod 0755 /work/{name} 2>/dev/null; echo "plain_chmod=$?"
echo "mounts=$({b}/awk '{{print $5}}' /proc/self/mountinfo | {b}/tr '\\n' ' ')"
echo "env=$({b}/env | {b}/grep ^KUBERNETES_ | {b}/cut -d= -f1 | {b}/sort | {b}/tr '\\n' ' ')"
echo {DONE}
exec {b}/sleep 600
"""


def _parse(said: str) -> dict[str, str]:
    return dict(line.split("=", 1) for line in said.splitlines() if "=" in line)


async def _start_plugin(cp: Machine, plugin: str) -> None:
    await cp.succeed(f"rm -f {READY}")
    await cp.succeed(f"systemd-run --unit=nri-probe --collect {plugin}")
    await cp.wait_until_succeeds(f"test -e {READY}", timeout=60)
    print("[nixkube] the probe NRI plugin is registered", flush=True)


async def _probe_pod(api: kr8s.asyncio.Api, cp: Machine, image: str, busybox: str) -> dict[str, dict[str, str]]:
    script = _report(busybox)
    containers: list[dict[str, object]] = [
        {
            "name": name,
            "image": image,
            "command": [f"{busybox}/bin/sh", "-c", script],
            "volumeMounts": [{"name": "work", "mountPath": "/work"}],
        }
        for name in ("rewritten", "control")
    ]
    return await _reports(api, cp, {"containers": containers}, ("rewritten", "control"))


async def _builder_pod(api: kr8s.asyncio.Api, cp: Machine, settings: Settings) -> dict[str, dict[str, str]]:
    """The chosen shape: a controller as a native sidecar, then a builder on an empty image as a build uid."""
    busybox = settings["busybox"]
    b = f"{busybox}/bin"
    controller: dict[str, object] = {
        "name": "controller",
        "image": settings["workloadImage"],
        "restartPolicy": "Always",
        "command": [f"{b}/sh", "-c", _report(busybox, "c", before="echo filled > /work/ready")],
        "startupProbe": {"exec": {"command": [f"{b}/test", "-e", "/work/ready"]}, "periodSeconds": 1},
        "volumeMounts": [{"name": "work", "mountPath": "/work"}],
    }
    builder: dict[str, object] = {
        "name": "builder",
        "image": SCRATCH,
        "command": [f"{b}/sh", "-c", _report(busybox, "b")],
        "securityContext": {"runAsUser": BUILD_UID, "runAsGroup": BUILD_UID},
        "volumeMounts": [{"name": "work", "mountPath": "/work"}],
    }
    spec = {"initContainers": [controller], "containers": [builder]}
    return await _reports(api, cp, spec, ("builder", "controller"))


async def _reports(
    api: kr8s.asyncio.Api, cp: Machine, containers: dict[str, list[dict[str, object]]], names: tuple[str, ...]
) -> dict[str, dict[str, str]]:
    pod = Pod(
        {
            "metadata": {"generateName": "nri-probe-", "namespace": "default", "labels": {LABEL: "1"}},
            "spec": {
                "nodeSelector": {"kubernetes.io/hostname": cp.name},
                "restartPolicy": "Never",
                "terminationGracePeriodSeconds": 0,
                "volumes": [{"name": "work", "emptyDir": {}}],
                **containers,
            },
        },
        api=api,
    )
    await pod.create()

    async def reported() -> tuple[bool, str]:
        await pod.refresh()
        if raw(pod).get("status", {}).get("phase") != "Running":
            return False, json.dumps(raw(pod).get("status", {}))
        said = {name: await pod_logs(pod, name) for name in names}
        return all(DONE in s for s in said.values()), json.dumps(said)

    said = json.loads(await until(f"pod {pod.name} reports", reported, POD_TIMEOUT, cp))
    await pod.delete()
    return {name: _parse(text) for name, text in said.items()}


def _check_builder(found: dict[str, dict[str, str]]) -> list[str]:
    builder, controller = found["builder"], found["controller"]
    wrong = [f"ns.{n} is the pod's" for n in NAMESPACES if builder[f"ns.{n}"] == controller[f"ns.{n}"]]
    if builder["hostname"] != "localhost":
        wrong.append(f"the hook did not set the host name: {builder['hostname']!r}")
    if controller["hostname"] == "localhost":
        wrong.append("the controller's host name is localhost too: the check proves nothing")
    if builder["uid"] != str(BUILD_UID):
        wrong.append(f"the builder runs as {builder['uid']}")
    if builder["ready"] != "filled":
        wrong.append(f"the builder started before the controller filled its directory: {builder['ready']!r}")
    if builder["seccomp"] != "2":
        wrong.append("the builder has no seccomp filter")
    return wrong


def _check(found: dict[str, dict[str, str]]) -> list[str]:
    rewritten, control = found["rewritten"], found["control"]
    wrong = [f"ns.{n} is the pod's" for n in NAMESPACES if rewritten[f"ns.{n}"] == control[f"ns.{n}"]]
    if rewritten["seccomp"] != "2" or rewritten["setuid_chmod"] == "0" or rewritten["plain_chmod"] != "0":
        wrong.append("the seccomp policy does not refuse a setuid chmod alone")
    if control["setuid_chmod"] != "0":
        wrong.append("the control refuses a setuid chmod: the check proves nothing")
    for path in MOUNTS:
        if path in rewritten["mounts"].split():
            wrong.append(f"{path} is still mounted")
        if path not in control["mounts"].split():
            wrong.append(f"the control has no {path}: the check proves nothing")
    # Not the variables: NRI v0.12.3's `result.adjustEnv` drops a removal of a variable no plugin set, and the
    # runtime sees none (measured: all eight stayed). containerd/nri 193a7fdfa4 fixes it, after v0.12.5.
    return wrong


async def test(vms: Machines) -> None:
    cp, settings = vms.cp, vms.settings
    api = await cluster(vms)
    await _start_plugin(cp, f"{settings['nriProbe']} {settings['busybox']}")
    try:
        found = await _probe_pod(api, cp, settings["workloadImage"], settings["busybox"])
        built = await _builder_pod(api, cp, settings)
    finally:
        offered = (await cp.execute("journalctl -u nri-probe -o cat --no-pager")).stdout
        await cp.execute("systemctl stop nri-probe")
    for line in offered.splitlines():
        if line.startswith("{"):
            print(f"[nixkube] the runtime offered the plugin: {line}", flush=True)
    for name, report in found.items():
        print(f"[nixkube] {name}: {json.dumps(report)}", flush=True)
    rewritten = found["rewritten"]
    print(
        f"[nixkube] not asserted: hostname={rewritten['hostname']!r} lo={rewritten['lo']!r}"
        f" bind={rewritten['bind']!r} links={rewritten['links']!r} env={rewritten['env']!r}",
        flush=True,
    )
    for name, report in built.items():
        print(f"[nixkube] {name}: {json.dumps(report)}", flush=True)
    print(f"[nixkube] not asserted: the builder's root on an empty image: {built['builder']['root']!r}", flush=True)
    if wrong := _check(found) + _check_builder(built):
        raise MachineError("[cp] the NRI rewrite did not hold:\n  " + "\n  ".join(wrong))
    print("[nixkube] NRI made the container's namespaces, seccomp filter and mounts", flush=True)
