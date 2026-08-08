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
* Application Flow uses the invocation stack: a direct caller or trace root
  hands arguments to a node and receives its result.
* Data Flow fingerprints values at node exits and entries. Its producer/consumer
  relations are recorded separately, so lineage never changes call structure.
"""

from __future__ import annotations

import contextlib
import contextvars
import functools
import inspect
import time
import warnings
from collections.abc import Awaitable, Callable, Iterable, Sequence
from dataclasses import dataclass
from typing import Any, TypeVar

from pylier.fingerprint import fingerprint, preview_of, serialize_value, size_of, tuple_member_type_names, type_name
from pylier.model import Edge, Event, Level, Node, Trace, TraceHistory

__all__ = [
    "NodeMeta",
    "record_call",
    "current_trace",
    "default_trace",
    "mark_last_trace",
    "resolve_trace",
    "use_trace",
    "reset_trace",
    "trace_history",
    "register_trace",
    "set_level",
    "level_context",
    "current_level",
    "derive_value",
]

T = TypeVar("T")

_active_trace: contextvars.ContextVar[Trace | None] = contextvars.ContextVar("pylier_active_trace", default=None)
_level_override: contextvars.ContextVar[Level | None] = contextvars.ContextVar("pylier_level_override", default=None)
# Execution scope identifies authoritative direct caller/callee handoffs. Value
# fingerprints are only fallback lineage when no decorated caller is active.
_execution_stack: contextvars.ContextVar[tuple[InvocationFrame, ...]] = contextvars.ContextVar(
    "pylier_execution_stack", default=()
)

_default_trace: Trace | None = None
# most recently entered trace context, so ``pylier.render()`` / ``serve()``
# called *after* a ``with pylier.trace(...)`` block renders that block's trace
# instead of the (empty) process default.
_last_trace: Trace | None = None
_history = TraceHistory()


@dataclass(frozen=True)
class NodeMeta:
    """Declared metadata for a ``@node``-decorated callable."""

    id: str
    name: str
    module: str
    level: Level
    tags: tuple[str, ...]
    parameter_names: tuple[str, ...] = ()
    is_async: bool = False


@dataclass(frozen=True)
class InvocationFrame:
    """One active decorated call, distinct from its reusable function node."""

    node_id: str
    invocation_id: str


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
        _default_trace = register_trace(Trace("default"))
    return _default_trace


def trace_history() -> TraceHistory:
    """Return the in-process trace history used by the live request viewer."""
    return _history


def register_trace(trace: Trace) -> Trace:
    """Retain ``trace`` for live debugging and return it unchanged."""
    return _history.add(trace)


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
        tags=meta.tags,
        is_async=meta.is_async,
    )


def _capture_values_enabled() -> bool:
    from pylier.config import get_settings

    return get_settings().capture_values


def derive_value[T](value: T, *, from_: Iterable[object]) -> T:
    """Associate a computed value with the traced values used to make it.

    Args:
        value: The plain computed value returned unchanged to application code.
        from_: Source values that contributed to ``value``.

    Returns:
        The original ``value``.

    Raises:
        TypeError: If ``from_`` is a string/bytes value or is not iterable.
    """
    if isinstance(from_, (str, bytes)) or not isinstance(from_, Iterable):
        raise TypeError("from_ must be an iterable of source values, not a single string/bytes value")

    trace = current_trace()
    source_ids: list[str] = []
    unknown_source_count = 0
    for source_value in from_:
        resolved_source_ids = trace.lookup_sources(fingerprint(source_value))
        if not resolved_source_ids:
            unknown_source_count += 1
            continue
        for source_id in resolved_source_ids:
            if source_id not in source_ids:
                source_ids.append(source_id)

    if source_ids:
        trace.register_derived_sources(fingerprint(value), tuple(source_ids))
    if unknown_source_count:
        warnings.warn(
            f"pylier.derive() could not resolve {unknown_source_count} declared source"
            f"{'s' if unknown_source_count != 1 else ''}; continuing with known lineage",
            RuntimeWarning,
            stacklevel=2,
        )
    return value


def _argument_handoff_details(
    args: tuple, kwargs: dict, arguments: dict[str, Any], level: Level, capture: bool
) -> dict[str, Any]:
    """Summarize one inbound handoff without losing single-value type detail."""
    values = (*args, *kwargs.values())
    if not values:
        return {"payload_type": "empty", "size": None, "preview": None, "value": None}
    payload = values[0] if len(values) == 1 else arguments
    return {
        "payload_type": type_name(payload) if len(values) == 1 else "arguments",
        "size": size_of(payload) if level >= Level.INFO else None,
        "preview": preview_of(payload) if level >= Level.DEBUG else None,
        "payload_types": tuple_member_type_names(payload),
        "value": serialize_value(payload) if capture else None,
    }


def record_enter(trace: Trace, meta: NodeMeta, args: tuple, kwargs: dict) -> contextvars.Token:
    """Register a call, preferring its direct caller over value lineage.

    A nested decorated call is an authoritative handoff from its active caller.
    At the top level, the trace root is the implicit orchestration caller.
    Fingerprints remain available to ``derive()`` but never bypass either
    execution boundary in the default graph.
    """
    trace.get_or_create_node(_make_node(meta))
    caller_stack = _execution_stack.get()
    caller = caller_stack[-1] if caller_stack else None
    invocation_id = trace.next_invocation_id()
    execution_token = _execution_stack.set((*caller_stack, InvocationFrame(meta.id, invocation_id)))
    level = current_level()
    capture = _capture_values_enabled()
    arguments = dict(zip(meta.parameter_names, args, strict=False))
    arguments.update(kwargs)
    fired: list[dict[str, str]] = []
    handoff_details = _argument_handoff_details(args, kwargs, arguments, level, capture)
    # Data Flow intentionally observes every matching argument even when an
    # active caller provides the authoritative Application Flow handoff.
    for parameter_name, argument_value in arguments.items():
        if argument_value is None:
            continue
        for producer_id, producer_invocation_id, provenance in trace.lookup_producers(fingerprint(argument_value)):
            trace.add_data_edge(
                producer_id,
                meta.id,
                payload_type=type_name(argument_value),
                size=size_of(argument_value) if level >= Level.INFO else None,
                preview=preview_of(argument_value) if level >= Level.DEBUG else None,
                payload_types=tuple_member_type_names(argument_value),
                value=serialize_value(argument_value) if capture else None,
                metadata={"perspective": "data"},
                handoff={
                    "producer_invocation_id": producer_invocation_id,
                    "consumer_invocation_id": invocation_id,
                    "parameter": parameter_name,
                    "provenance": provenance,
                },
            )

    if caller is not None:
        trace.add_edge(
            caller.node_id,
            meta.id,
            **handoff_details,
            metadata={"phase": "arguments"},
            handoff={
                "invocation_id": invocation_id,
                "parent_invocation_id": caller.invocation_id,
                "arguments": list(arguments),
            },
        )
        fired.append({"source": caller.node_id, "target": meta.id})
    else:
        trace.add_edge(
            trace.root_node_id,
            meta.id,
            **handoff_details,
            metadata={"phase": "arguments"},
            handoff={"invocation_id": invocation_id, "arguments": list(arguments)},
        )
        fired.append({"source": trace.root_node_id, "target": meta.id})
    trace.record_event(
        Event(
            ts=time.time(),
            node_id=meta.id,
            kind="enter",
            invocation_id=invocation_id,
            parent_invocation_id=caller.invocation_id if caller else None,
            edges=fired,
        )
    )
    return execution_token


def record_exit(trace: Trace, meta: NodeMeta, result: Any, exc: BaseException | None, ms: float | None = None) -> None:
    """Register the return value (for downstream inference) and emit the exit.

    ``ms`` is the wall-clock duration of this call (when known); it updates the
    node's latest/average latency so the renderer can show ``38 ms`` cards.
    """
    if ms is not None:
        trace.record_latency(meta.id, ms)
    caller_stack = _execution_stack.get()
    current_invocation = caller_stack[-1] if caller_stack else None
    caller = caller_stack[-2] if len(caller_stack) > 1 else None
    return_target = caller.node_id if caller is not None else trace.root_node_id
    if exc is not None:
        if return_target != meta.id:
            trace.add_edge(
                meta.id,
                return_target,
                payload_type="exception",
                metadata={"phase": "exception"},
                handoff={
                    "invocation_id": current_invocation.invocation_id if current_invocation else None,
                    "parent_invocation_id": caller.invocation_id if caller else None,
                },
            )
        trace.record_event(
            Event(
                ts=time.time(),
                node_id=meta.id,
                kind="exit",
                invocation_id=current_invocation.invocation_id if current_invocation else None,
                parent_invocation_id=caller.invocation_id if caller else None,
            )
        )
        _emit(trace, meta, result=None, return_type=None)
        return
    return_type = type_name(result)
    if return_target is not None and return_target != meta.id:
        level = current_level()
        trace.add_edge(
            meta.id,
            return_target,
            payload_type="empty" if result is None else return_type,
            size=size_of(result) if level >= Level.INFO else None,
            preview=preview_of(result) if level >= Level.DEBUG else None,
            value=serialize_value(result) if _capture_values_enabled() else None,
            metadata={"phase": "return"},
            handoff={
                "invocation_id": current_invocation.invocation_id if current_invocation else None,
                "parent_invocation_id": caller.invocation_id if caller else None,
            },
        )
    # A nested return is a real data consumption by its decorated caller even
    # though no second function-entry event occurs for that local assignment.
    if result is not None and caller is not None:
        trace.add_data_edge(
            meta.id,
            caller.node_id,
            payload_type=return_type,
            size=size_of(result) if level >= Level.INFO else None,
            preview=preview_of(result) if level >= Level.DEBUG else None,
            payload_types=tuple_member_type_names(result),
            value=serialize_value(result) if _capture_values_enabled() else None,
            metadata={"perspective": "data"},
            handoff={
                "producer_invocation_id": current_invocation.invocation_id if current_invocation else None,
                "consumer_invocation_id": caller.invocation_id,
                "parameter": "return",
                "provenance": "return",
            },
        )
    fp = fingerprint(result)
    trace.register_return(fp, meta.id, current_invocation.invocation_id if current_invocation else None)
    trace.record_event(
        Event(
            ts=time.time(),
            node_id=meta.id,
            kind="exit",
            fingerprint=fp,
            return_type=return_type,
            invocation_id=current_invocation.invocation_id if current_invocation else None,
            parent_invocation_id=caller.invocation_id if caller else None,
        )
    )
    _emit(trace, meta, result=result, return_type=return_type)


def _emit(trace: Trace, meta: NodeMeta, result: Any, return_type: str | None) -> None:
    """Push the latest node + its inbound edges to all sinks (sidecar/viewer)."""
    if not trace.sinks:
        return
    level = current_level()
    # Emit a compact, replayable event: the node and every edge into it.
    edges_out: list[dict[str, Any]] = []
    data_edges_out: list[dict[str, Any]] = []
    for (_src, tgt), edge in trace.edges.items():
        if tgt == meta.id:
            edges_out.append(_edge_dict(edge))
    for (_src, tgt), edge in trace.data_edges.items():
        if tgt == meta.id:
            data_edges_out.append(_edge_dict(edge))
    event = {
        "ts": time.time(),
        "node_id": meta.id,
        "name": meta.name,
        "module": meta.module,
        "level": int(level),
        "tags": list(meta.tags),
        "return_type": return_type,
        "result_preview": preview_of(result) if level >= Level.DEBUG and result is not None else None,
        "edges": edges_out,
        "data_edges": data_edges_out,
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
        "payload_types": list(edge.payload_types),
        "count": edge.count,
        "value": edge.value,
        "metadata": edge.metadata,
        "handoffs": edge.handoffs,
    }


def record_call[T](meta: NodeMeta, func: Callable[..., T], args: tuple, kwargs: dict) -> T:
    """Wrap a sync call with enter/exit recording when the node is captured."""
    if meta.level > current_level():
        return func(*args, **kwargs)
    trace = current_trace()
    execution_token = record_enter(trace, meta, args, kwargs)
    start = time.perf_counter()
    result: Any = None
    exc: BaseException | None = None
    try:
        result = func(*args, **kwargs)
        return result  # type: ignore[return-value]
    except BaseException as caught:
        exc = caught
        raise
    finally:
        record_exit(trace, meta, result, exc, (time.perf_counter() - start) * 1000.0)
        _execution_stack.reset(execution_token)


async def record_call_async[T](meta: NodeMeta, func: Callable[..., Awaitable[T]], args: tuple, kwargs: dict) -> T:
    """Async counterpart of :func:`record_call`."""
    if meta.level > current_level():
        return await func(*args, **kwargs)
    trace = current_trace()
    execution_token = record_enter(trace, meta, args, kwargs)
    start = time.perf_counter()
    result: Any = None
    exc: BaseException | None = None
    try:
        result = await func(*args, **kwargs)
        return result  # type: ignore[return-value]
    except BaseException as caught:
        exc = caught
        raise
    finally:
        record_exit(trace, meta, result, exc, (time.perf_counter() - start) * 1000.0)
        _execution_stack.reset(execution_token)


def make_meta(func: Callable[..., Any], level: Level, tags: tuple[str, ...]) -> NodeMeta:
    module = getattr(func, "__module__", "unknown") or "unknown"
    name = getattr(func, "__qualname__", getattr(func, "__name__", "anonymous"))
    node_id = f"{module}.{name}"
    return NodeMeta(
        id=node_id,
        name=name,
        module=module,
        level=level,
        tags=tags,
        parameter_names=tuple(inspect.signature(func).parameters),
        is_async=inspect.iscoroutinefunction(func),
    )


def _normalize_tags(tags: Sequence[str]) -> tuple[str, ...]:
    """Validate Logfire-style node tags while preserving caller order."""
    if not isinstance(tags, Sequence) or isinstance(tags, (str, bytes)):
        raise TypeError("_tags must be a sequence of strings, not a string")
    normalized: list[str] = []
    for tag in tags:
        if not isinstance(tag, str):
            raise TypeError("_tags must contain only strings")
        cleaned = tag.strip()
        if not cleaned:
            raise ValueError("_tags must not contain empty strings")
        if cleaned not in normalized:
            normalized.append(cleaned)
    return tuple(normalized)


def node_decorator(
    func: Callable[..., Any] | None = None,
    *,
    level: Level | str = Level.INFO,
    _tags: Sequence[str] = (),
) -> Any:
    """Decorate a sync/async callable as a pipeline node.

    Args:
        level: Per-node capture level. ``"core"`` nodes record even at the
            lowest global level; ``"trace"`` only when verbosity is maxed.
        _tags: Logfire-style labels attached to this node. They support node
            inspection and client-side node filtering; inferred edges stay
            tag-less.
    """
    resolved_level = _coerce_level(level)
    normalized_tags = _normalize_tags(_tags)

    def wrap(fn: Callable[..., Any]) -> Callable[..., Any]:
        meta = make_meta(fn, resolved_level, normalized_tags)
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
