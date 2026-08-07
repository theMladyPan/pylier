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

__all__ = ["serve", "register_trace", "notify_trace_change"]

# SSE heartbeat cadence: if nothing changed for this long, send a comment line
# to keep the HTTP connection alive through proxies/timeouts.
_SSE_HEARTBEAT = 15.0

_registry_lock = threading.Condition()
_registry: list[Trace] = []
_registry_version = 0


def register_trace(trace: Trace) -> None:
    """Make an independent root trace available as a live viewer tab."""
    global _registry_version
    with _registry_lock:
        if trace not in _registry:
            _registry.append(trace)
            _registry_version += 1
            _registry_lock.notify_all()


def _reset_registry(trace: Trace) -> None:
    """Start a viewer session without stale traces from an earlier server."""
    global _registry_version
    with _registry_lock:
        _registry[:] = [trace]
        _registry_version += 1
        _registry_lock.notify_all()


def notify_trace_change() -> None:
    """Wake SSE clients after a registered trace changes."""
    with _registry_lock:
        _registry_lock.notify_all()


def _trace_tabs() -> list[dict[str, str]]:
    with _registry_lock:
        return [{"id": trace.otel_trace_id or f"local-{id(trace)}", "name": trace.name} for trace in _registry]


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

    _reset_registry(trace)
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
                graph = captured_trace.to_graph_dict()
                graph["tabs"] = _trace_tabs()
                self._send(build_html(captured_trace, graph=graph).encode("utf-8"), "text/html; charset=utf-8")
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
            graph_versions: dict[int, int] = {}
            exec_versions: dict[int, int] = {}
            event_indices: dict[int, int] = {}
            registry_since = -1
            try:
                while True:
                    with _registry_lock:
                        traces = list(_registry)
                        registry_v = _registry_version
                    wrote = False
                    if registry_v > registry_since:
                        self.wfile.write(f"event: tabs\ndata: {json.dumps(_trace_tabs())}\n\n".encode())
                        registry_since = registry_v
                        wrote = True
                    for streamed_trace in traces:
                        key = id(streamed_trace)
                        graph_since = graph_versions.get(key, -1)
                        exec_version_since = exec_versions.get(key, 0)
                        graph_v, exec_v = streamed_trace.wait_for_change(graph_since, exec_version_since, timeout=0)
                        trace_id = streamed_trace.otel_trace_id or f"local-{key}"
                        if graph_v > graph_since:
                            payload = json.dumps(
                                {"trace_id": trace_id, "graph": streamed_trace.to_graph_dict()}, default=str
                            )
                            self.wfile.write(f"event: graph\ndata: {payload}\n\n".encode())
                            graph_versions[key] = graph_v
                            wrote = True
                        if exec_v <= exec_version_since:
                            continue
                        event_index_since = event_indices.get(key, 0)
                        event_index_since, new_events = streamed_trace.events_since(event_index_since)
                        # Latency updates advance the execution version without adding a
                        # timeline event. Advance its cursor either way; otherwise the
                        # server would spin forever emitting empty ``exec`` batches.
                        exec_versions[key] = exec_v
                        event_indices[key] = event_index_since
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
                            event_payload = json.dumps({"trace_id": trace_id, "events": json.loads(batch)})
                            self.wfile.write(f"event: exec\ndata: {event_payload}\n\n".encode())
                            wrote = True
                    if wrote:
                        self.wfile.flush()
                    else:
                        # wait for either trace work or a newly registered tab
                        with _registry_lock:
                            _registry_lock.wait(timeout=_SSE_HEARTBEAT)
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
