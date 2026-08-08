"""Universal in-process OpenTelemetry span bridge for pylier."""

from __future__ import annotations

from typing import Any

from pylier.model import TraceHistory
from pylier.recorder import trace_history

try:  # Keep the base library importable without the optional OTel SDK.
    from opentelemetry.sdk.trace import SpanProcessor as _SpanProcessor
except ImportError:

    class _SpanProcessor:  # type: ignore[no-redef]
        """Fallback that lets ``instrument_otel`` raise a useful runtime error."""


__all__ = ["PylierSpanProcessor", "instrument_otel"]

_installed_processors: dict[tuple[int, int], PylierSpanProcessor] = {}


class PylierSpanProcessor(_SpanProcessor):
    """Materialize completed OTel SDK spans into the matching pylier trace."""

    def __init__(self, history: TraceHistory | None = None) -> None:
        self.history = history or trace_history()

    def on_start(self, span: Any, parent_context: Any | None = None) -> None:
        """OTel SDK hook; completed spans carry the debugger payload on end."""

    def on_end(self, span: Any) -> None:
        """Import a completed span when its OTel trace belongs to pylier."""
        context = span.get_span_context()
        if not context.is_valid:
            return
        trace = self.history.find_by_otel_trace_id(f"{context.trace_id:032x}")
        if trace is None:
            return
        node_id = trace.record_otel_span(span)
        if node_id is None:
            return
        node = trace.nodes[node_id]
        trace.emit_external_event(
            {
                "type": "otel.span",
                "node_id": node_id,
                "name": node.name,
                "otel": node.otel,
                "links": [
                    {
                        "source": edge.source,
                        "target": edge.target,
                        "kind": edge.kind,
                        "metadata": edge.metadata,
                    }
                    for edge in trace.edges.values()
                    if edge.source == node_id or edge.target == node_id
                ],
            }
        )

    def shutdown(self) -> None:
        """OTel SDK lifecycle hook; pylier retains no external resources."""

    def force_flush(self, timeout_millis: int = 30_000) -> bool:
        """OTel SDK lifecycle hook."""
        return True


def instrument_otel(*, provider: Any | None = None, history: TraceHistory | None = None) -> PylierSpanProcessor:
    """Attach pylier's universal span bridge to an OpenTelemetry SDK provider.

    Args:
        provider: OTel SDK tracer provider. Defaults to the global provider.
        history: Optional trace history, useful for isolated integrations/tests.

    Returns:
        The installed span processor.

    Raises:
        RuntimeError: If OpenTelemetry is missing or the provider is not an SDK provider.
    """
    try:
        from opentelemetry import trace as otel_trace
    except ImportError as error:
        raise RuntimeError("instrument_otel() requires opentelemetry-api and opentelemetry-sdk") from error
    resolved_provider = provider or otel_trace.get_tracer_provider()
    add_span_processor = getattr(resolved_provider, "add_span_processor", None)
    if not callable(add_span_processor):
        raise RuntimeError("instrument_otel() requires an OpenTelemetry SDK TracerProvider")
    resolved_history = history or trace_history()
    key = (id(resolved_provider), id(resolved_history))
    if key in _installed_processors:
        return _installed_processors[key]
    processor = PylierSpanProcessor(resolved_history)
    add_span_processor(processor)
    _installed_processors[key] = processor
    return processor
