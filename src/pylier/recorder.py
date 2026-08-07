"""Recorder core: active-trace context, capture level handling, and the
edge-inference engine that runs inside ``@node`` wrappers.

Design notes
------------
* The active trace is a ``ContextVar`` so ``pylier.trace()`` blocks (and async
  tasks spawned from them) get an isolated graph. Outside any block, a lazy
  process-default trace is used so decoration "just works" like logfire.
* Capture level filters *before* instrumentation: nodes above the active level
  are called with zero overhead and never recorded, so uncaptured nodes can't
  create phantom edges.
* Edge inference: on node exit we fingerprint the return value and register
  ``fp -> this_node``. On node entry we fingerprint each positional/kw arg and
  link any matching prior producer. This is the only place values are
  fingerprinted; everything downstream (sidecar, viewer) consumes resolved
  edges.
"""

from __future__ import annotations

import contextlib
import contextvars
import functools
import inspect
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, TypeVar

from pylier.fingerprint import fingerprint, preview_of, size_of, type_name
from pylier.model import Edge, Event, Level, Node, Trace

__all__ = [
    "NodeMeta",
    "record_call",
    "current_trace",
    "default_trace",
    "mark_last_trace",
    "resolve_trace",
    "use_trace",
    "reset_trace",
    "set_level",
    "level_context",
    "current_level",
]

T = TypeVar("T")

_active_trace: contextvars.ContextVar[Trace | None] = contextvars.ContextVar(
    "pylier_active_trace", default=None
)
_level_override: contextvars.ContextVar[Level | None] = contextvars.ContextVar(
    "pylier_level_override", default=None
)

_default_trace: Trace | None = None
# most recently entered trace context, so ``pylier.render()`` / ``serve()``
# called *after* a ``with pylier.trace(...)`` block renders that block's trace
# instead of the (empty) process default.
_last_trace: Trace | None = None


@dataclass(frozen=True)
class NodeMeta:
    """Declared metadata for a ``@node``-decorated callable."""

    id: str
    name: str
    module: str
    level: Level
    tags: dict[str, str]


def current_trace() -> Trace:
    """Return the active trace for this context, falling back to the default."""
    trace = _active_trace.get()
    if trace is not None:
        return trace
    return default_trace()


def default_trace() -> Trace:
    """Return (creating if needed) the process-wide default trace."""
    global _default_trace
    if _default_trace is None:
        _default_trace = Trace("default")
    return _default_trace


def mark_last_trace(trace: Trace) -> None:
    """Remember the most recently entered trace context (for post-block render)."""
    global _last_trace
    _last_trace = trace


def resolve_trace(trace: Trace | None = None) -> Trace:
    """Pick the trace to render/serve: explicit arg, else last, else default."""
    if trace is not None:
        return trace
    if _last_trace is not None:
        return _last_trace
    return default_trace()


def use_trace(trace: Trace | None) -> contextvars.Token:
    """Set the active trace for the current context (use in ``trace()``)."""
    return _active_trace.set(trace)


def reset_trace(token: contextvars.Token) -> None:
    """Restore the active trace previously set by :func:`use_trace`."""
    _active_trace.reset(token)


def current_level() -> Level:
    """Effective capture level: per-context override or the global default."""
    override = _level_override.get()
    if override is not None:
        return override
    from pylier.config import get_settings

    return get_settings().level


def set_level(level: Level | str) -> contextvars.Token:
    """Override the capture level for the current context.

    Returns a token; pass to :func:`reset_level` to restore. Intended for
    ``with``-style usage via :func:`level_context` in most cases.
    """
    return _level_override.set(_coerce_level(level))


def reset_level(token: contextvars.Token) -> None:
    _level_override.reset(token)


def level_context(level: Level | str):
    """Context manager temporarily setting the capture level."""
    import contextlib

    @contextlib.contextmanager
    def _cm():
        token = set_level(level)
        try:
            yield
        finally:
            reset_level(token)

    return _cm()


def _coerce_level(level: Level | str) -> Level:
    return Level[level.upper()] if isinstance(level, str) else Level(level)


def _make_node(meta: NodeMeta) -> Node:
    return Node(
        id=meta.id,
        name=meta.name,
        module=meta.module,
        level=meta.level,
        tags=dict(meta.tags),
    )


def record_enter(trace: Trace, meta: NodeMeta, args: tuple, kwargs: dict) -> None:
    """Register the node call and infer inbound edges from arg fingerprints."""
    trace.get_or_create_node(_make_node(meta))
    level = current_level()
    for arg in (*args, *kwargs.values()):
        fp = fingerprint(arg)
        source_id = trace.lookup_source(fp)
        if source_id is None or source_id == meta.id:
            continue
        trace.add_edge(
            source_id,
            meta.id,
            payload_type=type_name(arg),
            size=size_of(arg) if level >= Level.INFO else None,
            preview=preview_of(arg) if level >= Level.DEBUG else None,
            tags=meta.tags,
        )
    trace.events.append(Event(ts=time.time(), node_id=meta.id, kind="enter", fingerprint=None))


