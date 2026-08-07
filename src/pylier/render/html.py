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
_NAME_TOKEN = "{{NAME}}"


def build_html(trace: Trace, *, graph: dict[str, Any] | None = None) -> str:
    """Return a complete HTML string for ``trace``.

    The graph JSON is injected twice: once as a JS object for the renderer to
    read immediately, and once in a ``<script type="application/json">`` tag as a
    ``file://`` fallback (mirroring the PoC's offline-open behavior).
    """
    template = _TEMPLATE_PATH.read_text(encoding="utf-8")
    graph = graph if graph is not None else trace.to_graph_dict()
    data_json = json.dumps(graph, default=str, ensure_ascii=False)
    html = (
        template.replace(_DATA_TOKEN, data_json)
        .replace(_EMBED_TOKEN, data_json)
        .replace(_NAME_TOKEN, _escape(str(graph.get("name", "trace"))))
    )
    return html


def render_to_file(trace: Trace, path: str | Path) -> Path:
    """Write ``trace`` as a self-contained HTML file and return its path."""
    out = Path(path)
    out.write_text(build_html(trace), encoding="utf-8")
    return out


def _escape(text: str) -> str:
    import html as _html

    return _html.escape(text, quote=False)


def graph_dict(trace: Trace) -> dict[str, Any]:
    """Expose the renderer's JSON shape (used by the live server's poll API)."""
    return trace.to_graph_dict()
