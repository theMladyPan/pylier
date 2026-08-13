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
from collections.abc import Callable, Iterable, Sequence
from contextlib import AbstractContextManager
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _pkg_version
from pathlib import Path
from typing import cast, overload

from pylier.autotrace import autotrace
from pylier.model import Edge, Event, Invocation, Level, Node, Trace, TraceHistory
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
    "autotrace",
    "trace",
    "render",
    "serve",
    "level",
    "set_level",
    "build_html",
    "render_to_file",
    "instrument_fastapi",
    "Level",
    "Trace",
    "TraceHistory",
    "Node",
    "Edge",
    "Event",
    "Invocation",
    "__version__",
]

# Single source of truth is [project].version in pyproject.toml; the CI
# pipeline (uv version, uv publish) reads it from there. Derive at runtime so
# the two never drift — no manual sync on version bumps.
try:
    __version__ = _pkg_version("pylier")
except PackageNotFoundError:  # running from a source tree that isn't installed
    __version__ = "0.0.0"


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


@overload
def node[**P, R](func: Callable[P, R], /) -> Callable[P, R]: ...


@overload
def node[**P, R](
    *,
    level: Level | str = Level.INFO,
    _tags: Sequence[str] = (),
) -> Callable[[Callable[P, R]], Callable[P, R]]: ...


def node[**P, R](
    func: Callable[P, R] | None = None,
    *,
    level: Level | str = Level.INFO,
    _tags: Sequence[str] = (),
) -> Callable[P, R] | Callable[[Callable[P, R]], Callable[P, R]]:
    """Decorate a sync or async function/method as a pipeline node.

    The decorated callable keeps its original parameter names, keyword
    arguments, and return type, so IDE autocompletion and call-site type
    checks work unchanged.

    Args:
        func: Function or method to instrument (bare ``@pylier.node`` form).
        level: Per-node capture level ("core" | "info" | "debug" | "trace").
            The node is recorded only when the active global level is at least
            this verbose.
        _tags: Logfire-style labels attached to the node for inspection and
            client-side filtering. Inferred edges deliberately have no tags.
    """
    # Map to the two documented forms so the overload signatures stay exact:
    # bare ``@pylier.node`` (defaults) vs. ``@pylier.node(level=..., _tags=...)``.
    # The cast carries node's own P/R across the delegation boundary — ty can't
    # unify ParamSpecs across two generic functions, but the overload signatures
    # above define the exact user-facing type, so this is sound.
    if func is None:
        # ty can't unify ParamSpecs across this keyword-only delegation; the
        # cast carries node's own P/R. The overload signatures above define the
        # exact user-facing type, so this is sound.
        return cast(
            "Callable[P, R] | Callable[[Callable[P, R]], Callable[P, R]]",
            _node_decorator(level=level, _tags=_tags),
        )
    return _node_decorator(func)


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
        t.finish()
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


def set_level(level: Level | str) -> AbstractContextManager[None]:
    """Return a context manager temporarily setting the capture level.

    Example::

        with pylier.set_level("debug"):
            ...
    """
    from pylier.recorder import level_context

    return level_context(level)


# keep `level` as an alias users may find more natural
level = set_level


def instrument_fastapi(app: object) -> object:
    """Install per-request pylier tracing on a FastAPI/Starlette app.

    Lazy-imports ``pylier.integrations.fastapi`` so importing ``pylier`` never
    pulls in FastAPI. The integration lives behind the ``[fastapi]`` extra.

    Args:
        app: A FastAPI or Starlette application.

    Returns:
        The same ``app`` (the middleware is registered in place).

    Raises:
        ImportError: If FastAPI is not installed (``uv add pylier[fastapi]``).
    """
    from pylier.integrations.fastapi import instrument_fastapi as _impl

    return _impl(app)


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
