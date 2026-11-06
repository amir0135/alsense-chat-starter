import logging

import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from alsense_chat import timing


@pytest.fixture
def spans(monkeypatch):
    """Spans recorded in memory instead of the default no-op tracer."""
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    monkeypatch.setattr(timing, "TRACER", provider.get_tracer("test"))
    yield exporter
    provider.shutdown()


@pytest.fixture
def warnings():
    """Warnings logged on the alsense_chat logger, which Application Insights exports."""
    records: list[logging.LogRecord] = []
    handler = logging.Handler(logging.WARNING)
    handler.emit = records.append  # type: ignore[method-assign]
    logger = logging.getLogger("alsense_chat")
    logger.addHandler(handler)
    yield records
    logger.removeHandler(handler)
