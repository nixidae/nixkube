# SPDX-License-Identifier: Apache-2.0

{ config, lib, ... }:
let
  cfg = config.nixkube;

  /*
    A hostPath volume mounted with subPath is a trap on any distribution that
    runs the kubelet in a container.

    The kubelet performs a subPath bind itself, in its own mount namespace. A
    plain mount is handed to the runtime as a path instead, and resolves
    against the host tree. So the kubelet must be able to see the source, and
    for a hostPath volume outside /var/lib/kubelet it often cannot. It then
    binds an empty directory, with no error anywhere.

    A CSI volume is a different case and is allowed. Its source lives under
    /var/lib/kubelet, which a containerised kubelet must have or it cannot
    work at all.

    Both sides are measured, on Talos v1.13.9. One pod, one volume, mounted
    twice -- once plainly and once with subPath -- for each kind of volume:

      hostPath + subPath   source 0:62    overlay, container rootfs, EMPTY
      CSI      + subPath   source 253:5   xfs /dev/vda5, populated

    That contrast is the whole reason this assertion refuses one and permits
    the other.

    This is issue #16. It cost days to find, because the symptom named $PATH
    and not the mount. No test with a kubeadm control plane can catch it --
    kubeadm runs its kubelet in the host mount namespace. See issue #18.
  */

  # A pod template, wherever this kind keeps one. Returns null for a kind that
  # has none, such as a ConfigMap.
  podSpecOf =
    resource:
    resource.spec.template.spec or resource.spec.jobTemplate.spec.template.spec
      or (if resource.spec or null != null && resource.spec ? containers then resource.spec else null);

  # A list field of a rendered resource. `x or [ ]` is not enough: easykubenix
  # renders through typed submodules whose optional fields default to null, so
  # the attribute is present and null rather than missing. Measured on a
  # consumer tree -- Deployment/hubble-ui has a container with a null
  # volumeMounts, and `or [ ]` let the null through to lib.filter.
  listOf =
    attrs: field:
    let
      value = attrs.${field} or null;
    in
    if value == null then [ ] else value;

  # Present-and-null is not "has a subPath" either. Nothing hit this yet, but
  # `mount ? subPath` would fire on it and print "with subPath null".
  hasSubPath = mount: mount.subPath or null != null;

  offendersIn =
    kind: name: resource:
    let
      pod = podSpecOf resource;
    in
    if pod == null then
      [ ]
    else
      let
        hostPathVolumes = lib.pipe (listOf pod "volumes") [
          (lib.filter (volume: volume ? hostPath))
          (map (volume: volume.name))
        ];
        containers = (listOf pod "containers") ++ (listOf pod "initContainers");
        offendingMounts =
          container:
          lib.pipe (listOf container "volumeMounts") [
            (lib.filter (mount: hasSubPath mount && lib.elem mount.name hostPathVolumes))
            (map (
              mount:
              "${kind}/${name}: container ${container.name or "?"} mounts hostPath volume "
              + "${mount.name} at ${mount.mountPath} with subPath ${mount.subPath}"
            ))
          ];
      in
      lib.concatMap offendingMounts containers;

  /*
    An env var with an empty string value never survives a round trip.

    `EnvVar.Value` is `json:"value,omitempty"` in the Kubernetes Go types, so
    the apiserver drops it:

      rendered   {"name": "PYNIXD_SSH_HOST", "value": ""}
      live       {"name": "PYNIXD_SSH_HOST"}

    So the rendered object and the live one differ on a field nobody can make
    match. That much is measured, on nixlab2.

    What is *not* established is that this causes a GitOps tool to report
    drift. It was reported here as the cause of an ArgoCD Application staying
    OutOfSync, and then disproved on the same cluster: Rook's
    ceph-csi-controller-manager carries the same shape, on the same
    Application, and ArgoCD calls it Synced. A tool that normalises against the
    live object does not care.

    The reason to refuse it is simpler and does not need the drift claim.
    Writing a field that cannot survive is writing something untrue: the
    manifest says the variable is set to the empty string, and no object ever
    holds that. Anything that later diffs, audits or reasons about the
    rendered form starts from a value the cluster never had.

    The value belongs somewhere the apiserver does not rewrite. A string inside
    a ConfigMap is not an EnvVar and survives, which is where pynixd's
    `ssh_host` went.

    This is the same family as a rendered `kustomize = {}`: any field the
    apiserver drops must not be rendered.
  */
  emptyEnvIn =
    kind: name: resource:
    let
      pod = podSpecOf resource;
    in
    if pod == null then
      [ ]
    else
      let
        containers = (listOf pod "containers") ++ (listOf pod "initContainers");
        offending =
          container:
          lib.pipe (listOf container "env") [
            (lib.filter (entry: entry.value or null == ""))
            (map (
              entry:
              "${kind}/${name}: container ${container.name or "?"} sets ${entry.name} to an "
              + "empty string, which the apiserver drops"
            ))
          ];
      in
      lib.concatMap offending containers;

  /*
    Whose object is this.

    `kubernetes.objectChecks` sees every object of the whole instance, not
    nixkube's. A consumer deploys nixkube beside its own charts, so a check
    that refuses on any object lets a nixkube module veto a neighbour.

    That happened. The empty-string check refused to render a 448-object tree
    over two env vars in Rook's ceph-csi controller, where `WATCH_NAMESPACE:
    ""` is the ordinary idiom for "all namespaces". Nothing of nixkube's was
    wrong. The subPath check has the same reach and escaped notice only
    because pairing hostPath with subPath is rare.

    So the check refuses our objects, and a neighbour's gets a warning. A
    finding in someone else's chart is worth saying and is not ours to refuse.

    Every nixkube object carries this label -- measured over kubenixApply, 21
    of 21 -- because nixkube.matchLabels sets it and every resource merges
    those labels.
  */
  isOurs = resource: (resource.metadata.labels."app.kubernetes.io/part-of" or null) == "nixkube";

  subPathMessage =
    offenders:
    "A hostPath volume is mounted with subPath. The kubelet resolves a "
    + "subPath bind in its own mount namespace, so this silently mounts an "
    + "empty directory wherever the kubelet runs in a container, such as on "
    + "Talos. Give the subdirectory its own hostPath volume and mount it "
    + "plainly. See issue #16.\n  "
    + lib.concatStringsSep "\n  " offenders;

  emptyEnvMessage =
    offenders:
    "An env var is set to an empty string. EnvVar.Value is "
    + "`json:\"value,omitempty\"`, so the apiserver drops it and the live "
    + "object never matches what was rendered, so the manifest states a "
    + "value no object ever holds. Carry it somewhere the apiserver does "
    + "not rewrite, such as a ConfigMap, or give it a value that is not "
    + "empty.\n  "
    + lib.concatStringsSep "\n  " offenders;

  # An object check, not an assertion: an assertion walks every object for
  # any output, and the env values it reads force every derivation they name.
  check =
    object:
    let
      kind = object.kind or "?";
      name = object.metadata.name or "?";
      subPath = offendersIn kind name object;
      emptyEnv = emptyEnvIn kind name object;
    in
    if isOurs object then
      {
        errors =
          lib.optional (subPath != [ ]) (subPathMessage subPath)
          ++ lib.optional (emptyEnv != [ ]) (emptyEnvMessage emptyEnv);
      }
    else
      {
        warnings = lib.optional (subPath ++ emptyEnv != [ ]) (
          "nixkube: this object is not nixkube's, and carries a shape that does "
          + "not survive the apiserver or a containerised kubelet. Reported, not "
          + "refused.\n  "
          + lib.concatStringsSep "\n  " (subPath ++ emptyEnv)
        );
      };
in
{
  config = lib.mkIf cfg.enable {
    kubernetes.objectChecks = [ check ];
  };
}
