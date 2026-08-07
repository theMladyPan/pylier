"""Live viewer server.

A tiny stdlib-only HTTP server that runs in a background thread and serves the
current in-memory trace. The served page polls ``/graph`` every 1.5s and
re-renders the D3 graph in place, giving a live preview of a single running
trace without any disk sidecar (the sidecar path is a v0.2 addition for
cross-process / OTel replay).

This is intentionally minimal: stdlib ``http.server`` in a daemon thread so it
never blocks the user's program. For production-grade transport (OTel
receiver, websocket push) see the planned ``tracing/otel.py``.
"""

from __future__ import annotations

import json
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from pylier.model import Trace
from pylier.render import build_html

__all__ = ["serve"]


def serve(
    trace: Trace | None = None, port: int = 8765, *, open_browser: bool = True
) -> ThreadingHTTPServer:
    """Start the live viewer server in a background thread and return it.

    Args:
        trace: Trace to visualize. Defaults to the active/default trace.
        port: Port to listen on.
        open_browser: If True, attempt to open the viewer in the default browser.

    Returns:
        The running :class:`ThreadingHTTPServer` (call ``shutdown()`` to stop).
    """
    if trace is None:
        from pylier.recorder import resolve_trace

        trace = resolve_trace()

    server = _make_server(trace, port)

    thread = threading.Thread(target=server.serve_forever, daemon=True, name="pylier-viewer")
    thread.start()

    url = f"http://localhost:{port}"
    print(f"pylier viewer: {url}")
    if open_browser:
        _try_open(url)

    return server


def _make_server(trace: Trace, port: int) -> ThreadingHTTPServer:
    captured_trace = trace

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt: str, *args: Any) -> None:  # silence default logging
            pass

        def _send(self, body: bytes, content_type: str, status: int = HTTPStatus.OK) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:  # noqa: N802 - http.server API
            if self.path in ("/", "/index.html"):
                self._send(build_html(captured_trace).encode("utf-8"), "text/html; charset=utf-8")
            elif self.path == "/graph":
                self._send(
                    json.dumps(captured_trace.to_graph_dict(), default=str).encode("utf-8"),
                    "application/json",
                )
            else:
                self._send(b"not found", "text/plain", HTTPStatus.NOT_FOUND)

    return ThreadingHTTPServer(("127.0.0.1", port), Handler)


def _try_open(url: str) -> None:
    import sys
    import webbrowser

    try:
        webbrowser.open(url)
    except Exception:  # pragma: no cover - best effort
        print(f"open manually: {url}", file=sys.stderr)
