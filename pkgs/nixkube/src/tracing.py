# SPDX-License-Identifier: Apache-2.0
"""OpenTelemetry spans of each volume publish, under the trace of the pynixd build that asked for the pod.

pynixd's central writes its build attempt's W3C `traceparent` into the volume's attributes
(`TRACEPARENT_ATTRIBUTE`), and `NodePublishVolume` receives them in `volume_context`: no API call. A volume
without one gets a trace of its own. nixidae/nixidae#1654.

Off unless `OTEL_EXPORTER_OTLP_TRACES_ENDPOINT` is set; the API's tracer is then a no-op.
"""

from __future__ import annotations

import contextlib
import os
from collections.abc import Iterator, Mapping
from typing import Final

import structlog
from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.trace import Span, Tracer
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

logger = structlog.get_logger("nixkube.tracing")

TRACEPARENT_ATTRIBUTE: Final = "traceparent"
"""A volume attribute: the `traceparent` of the span the publish belongs under. pynixd's central writes it
(`kubernetes.TRACEPARENT_ATTRIBUTE`)."""

ENDPOINT_VARIABLE: Final = "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT"

_PROPAGATOR: Final = TraceContextTextMapPropagator()

TRACER: Final[Tracer] = trace.get_tracer("nixkube.csi")
"""A proxy: it follows the provider `start` installs, and records nothing before."""


def start() -> bool:
    """Install an exporting provider when the endpoint is set; whether one was. The SDK's first span compiles
    code: run it in a thread, before the CSI server starts."""
    endpoint = os.environ.get(ENDPOINT_VARIABLE)
    if not endpoint:
        return False
    # A `service.name` given to `Resource.create` wins over OTEL_SERVICE_NAME.
    service = os.environ.get("OTEL_SERVICE_NAME") or "nixkube"
    provider = TracerProvider(resource=Resource.create({"service.name": service}))
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint)))
    trace.set_tracer_provider(provider)
    provider.get_tracer("nixkube").start_span("warmup").end()
    logger.info("tracing_started", endpoint=endpoint)
    return True


@contextlib.contextmanager
def publish_span(
    volume_context: Mapping[str, str],
    started_ns: int,
    attributes: Mapping[str, str],
    tracer: Tracer = TRACER,
) -> Iterator[Span]:
    """The span of one `NodePublishVolume`, from *started_ns* (the handler's entry), under the volume's
    `traceparent` when it has one. Its start, against pynixd's `pynixd.pod.central.place`, is the time kubelet
    took to call the driver."""
    parent = _PROPAGATOR.extract({"traceparent": volume_context.get(TRACEPARENT_ATTRIBUTE, "")})
    with tracer.start_as_current_span(
        "NodePublishVolume", context=parent, start_time=started_ns, attributes=dict(attributes)
    ) as span:
        yield span


def step(name: str) -> contextlib.AbstractContextManager[Span]:
    """A child span of the current publish, for one step of it."""
    return TRACER.start_as_current_span(name)


def annotate(key: str, value: str | int | float) -> None:
    """An attribute of the current span; nothing without one."""
    trace.get_current_span().set_attribute(key, value)
