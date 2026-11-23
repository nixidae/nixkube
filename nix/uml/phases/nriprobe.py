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
the new network namespace. The node's containerd carries NRI's fix for removing a variable
(`../containerd-nri-remove-env.patch`), so the phase asserts that `HOSTNAME` and the `KUBERNETES_*` are gone.
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
echo "env=$({b}/env | {b}/grep -E '^(KUBERNETES_|HOSTNAME=)' | {b}/cut -d= -f1 | {b}/sort | {b}/tr '\\n' ' ')"
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


def _ending_scripts(b: str) -> tuple[str, str]:
    """The controller and the builder of the pod whose builder exits: what the controller sees after that."""
    controller = f"""
echo filled > /work/ready
{b}/mkdir /work/db; echo v > /work/db/f; {b}/chmod 755 /work/db; {b}/chmod 644 /work/db/f
term() {{
  echo "term_at=$({b}/date +%s)"
  {b}/ls /work/out >/dev/null 2>&1; echo "read_dir=$?"
  {b}/cat /work/out/f >/dev/null 2>&1; echo "read_file=$?"
  {b}/chmod 755 /work/out 2>/dev/null; echo "chmod_out=$?"
  {b}/sleep 10
  echo "late_at=$({b}/date +%s)"
  echo "the controller's message" > /dev/termination-log
  exit 3
}}
trap term TERM
echo "started_at=$({b}/date +%s)"
while true; do {b}/sleep 1 & wait $!; done
"""
    builder = f"""
echo "argv=$*"
{b}/env | {b}/sort | {b}/sed 's/^/env:/'
{b}/mkdir /work/out; echo x > /work/out/f; {b}/chmod 600 /work/out/f; {b}/chmod 700 /work/out
(echo y >> /work/db/f) 2>/dev/null; echo "db_write=$?"
echo "ended_at=$({b}/date +%s)"
exit 7
"""
    return controller, builder


async def _ending_pod(api: kr8s.asyncio.Api, cp: Machine, settings: Settings) -> dict[str, object]:
    """The chosen shape, with a builder that exits: how the sidecar ends, and what reaches the builder's argv and env."""
    b = f"{settings['busybox']}/bin"
    controller_script, builder_script = _ending_scripts(b)
    work = [{"name": "work", "mountPath": "/work"}]
    controller: dict[str, object] = {
        "name": "controller",
        "image": settings["workloadImage"],
        "restartPolicy": "Always",
        "command": [f"{b}/sh", "-c", controller_script],
        "startupProbe": {"exec": {"command": [f"{b}/test", "-e", "/work/ready"]}, "periodSeconds": 1},
        "securityContext": {"runAsUser": 1000, "runAsGroup": 1000},
        "volumeMounts": work,
    }
    builder: dict[str, object] = {
        "name": "builder",
        "image": SCRATCH,
        "command": [f"{b}/sh", "-c", builder_script, "argv0"],
        "args": ["$(out)", "$$(out)", "$(NOPE)", "a$$b"],
        "env": [
            {"name": "out", "value": "/nix/store/abc-x"},
            {"name": "DOLLARS", "value": "a$$b$(out)c"},
            {"name": "FLAGS", "value": "PREFIX=$(out)"},
            {"name": "PATH", "value": "/path-not-set"},
        ],
        "securityContext": {"runAsUser": BUILD_UID, "runAsGroup": BUILD_UID},
        "volumeMounts": work,
    }
    pod = Pod(
        {
            "metadata": {"generateName": "nri-probe-end-", "namespace": "default", "labels": {LABEL: "1"}},
            "spec": {
                "nodeSelector": {"kubernetes.io/hostname": cp.name},
                "restartPolicy": "Never",
                "terminationGracePeriodSeconds": 30,
                "volumes": [{"name": "work", "emptyDir": {}}],
                "initContainers": [controller],
                "containers": [builder],
            },
        },
        api=api,
    )
    await pod.create()

    async def ended() -> tuple[bool, str]:
        await pod.refresh()
        status = raw(pod).get("status", {})
        return status.get("phase") in {"Succeeded", "Failed"}, json.dumps(status)

    status = json.loads(await until(f"pod {pod.name} ends", ended, POD_TIMEOUT, cp))
    said = {name: await pod_logs(pod, name) for name in ("controller", "builder")}
    await pod.delete()
    return {
        "phase": status.get("phase"),
        "controller": status.get("initContainerStatuses", []),
        "builder": status.get("containerStatuses", []),
        "said": said,
    }


