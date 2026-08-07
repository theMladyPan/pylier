"""Live viewer server.

A tiny stdlib-only HTTP server that runs in a background thread and serves the
current in-memory trace. The served page subscribes to ``/events`` (Server-Sent
Events) and re-renders the D3 graph in place whenever the trace changes — no
fixed-interval polling, so a stable pipeline produces zero redundant renders
and a growing pipeline updates smoothly without cold-restarting the force
layout (the layout itself is persistent on the client; see render/template).

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

# SSE heartbeat cadence: if nothing changed for this long, send a comment line
# to keep the HTTP connection alive through proxies/timeouts.
_SSE_HEARTBEAT = 15.0


def serve(trace: Trace | None = None, port: int = 8765, *, open_browser: bool = True) -> ThreadingHTTPServer:
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
        # keep SSE handler threads from blocking shutdown
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt: str, *args: Any) -> None:  # silence default logging
            pass

        def _send(self, body: bytes, content_type: str, status: int = HTTPStatus.OK) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:  # noqa: N802 - http.server API
            if self.path in ("/", "/index.html"):
                self._send(build_html(captured_trace).encode("utf-8"), "text/html; charset=utf-8")
            elif self.path == "/graph":
                # kept for debugging / non-SSE clients; the page itself uses /events
                self._send(
                    json.dumps(captured_trace.to_graph_dict(), default=str).encode("utf-8"),
                    "application/json",
                )
            elif self.path == "/events":
                self._handle_sse()
            else:
                self._send(b"not found", "text/plain", HTTPStatus.NOT_FOUND)

        def _handle_sse(self) -> None:
            """Stream the graph as SSE whenever the trace version advances."""
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "keep-alive")
            self.end_headers()
            last = -1  # forces an immediate full-graph send on connect
            try:
                while True:
                    version = captured_trace.wait_for_change(last, timeout=_SSE_HEARTBEAT)
                    if version > last:
                        payload = json.dumps(captured_trace.to_graph_dict(), default=str)
                        self.wfile.write(f"event: graph\ndata: {payload}\n\n".encode())
                        self.wfile.flush()
                        last = version
                    else:
                        # no change within the heartbeat window: keep alive
                        self.wfile.write(b": heartbeat\n\n")
                        self.wfile.flush()
            except BrokenPipeError, ConnectionResetError:
                # client closed the tab / navigated away
                pass

    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    # allow quick restart after a crash, and don't let SSE threads block shutdown
    server.allow_reuse_address = True
    server.daemon_threads = True
    return server


def _try_open(url: str) -> None:
    import sys
    import webbrowser

    try:
        webbrowser.open(url)
    except Exception:  # pragma: no cover - best effort
        print(f"open manually: {url}", file=sys.stderr)
