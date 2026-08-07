"""OpenTelemetry bridge for pylier's span tree.

The bridge deliberately uses OpenTelemetry's current context as the only parent
selection mechanism. This matches Logfire's span lifecycle while leaving data
lineage to pylier's fingerprint recorder.
"""

from __future__ import annotations

import threading
from collections.abc import Iterator
from contextlib import contextmanager

from opentelemetry import trace
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import SpanProcessor

from pylier.model import Span, Trace

__all__ = ["configure_otel", "trace_span", "node_span", "register_trace"]

_lock = threading.Lock()
_traces: dict[str, Trace] = {}
_processor_installed = False


def _hex(identifier: int) -> str:
    return f"{identifier:032x}" if identifier.bit_length() > 64 else f"{identifier:016x}"


class _PylierSpanProcessor(SpanProcessor):
    """Copies standard OTel spans into their matching pylier trace lifecycle."""

    def on_start(self, span: ReadableSpan, parent_context: object | None = None) -> None:
        self._record(span, ended=False)

    def on_end(self, readable_span: ReadableSpan) -> None:
        self._record(readable_span, ended=True)

    @staticmethod
    def _record(readable_span: ReadableSpan, *, ended: bool) -> None:
        context = readable_span.context
        trace_id = _hex(context.trace_id)
        with _lock:
            pylier_trace = _traces.get(trace_id)
        if pylier_trace is None:
            return
        parent_span_id = _hex(readable_span.parent.span_id) if readable_span.parent else None
        pylier_trace.record_span(
            Span(
                trace_id=trace_id,
                span_id=_hex(context.span_id),
                parent_span_id=parent_span_id,
                name=readable_span.name,
                started_ns=readable_span.start_time,
                ended_ns=readable_span.end_time if ended else None,
                status=readable_span.status.status_code.name.lower() if ended else "running",
                attributes={key: str(value) for key, value in readable_span.attributes.items()},
            )
        )

    def shutdown(self) -> None:
        pass

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        return True


def configure_otel() -> None:
    """Attach pylier's processor to the active SDK OTel provider.

    Creates the default SDK provider only when OTel has not been configured by
    the application. Existing providers and their exporters remain untouched.
    """
    global _processor_installed
    with _lock:
        if _processor_installed:
            return
        provider = trace.get_tracer_provider()
        if not isinstance(provider, TracerProvider):
            provider = TracerProvider()
            trace.set_tracer_provider(provider)
        provider.add_span_processor(_PylierSpanProcessor())
        _processor_installed = True


@contextmanager
def trace_span(name: str, pylier_trace: Trace) -> Iterator[None]:
    """Create the root OTel span for one independent pylier trace."""
    configure_otel()
    tracer = trace.get_tracer("pylier")
    with tracer.start_as_current_span(name) as span:
        trace_id = _hex(span.get_span_context().trace_id)
        pylier_trace.otel_trace_id = trace_id
        with _lock:
            _traces[trace_id] = pylier_trace
        from pylier.server import register_trace

        register_trace(pylier_trace)
        yield


@contextmanager
def node_span(name: str) -> Iterator[None]:
    """Create a standard child span for a decorated pylier node."""
    configure_otel()
    with trace.get_tracer("pylier").start_as_current_span(name):
        yield


def register_trace(pylier_trace: Trace) -> None:
    """Register an externally-created OTel trace after its root span is known."""
    if pylier_trace.otel_trace_id is not None:
        with _lock:
            _traces[pylier_trace.otel_trace_id] = pylier_trace