async def _env_name(api: kr8s.asyncio.Api, cp: Machine, settings: Settings) -> str:
    """Whether the API takes a variable whose name holds `-` and `.`, as a derivation's may."""
    b = f"{settings['busybox']}/bin"
    pod = Pod(
        {
            "metadata": {"generateName": "nri-probe-envname-", "namespace": "default"},
            "spec": {
                "nodeSelector": {"kubernetes.io/hostname": cp.name},
                "restartPolicy": "Never",
                "terminationGracePeriodSeconds": 0,
                "containers": [
                    {
                        "name": "c",
                        "image": settings["workloadImage"],
                        "command": [f"{b}/true"],
                        "env": [{"name": "a-b.c", "value": "1"}],
                    }
                ],
            },
        },
        api=api,
    )
    try:
        await pod.create()
    except kr8s.ServerError as error:
        return f"refused: {error}"
    await pod.delete()
    return "accepted"


async def _post_start_pod(api: kr8s.asyncio.Api, cp: Machine, settings: Settings) -> dict[str, dict[str, str]]:
    """The other order: a plain controller container whose postStart hook waits, then the builder."""
    busybox = settings["busybox"]
    b = f"{busybox}/bin"
    controller: dict[str, object] = {
        "name": "controller",
        "image": settings["workloadImage"],
        "command": [f"{b}/sh", "-c", _report(busybox, "c", before=f"{b}/sleep 3; echo filled > /work/ready")],
        "lifecycle": {
            "postStart": {
                "exec": {"command": [f"{b}/sh", "-c", f"until {b}/test -e /work/ready; do {b}/sleep 0.2; done"]}
            }
        },
        "volumeMounts": [{"name": "work", "mountPath": "/work"}],
    }
    builder: dict[str, object] = {
        "name": "builder",
        "image": SCRATCH,
        "command": [f"{b}/sh", "-c", _report(busybox, "b")],
        "securityContext": {"runAsUser": BUILD_UID, "runAsGroup": BUILD_UID},
        "volumeMounts": [{"name": "work", "mountPath": "/work"}],
    }
    return await _reports(api, cp, {"containers": [controller, builder]}, ("builder", "controller"))


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
    # Without the patch NRI v0.12.3 drops each removal, and all nine stay (measured, run nrip5).
    if rewritten["env"].strip():
        wrong.append(f"variables the plugin removes are still set: {rewritten['env']}")
    if "KUBERNETES_SERVICE_HOST" not in control["env"].split():
        wrong.append("the control has no KUBERNETES_SERVICE_HOST: the check proves nothing")
    return wrong


async def test(vms: Machines) -> None:
    cp, settings = vms.cp, vms.settings
    api = await cluster(vms)
    await _start_plugin(cp, f"{settings['nriProbe']} {settings['busybox']}")
    try:
        found = await _probe_pod(api, cp, settings["workloadImage"], settings["busybox"])
        built = await _builder_pod(api, cp, settings)
        ending = await _ending_pod(api, cp, settings)
        env_name = await _env_name(api, cp, settings)
        post_start = await _post_start_pod(api, cp, settings)
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
    print(f"[nixkube] not asserted: the ending pod: {json.dumps(ending)}", flush=True)
    print(f"[nixkube] not asserted: a variable named a-b.c: {env_name}", flush=True)
    for name, report in post_start.items():
        print(f"[nixkube] postStart {name}: {json.dumps(report)}", flush=True)
    wrong = _check(found) + _check_builder(built)
    if post_start["builder"]["ready"] != "filled":
        wrong.append(f"the postStart hook did not hold the builder: {post_start['builder']['ready']!r}")
    if wrong:
        raise MachineError("[cp] the NRI rewrite did not hold:\n  " + "\n  ".join(wrong))
    print("[nixkube] NRI made the container's namespaces, seccomp filter and mounts", flush=True)
