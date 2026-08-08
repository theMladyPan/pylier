"""FastAPI / Starlette instrumentation for pylier.

A single pure-ASGI middleware that opens one :func:`pylier.trace` per HTTP
request and stamps the response status onto the trace. Endpoints themselves
stay un-decorated: decorate the service/business functions you call from a
handler with :func:`pylier.node` and they appear in the request's graph.

Design
------
* Self-contained adapter. Touches only the public pylier surface
  (``pylier.trace``) and the already-existing ``Trace.endpoint["status_code"]``
  field. No edits to the recorder or model.
* Pure ASGI (no ``BaseHTTPMiddleware``) so it adds no per-request task and no
  extra dependency beyond what FastAPI already pulls in (starlette).
* Installed via ``uv add pylier[fastapi]`` and activated with
  ``pylier.instrument_fastapi(app)`` — must be called before the app serves its
  first request, like any Starlette ``add_middleware``.

Usage::

    from fastapi import FastAPI
    import pylier

    app = FastAPI()
    pylier.instrument_fastapi(app)
"""

from __future__ import annotations

from typing import Any

import pylier

__all__ = ["PylierMiddleware", "instrument_fastapi"]


class PylierMiddleware:
    """Pure-ASGI middleware: one pylier trace per HTTP request.

    Follows the Starlette middleware convention (``__init__(self, app)``) so it
    can be registered with ``app.add_middleware(PylierMiddleware)``. Non-HTTP
    scopes (lifespan, websocket) are passed through untraced.
    """

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        # Only http requests get a trace; lifespan/websocket pass through.
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        method = scope.get("method", "?")
        path = scope.get("path", "?")
        # One trace per request -> live viewer shows the newest 100 as a
        # request log (TraceHistory caps and evicts oldest automatically).
        with pylier.trace(f"{method} {path}") as trace:
            status_code: int | None = None

            async def send_wrapper(message: dict[str, Any]) -> None:
                nonlocal status_code
                # The first http.response.start carries the status; capture it
                # before forwarding so we can stamp the trace after the body.
                if message.get("type") == "http.response.start":
                    status_code = message.get("status")
                await send(message)

            try:
                await self.app(scope, receive, send_wrapper)
            finally:
                # Trace.endpoint is an existing render-time field ({"name",
                # "status_code"}); setting it here populates the viewer header
                # without touching the recorder/model. No version bump needed:
                # status is read at render time, not via the live event stream.
                if status_code is not None:
                    trace.endpoint["status_code"] = status_code


def instrument_fastapi(app: Any) -> Any:
    """Install pylier request tracing on a FastAPI/Starlette application.

    Must be called before the app serves its first request (Starlette builds its
    middleware stack lazily and rejects ``add_middleware`` after that point).

    Args:
        app: A FastAPI or Starlette application exposing ``add_middleware``.

    Returns:
        The same ``app``, for chaining.

    Raises:
        ImportError: If FastAPI/Starlette is not installed (install the extra:
            ``uv add pylier[fastapi]``).
        AttributeError: If ``app`` has no ``add_middleware`` (not a Starlette app).
    """
    try:
        import fastapi  # noqa: F401  (assert extra is installed)
    except ImportError as exc:  # pragma: no cover - exercised only without extra
        raise ImportError(
            "pylier.instrument_fastapi requires FastAPI. Install it with: uv add pylier[fastapi]"
        ) from exc

    if not hasattr(app, "add_middleware"):
        raise AttributeError(
            "instrument_fastapi(app): app has no add_middleware — pass a FastAPI/Starlette application"
        )

    app.add_middleware(PylierMiddleware)
    return app