def record_exit(trace: Trace, meta: NodeMeta, result: Any, exc: BaseException | None) -> None:
    """Register the return value (for downstream inference) and emit the exit."""
    if exc is not None:
        trace.events.append(Event(ts=time.time(), node_id=meta.id, kind="exit", fingerprint=None))
        _emit(trace, meta, result=None, return_type=None)
        return
    return_type = type_name(result)
    fp = fingerprint(result)
    trace.register_return(fp, meta.id)
    trace.events.append(
        Event(ts=time.time(), node_id=meta.id, kind="exit", fingerprint=fp, return_type=return_type)
    )
    _emit(trace, meta, result=result, return_type=return_type)


def _emit(trace: Trace, meta: NodeMeta, result: Any, return_type: str | None) -> None:
    """Push the latest node + its inbound edges to all sinks (sidecar/viewer)."""
    if not trace.sinks:
        return
    level = current_level()
    # Emit a compact, replayable event: the node and every edge into it.
    edges_out: list[dict[str, Any]] = []
    for (_src, tgt), edge in trace.edges.items():
        if tgt == meta.id:
            edges_out.append(_edge_dict(edge))
    event = {
        "ts": time.time(),
        "node_id": meta.id,
        "name": meta.name,
        "module": meta.module,
        "level": int(level),
        "return_type": return_type,
        "result_preview": preview_of(result)
        if level >= Level.DEBUG and result is not None
        else None,
        "edges": edges_out,
    }
    for sink in trace.sinks:
        # sinks must never break the recording run
        with contextlib.suppress(Exception):
            sink(event)


def _edge_dict(edge: Edge) -> dict[str, Any]:
    return {
        "source": edge.source,
        "target": edge.target,
        "payload": edge.payload_type,
        "size": edge.size,
        "preview": edge.preview,
        "tags": edge.tags,
        "count": edge.count,
    }


def record_call[T](meta: NodeMeta, func: Callable[..., T], args: tuple, kwargs: dict) -> T:
    """Wrap a sync call with enter/exit recording when the node is captured."""
    if meta.level > current_level():
        return func(*args, **kwargs)
    trace = current_trace()
    record_enter(trace, meta, args, kwargs)
    result: Any = None
    exc: BaseException | None = None
    try:
        result = func(*args, **kwargs)
        return result  # type: ignore[return-value]
    except BaseException as caught:
        exc = caught
        raise
    finally:
        record_exit(trace, meta, result, exc)


async def record_call_async[T](
    meta: NodeMeta, func: Callable[..., Awaitable[T]], args: tuple, kwargs: dict
) -> T:
    """Async counterpart of :func:`record_call`."""
    if meta.level > current_level():
        return await func(*args, **kwargs)
    trace = current_trace()
    record_enter(trace, meta, args, kwargs)
    result: Any = None
    exc: BaseException | None = None
    try:
        result = await func(*args, **kwargs)
        return result  # type: ignore[return-value]
    except BaseException as caught:
        exc = caught
        raise
    finally:
        record_exit(trace, meta, result, exc)


def make_meta(func: Callable[..., Any], level: Level, tags: dict[str, str]) -> NodeMeta:
    module = getattr(func, "__module__", "unknown") or "unknown"
    name = getattr(func, "__qualname__", getattr(func, "__name__", "anonymous"))
    node_id = f"{module}.{name}"
    return NodeMeta(id=node_id, name=name, module=module, level=level, tags=tags)


def node_decorator(
    func: Callable[..., Any] | None = None,
    *,
    level: Level | str = Level.INFO,
    **tags: str,
) -> Any:
    """Decorate a sync/async callable as a pipeline node.

    Args:
        level: Per-node capture level. ``"core"`` nodes record even at the
            lowest global level; ``"trace"`` only when verbosity is maxed.
        **tags: Free-form metadata attached to the node and its inbound edges.
            Use ``payload_kind="trigger"`` to drive edge stroke style in the
            renderer (data/trigger/job/telemetry/code).
    """
    resolved_level = _coerce_level(level)

    def wrap(fn: Callable[..., Any]) -> Callable[..., Any]:
        meta = make_meta(fn, resolved_level, tags)
        if inspect.iscoroutinefunction(fn):

            @functools.wraps(fn)
            async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
                return await record_call_async(meta, fn, args, kwargs)

            async_wrapper.pylier_meta = meta  # type: ignore[attr-defined]
            return async_wrapper

        @functools.wraps(fn)
        def sync_wrapper(*args: Any, **kwargs: Any) -> Any:
            return record_call(meta, fn, args, kwargs)

        sync_wrapper.pylier_meta = meta  # type: ignore[attr-defined]
        return sync_wrapper

    if func is not None and callable(func):
        return wrap(func)
    return wrap
