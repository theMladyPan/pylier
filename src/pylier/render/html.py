"""Render core: turn a :class:`~pylier.model.Trace` into a self-contained HTML
document using the D3 v7 PoC stack.

Both the static ``pylier.render()`` path and the live viewer server use this
module so the graph looks identical in tests and in live preview.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pylier.model import Trace

__all__ = ["build_html", "render_to_file"]

_TEMPLATE_PATH = Path(__file__).parent / "template.html"
_DATA_TOKEN = "__PYLIER_GRAPH__"
_EMBED_TOKEN = "__PYLIER_GRAPH_JSON__"
_LIVE_TOKEN = "__PYLIER_LIVE__"
_PAYLOAD_TOKEN = "__PYLIER_INVOCATION_PAYLOADS__"
_NAME_TOKEN = "{{NAME}}"


def build_html(
    trace: Trace,
    *,
    history: Any | None = None,
    live: bool = False,
    embed_payloads: bool = False,
) -> str:
    """Return a complete HTML string for ``trace``.

    The graph JSON is injected twice: once as a JS object for the renderer to
    read immediately, and once in a ``<script type="application/json">`` tag as a
    ``file://`` fallback (mirroring the PoC's offline-open behavior).

    Args:
        trace: Primary trace to render.
        history: Optional retained trace history for the live viewer.
        live: Whether the local in-process viewer serves the document.
        embed_payloads: Whether to embed retained full invocation values in the
            output. The resulting file exposes them to every reader.

    Returns:
        A complete HTML document.
    """
    template = _TEMPLATE_PATH.read_text(encoding="utf-8")
    graph = history.to_view_dict() if history is not None else trace.to_graph_dict()
    data_json = json.dumps(graph, default=str, ensure_ascii=False)
    payload_json = _inline_json(_payload_bundle(trace, history) if embed_payloads else {})
    html = (
        template.replace(_DATA_TOKEN, data_json)
        .replace(_EMBED_TOKEN, data_json)
        .replace(_LIVE_TOKEN, str(live).lower())
        .replace(_PAYLOAD_TOKEN, payload_json)
        .replace(_NAME_TOKEN, _escape(trace.name))
    )
    return html


def render_to_file(
    trace: Trace,
    path: str | Path,
    *,
    history: Any | None = None,
    embed_payloads: bool = False,
) -> Path:
    """Write ``trace`` as a self-contained HTML file and return its path.

    Args:
        trace: Primary trace to render.
        path: Output HTML path.
        history: Optional retained trace history.
        embed_payloads: Whether to include retained full invocation values.

    Returns:
        The written HTML path.
    """
    out = Path(path)
    out.write_text(build_html(trace, history=history, embed_payloads=embed_payloads), encoding="utf-8")
    return out


def _payload_bundle(trace: Trace, history: Any | None) -> dict[str, dict[str, str]]:
    """Collect retained full payloads for an explicit static debug bundle."""
    traces = history.snapshot_traces() if history is not None else (trace,)
    payloads: dict[str, dict[str, str]] = {}
    for current_trace in traces:
        for invocation_id in current_trace.invocations:
            state, payload = current_trace.invocation_payload(invocation_id)
            if state == "available" and payload is not None:
                payloads[invocation_id] = payload
    return payloads


def _inline_json(value: object) -> str:
    """Encode JSON safely for insertion into an inline script."""
    return json.dumps(value, ensure_ascii=False).replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")


def _escape(text: str) -> str:
    import html as _html

    return _html.escape(text, quote=False)
