"""Optional FastAPI integration for an already-instrumented OTel application.

The middleware is plain ASGI to avoid importing FastAPI or Starlette from the
base pylier package. It observes the active OTel server span, then makes a
pylier trace active for the request so ordinary ``@pylier.node`` functions
remain the algorithm graph inside that external request root.
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from typing import Any

from pylier.model import Trace, TraceHistory
from pylier.recorder import mark_last_trace, reset_trace, trace_history, use_trace

__all__ = ["PylierASGIMiddleware", "instrument_fastapi"]

type ASGIApp = Callable[
    [dict[str, Any], Callable[..., Awaitable[dict]], Callable[..., Awaitable[None]]], Awaitable[None]
]


class PylierASGIMiddleware:
    """Capture an OTel-instrumented HTTP request as one pylier trace.

    Args:
        app: Wrapped ASGI application.
        history: Optional isolated retained history, mainly useful for tests.
    """

    def __init__(self, app: ASGIApp, history: TraceHistory | None = None) -> None:
        self.app = app
        self.history = history

    async def __call__(
        self, scope: dict[str, Any], receive: Callable[..., Awaitable[dict]], send: Callable[..., Awaitable[None]]
    ) -> None:
        """Create a request trace only when OTel supplies a valid current span."""
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        span_data = _active_span_data()
        if span_data is None:
            await self.app(scope, receive, send)
            return

        method = str(scope.get("method", "HTTP"))
        route = _route(scope, span_data[2])
        trace = Trace(f"{method} {route}")
        (self.history or trace_history()).add(trace)
        trace.set_request_root(
            method=method,
            route=route,
            otel_trace_id=span_data[0],
            otel_span_id=span_data[1],
        )
        mark_last_trace(trace)
        status_code: int | None = None

        async def capture_send(message: dict[str, Any]) -> None:
            nonlocal status_code
            if message.get("type") == "http.response.start":
                raw_status = message.get("status")
                status_code = raw_status if isinstance(raw_status, int) else None
            await send(message)

        token = use_trace(trace)
        started = time.perf_counter()
        try:
            await self.app(scope, receive, capture_send)
        finally:
            reset_trace(token)
            trace.set_request_status(status_code, (time.perf_counter() - started) * 1000.0)


def instrument_fastapi(app: Any) -> None:
    """Install request capture on a FastAPI application.

    Call this before adding OpenTelemetry FastAPI instrumentation, so the OTel
    server-span middleware wraps pylier and exposes its active context here.

    Args:
        app: FastAPI/Starlette app with an ``add_middleware`` method.

    Raises:
        TypeError: If the supplied application cannot install ASGI middleware.
    """
    add_middleware = getattr(app, "add_middleware", None)
    if not callable(add_middleware):
        raise TypeError("instrument_fastapi() requires a FastAPI/Starlette app with add_middleware()")
    add_middleware(PylierASGIMiddleware)


def _active_span_data() -> tuple[str, str, dict[str, Any]] | None:
    """Read current OTel IDs and attributes without making OTel mandatory."""
    try:
        from opentelemetry import trace as otel_trace
    except ImportError:
        return None
    span = otel_trace.get_current_span()
    context = span.get_span_context()
    if not context.is_valid:
        return None
    attributes = dict(getattr(span, "attributes", {}) or {})
    return f"{context.trace_id:032x}", f"{context.span_id:016x}", attributes


def _route(scope: dict[str, Any], attributes: dict[str, Any]) -> str:
    """Prefer OTel's route semantic attribute, then use the ASGI request path."""
    route = attributes.get("http.route") or scope.get("route")
    if hasattr(route, "path"):
        route = route.path
    return str(route or scope.get("path") or "/")
