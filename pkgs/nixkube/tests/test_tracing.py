# SPDX-License-Identifier: Apache-2.0
"""A volume publish is a span under the pynixd build attempt that asked for the pod. nixidae/nixidae#1654."""

from __future__ import annotations

from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from src.tracing import TRACEPARENT_ATTRIBUTE, publish_span

TRACE_ID = 0x0AF7651916CD43DD8448EB211C80319C
SPAN_ID = 0xB7AD6B7169203331
TRACEPARENT = f"00-{TRACE_ID:032x}-{SPAN_ID:016x}-01"


def _published(volume_context: dict[str, str]) -> InMemorySpanExporter:
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = provider.get_tracer("test")
    with (
        publish_span(volume_context, 1_000, {"k8s.pod.name": "pynixd-build-x"}, tracer),
        tracer.start_as_current_span("mount"),
    ):
        pass
    return exporter


def test_a_publish_is_a_child_of_the_volume_s_traceparent() -> None:
    spans = {span.name: span for span in _published({TRACEPARENT_ATTRIBUTE: TRACEPARENT}).get_finished_spans()}

    publish, mount = spans["NodePublishVolume"], spans["mount"]
    assert publish.parent is not None
    assert (publish.context.trace_id, publish.parent.span_id) == (TRACE_ID, SPAN_ID)
    assert publish.start_time == 1_000, "the span starts when kubelet called, not when it was made"
    assert publish.attributes == {"k8s.pod.name": "pynixd-build-x"}
    assert mount.parent is not None and mount.parent.span_id == publish.context.span_id


def test_a_volume_without_a_traceparent_starts_a_trace() -> None:
    [publish, _] = sorted(_published({}).get_finished_spans(), key=lambda span: span.name)

    assert publish.name == "NodePublishVolume"
    assert publish.parent is None
