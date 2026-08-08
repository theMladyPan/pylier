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
from urllib.parse import unquote

from pylier.model import Trace, TraceHistory
from pylier.render import build_html

__all__ = ["serve"]

# SSE heartbeat cadence: if nothing changed for this long, send a comment line
# to keep the HTTP connection alive through proxies/timeouts.
_SSE_HEARTBEAT = 15.0


def serve(trace: Trace | None = None, port: int = 8765, *, open_browser: bool = True) -> ThreadingHTTPServer:
    """Start the live viewer server in a background thread and return it.

    Args:
        trace: Trace to visualize alone. When omitted, show the retained in-process
            history with the newest trace selected.
        port: Port to listen on.
        open_browser: If True, attempt to open the viewer in the default browser.

    Returns:
        The running :class:`ThreadingHTTPServer` (call ``shutdown()`` to stop).
    """
    if trace is None:
        from pylier.recorder import trace_history

        source: Trace | TraceHistory = trace_history()
    else:
        source = trace

    server = _make_server(source, port)

    thread = threading.Thread(target=server.serve_forever, daemon=True, name="pylier-viewer")
    thread.start()

    url = f"http://localhost:{port}"
    print(f"pylier viewer: {url}")
    if open_browser:
        _try_open(url)

    return server


def _make_server(trace: Trace | TraceHistory, port: int) -> ThreadingHTTPServer:
    captured_trace = trace

    def graph_payload() -> dict:
        if isinstance(captured_trace, TraceHistory):
            return captured_trace.to_view_dict()
        return captured_trace.to_graph_dict()

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
                initial_trace = (
                    next(iter(captured_trace.traces.values()), Trace())
                    if isinstance(captured_trace, TraceHistory)
                    else captured_trace
                )
                self._send(
                    build_html(
                        initial_trace, history=captured_trace if isinstance(captured_trace, TraceHistory) else None
                    ).encode("utf-8"),
                    "text/html; charset=utf-8",
                )
            elif self.path == "/graph":
                # kept for debugging / non-SSE clients; the page itself uses /events
                self._send(
                    json.dumps(graph_payload(), default=str).encode("utf-8"),
                    "application/json",
                )
            elif self.path == "/events":
                self._handle_sse()
            elif self.path.startswith("/invocations/") and self.path.endswith("/payload"):
                self._handle_invocation_payload()
            else:
                self._send(b"not found", "text/plain", HTTPStatus.NOT_FOUND)

        def _handle_invocation_payload(self) -> None:
            """Return one retained full payload only after inspector expansion."""
            parts = self.path.split("/")
            if len(parts) != 5:
                self._send(b"not found", "text/plain", HTTPStatus.NOT_FOUND)
                return
            trace_id, invocation_id = unquote(parts[2]), unquote(parts[3])
            if isinstance(captured_trace, TraceHistory):
                trace = captured_trace.traces.get(trace_id)
            else:
                trace = captured_trace if captured_trace.id == trace_id else None
            if trace is None:
                self._send(b"trace not found", "text/plain", HTTPStatus.NOT_FOUND)
                return
            state, payload = trace.invocation_payload(invocation_id)
            if state == "missing":
                self._send(b"invocation not found", "text/plain", HTTPStatus.NOT_FOUND)
            elif state == "disabled":
                self._send(b"full values were not captured", "text/plain", HTTPStatus.CONFLICT)
            elif state == "evicted":
                self._send(b"full payload was evicted", "text/plain", HTTPStatus.GONE)
            else:
                self._send(
                    json.dumps({"invocation_id": invocation_id, **(payload or {})}).encode(),
                    "application/json; charset=utf-8",
                )

        def _handle_sse(self) -> None:
            """Stream graph topology (rare) and execution events (real-time).

            Emits ``event: graph`` with the full graph whenever the topology
            version advances (new node/edge), and ``event: exec`` with a batch
            of enter/exit events whenever the execution version advances. The
            client uses graph events to (re)join nodes/links and exec events to
            drive the fire/pulse animation and call-count badges.
            """
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "keep-alive")
            self.end_headers()
            graph_since = -1  # forces an immediate full-graph send on connect
            exec_version_since = 0
            event_index_since = 0
            history_version = -1
            try:
                while True:
                    if isinstance(captured_trace, TraceHistory):
                        history_version = captured_trace.wait_for_change(history_version, timeout=_SSE_HEARTBEAT)
                        graph_v, exec_v = history_version, 0
                    else:
                        graph_v, exec_v = captured_trace.wait_for_change(
                            graph_since, exec_version_since, timeout=_SSE_HEARTBEAT
                        )
                    wrote = False
                    if graph_v > graph_since:
                        payload = json.dumps(graph_payload(), default=str)
                        self.wfile.write(f"event: graph\ndata: {payload}\n\n".encode())
                        graph_since = graph_v
                        wrote = True
                    if not isinstance(captured_trace, TraceHistory) and exec_v > exec_version_since:
                        event_index_since, new_events = captured_trace.events_since(event_index_since)
                        # Latency updates advance the execution version without adding a
                        # timeline event. Advance its cursor either way; otherwise the
                        # server would spin forever emitting empty ``exec`` batches.
                        exec_version_since = exec_v
                        if new_events:
                            batch = json.dumps(
                                [
                                    {
                                        "ts": ev.ts,
                                        "node_id": ev.node_id,
                                        "kind": ev.kind,
                                        "edges": ev.edges,
                                    }
                                    for ev in new_events
                                ],
                                default=str,
                            )
                            self.wfile.write(f"event: exec\ndata: {batch}\n\n".encode())
                            wrote = True
                    if wrote:
                        self.wfile.flush()
                    else:
                        # nothing changed within the heartbeat window: keep alive
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
