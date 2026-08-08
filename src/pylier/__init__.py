"""pylier — decorator-driven data process & pipeline visualization.

Decorate functions as pipeline nodes; pylier infers edges from the data that
flows between them and renders a force-directed graph (D3 v7, single-file HTML)
or a live in-process viewer. API mirrors logfire's flat, decoration-first feel:

    import pylier

    @pylier.node
    def load(path: str) -> Document: ...

    @pylier.node(_tags=["ocr", "document"])
    def ocr(img) -> str: ...

    with pylier.trace("ingest"):
        text = ocr(load("doc.pdf"))
    pylier.render("out.html")          # static single-file
    pylier.serve()                      # live in-process viewer
"""

from __future__ import annotations

import contextlib
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Any

from pylier.model import Edge, Event, Level, Node, Trace
from pylier.recorder import (
    derive_value as _derive_value,
)
from pylier.recorder import (
    mark_last_trace,
    register_trace,
    reset_trace,
    resolve_trace,
    use_trace,
)
from pylier.recorder import (
    node_decorator as _node_decorator,
)
from pylier.render import build_html, render_to_file
from pylier.server import serve

__all__ = [
    "node",
    "derive",
    "trace",
    "render",
    "serve",
    "level",
    "set_level",
    "build_html",
    "Level",
    "Trace",
    "Node",
    "Edge",
    "Event",
    "__version__",
]

__version__ = "0.1.0"


def derive[T](value: T, *, from_: Iterable[object]) -> T:
    """Preserve declared runtime lineage for a computed plain value.

    Use this after an expression such as ``title + body`` when its contributing
    values came from decorated nodes. It returns ``value`` unchanged, but a
    later decorated consumer receives an inferred edge from every resolved
    source. Unknown sources emit a :class:`RuntimeWarning` and are ignored.

    Args:
        value: The computed value to return unchanged.
        from_: Values that contributed to ``value``.

    Returns:
        The original ``value``.

    Raises:
        TypeError: If ``from_`` is a string/bytes value or is not iterable.
    """
    return _derive_value(value, from_=from_)


def node(
    func: Any = None,
    *,
    level: Level | str = Level.INFO,
    _tags: Sequence[str] = (),
) -> Any:
    """Decorate a sync or async function/method as a pipeline node.

    Args:
        level: Per-node capture level ("core" | "info" | "debug" | "trace").
            The node is recorded only when the active global level is at least
            this verbose.
        _tags: Logfire-style labels attached to the node for inspection and
            client-side filtering. Inferred edges deliberately have no tags.
    """
    return _node_decorator(func, level=level, _tags=_tags)


@contextlib.contextmanager
def trace(name: str = "trace", *, sidecar: bool | str | Path = False):
    """Context manager recording an isolated trace for the block.

    Args:
        name: Trace name shown in the viewer header.
        sidecar: If truthy, also write resolved events to a JSONL sidecar for
            offline replay / cross-process consumers. ``True`` uses the
            configured ``sidecar_path``; a path str/Path writes there directly.

    Yields:
        The :class:`Trace` for the block.
    """
    t = register_trace(Trace(name=name))
    if sidecar:
        _attach_sidecar(t, sidecar)
    mark_last_trace(t)
    token = use_trace(t)
    try:
        yield t
    finally:
        reset_trace(token)


def render(
    path: str | Path = "pylier.html",
    trace: Trace | None = None,
    *,
    embed_payloads: bool = False,
) -> Path:
    """Render a trace to a self-contained HTML file.

    Args:
        path: Output HTML path.
        trace: Trace to render; defaults to the current/default trace.
        embed_payloads: If True, embed retained full invocation inputs and
            outputs in the HTML. Use only for intentionally shareable data.

    Returns:
        The written file path.
    """
    if trace is None:
        trace = resolve_trace()
        from pylier.recorder import trace_history

        return render_to_file(trace, path, history=trace_history(), embed_payloads=embed_payloads)
    return render_to_file(trace, path, embed_payloads=embed_payloads)


def set_level(level: Level | str):
    """Return a context manager temporarily setting the capture level.

    Example::

        with pylier.set_level("debug"):
            ...
    """
    from pylier.recorder import level_context

    return level_context(level)


# keep `level` as an alias users may find more natural
level = set_level


def _attach_sidecar(t: Trace, sidecar: bool | str | Path) -> None:
    from pylier.tracing.sidecar import SidecarBackend

    if isinstance(sidecar, bool):
        from pylier.config import get_settings

        settings = get_settings()
        if settings.sidecar_path is None:
            raise ValueError("sidecar=True requires PYLIER_SIDECAR_PATH to be configured")
        t.sinks.append(SidecarBackend(settings.sidecar_path, settings.sidecar_name))
    else:
        path = Path(sidecar)
        if path.is_dir():
            t.sinks.append(SidecarBackend(path))
        else:
            t.sinks.append(SidecarBackend(path.parent, path.name))
