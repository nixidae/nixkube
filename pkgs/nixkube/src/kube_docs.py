# SPDX-License-Identifier: Apache-2.0

"""Typed views of the Kubernetes JSON that kr8s returns in `.raw`.

Only the fields nixkube reads are listed. The API leaves out any field it
has no value for, so every field is optional. Read helpers cast `.raw` to
one of these types, once, at the point of reading.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, TypedDict, cast

if TYPE_CHECKING:
    from kr8s.asyncio.objects import APIObject, Job, Node, Pod, PodTemplate, Secret


class OwnerReference(TypedDict, total=False):
    apiVersion: str
    kind: str
    name: str
    uid: str


class ObjectMeta(TypedDict, total=False):
    name: str
    namespace: str
    uid: str
    generation: int
    labels: dict[str, str]
    annotations: dict[str, str]
    ownerReferences: list[OwnerReference]
    deletionTimestamp: str


class Condition(TypedDict, total=False):
    type: str
    status: str


class MatchExpression(TypedDict, total=False):
    key: str
    operator: str
    values: list[str]


class LabelSelector(TypedDict, total=False):
    matchLabels: dict[str, str]
    matchExpressions: list[MatchExpression]


class Terminated(TypedDict, total=False):
    exitCode: int


class ContainerStateRunning(TypedDict, total=False):
    startedAt: str


class ContainerState(TypedDict, total=False):
    running: ContainerStateRunning
    terminated: Terminated


class ContainerStatus(TypedDict, total=False):
    name: str
    state: ContainerState


class PodStatus(TypedDict, total=False):
    phase: str
    conditions: list[Condition]
    containerStatuses: list[ContainerStatus]


class VolumeMount(TypedDict, total=False):
    name: str
    mountPath: str
    subPath: str


class Container(TypedDict, total=False):
    name: str
    volumeMounts: list[VolumeMount]


class CSIVolumeSource(TypedDict, total=False):
    driver: str


class EmptyDirVolumeSource(TypedDict, total=False):
    medium: str
    sizeLimit: str


class Volume(TypedDict, total=False):
    name: str
    csi: CSIVolumeSource
    emptyDir: EmptyDirVolumeSource


class PodSpec(TypedDict, total=False):
    nodeName: str
    hostUsers: bool
    containers: list[Container]
    initContainers: list[Container]
    volumes: list[Volume]


class PodDoc(TypedDict, total=False):
    metadata: ObjectMeta
    spec: PodSpec
    status: PodStatus


class NodeInfo(TypedDict, total=False):
    bootID: str


class NodeStatus(TypedDict, total=False):
    conditions: list[Condition]
    nodeInfo: NodeInfo


class NodeDoc(TypedDict, total=False):
    metadata: ObjectMeta
    status: NodeStatus


class JobStatus(TypedDict, total=False):
    conditions: list[Condition]


class JobDoc(TypedDict, total=False):
    metadata: ObjectMeta
    status: JobStatus


class SecretDoc(TypedDict, total=False):
    type: str
    data: dict[str, str]


class EventSeries(TypedDict, total=False):
    count: int
    lastObservedTime: str


class EventDoc(TypedDict, total=False):
    series: EventSeries


class LeaseSpec(TypedDict, total=False):
    renewTime: str


class LeaseDoc(TypedDict, total=False):
    spec: LeaseSpec


def pod_doc(pod: Pod) -> PodDoc:
    return cast(PodDoc, pod.raw)


def node_doc(node: Node) -> NodeDoc:
    return cast(NodeDoc, node.raw)


def job_doc(job: Job) -> JobDoc:
    return cast(JobDoc, job.raw)


def secret_doc(secret: Secret) -> SecretDoc:
    return cast(SecretDoc, secret.raw)


def lease_doc(lease: APIObject) -> LeaseDoc:
    return cast(LeaseDoc, lease.raw)


def event_doc(event: APIObject) -> EventDoc:
    return cast(EventDoc, event.raw)


class EnvVar(TypedDict, total=False):
    name: str
    value: str


class TemplateContainer(TypedDict, total=False):
    name: str
    env: list[EnvVar]


class TemplateSpec(TypedDict, total=False):
    nodeName: str
    containers: list[TemplateContainer]
    initContainers: list[TemplateContainer]


class PodTemplateSpec(TypedDict, total=False):
    metadata: ObjectMeta
    spec: TemplateSpec


class PodTemplateDoc(TypedDict, total=False):
    template: PodTemplateSpec


class SecretRef(TypedDict, total=False):
    name: str


class RolloutSource(TypedDict, total=False):
    url: str
    rev: str
    attribute: str
    secretRef: SecretRef


class RolloutSpec(TypedDict, total=False):
    source: RolloutSource
    systems: dict[str, str]
    nodeSelector: LabelSelector
    confirmTimeout: int
    drainTimeout: int
    rebootTimeout: int
    maxUnavailable: int
    paused: bool


class NodeEntry(TypedDict, total=False):
    """One node's entry in a NixOSRollout's status. Phases are NodePhase values."""

    phase: str
    target: str
    current: str
    failedSystem: str
    drained: bool
    generation: int
    message: str
    since: str
    job: str
    bootID: str
    leaseRenewTime: str


class RolloutStatus(TypedDict, total=False):
    observedGeneration: int
    phase: str
    message: str
    nodes: dict[str, NodeEntry]


class RolloutDoc(TypedDict, total=False):
    metadata: ObjectMeta
    spec: RolloutSpec
    status: RolloutStatus


def rollout_doc(rollout: APIObject) -> RolloutDoc:
    return cast(RolloutDoc, rollout.raw)


def pod_template_doc(template: PodTemplate) -> PodTemplateDoc:
    return cast(PodTemplateDoc, template.raw)
