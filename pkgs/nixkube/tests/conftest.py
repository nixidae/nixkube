# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

# OpenTelemetry takes the global provider once per process, and `tracing.step`
# traces through it: one recording provider for the whole session.
_EXPORTER = InMemorySpanExporter()
_provider = TracerProvider()
_provider.add_span_processor(SimpleSpanProcessor(_EXPORTER))
trace.set_tracer_provider(_provider)


@pytest.fixture
def spans() -> InMemorySpanExporter:
    """The spans finished during this test."""
    _EXPORTER.clear()
    return _EXPORTER
