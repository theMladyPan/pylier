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
from types import CodeType, FrameType
from typing import Any, cast, overload

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
_decorated_code_objects: set[CodeType] = set()


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


@dataclass(frozen=True)
class _NodeRollbackSnapshot:
    node: Node
    calls: int
    last_ms: float | None
    avg_ms: float | None


@dataclass(frozen=True)
class _EdgeRollbackSnapshot:
    edge: Edge
    payload_type: str
    size: int | None
    preview: str | None
    payload_types: tuple[str, ...]
    count: int
    value: str | None
    metadata: dict[str, Any]
    handoff_count: int


# this helper is used to determine enter rollback restoration in record_call,
# record_call_async, and pylier.autotrace during runtime
def _snapshot_node(node: Node | None) -> _NodeRollbackSnapshot | None:
    if node is None:
        return None
    return _NodeRollbackSnapshot(node=node, calls=node.calls, last_ms=node.last_ms, avg_ms=node.avg_ms)


def _restore_node(snapshot: _NodeRollbackSnapshot) -> None:
    snapshot.node.calls = snapshot.calls
    snapshot.node.last_ms = snapshot.last_ms
    snapshot.node.avg_ms = snapshot.avg_ms


def _snapshot_edge(edge: Edge | None) -> _EdgeRollbackSnapshot | None:
    if edge is None:
        return None
    return _EdgeRollbackSnapshot(
        edge=edge,
        payload_type=edge.payload_type,
        size=edge.size,
        preview=edge.preview,
        payload_types=edge.payload_types,
        count=edge.count,
        value=edge.value,
        metadata=dict(edge.metadata),
        handoff_count=len(edge.handoffs),
    )


def _restore_edge(snapshot: _EdgeRollbackSnapshot) -> None:
    edge = snapshot.edge
    edge.payload_type = snapshot.payload_type
    edge.size = snapshot.size
    edge.preview = snapshot.preview
    edge.payload_types = snapshot.payload_types
    edge.count = snapshot.count
    edge.value = snapshot.value
    edge.metadata.clear()
    edge.metadata.update(snapshot.metadata)
    del edge.handoffs[snapshot.handoff_count :]


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


def use_trace(trace: Trace | None) -> contextvars.Token[Trace | None]:
    """Set the active trace for the current context (use in ``trace()``)."""
    return _active_trace.set(trace)


def reset_trace(token: contextvars.Token[Trace | None]) -> None:
    """Restore the active trace previously set by :func:`use_trace`."""
    _active_trace.reset(token)


def current_level() -> Level:
    """Effective capture level: per-context override or the global default."""
    override = _level_override.get()
    if override is not None:
        return override
    from pylier.config import get_settings

    return get_settings().level


def set_level(level: Level | str) -> contextvars.Token[Level | None]:
    """Override the capture level for the current context.

    Returns a token; pass to :func:`reset_level` to restore. Intended for
    ``with``-style usage via :func:`level_context` in most cases.
    """
    return _level_override.set(_coerce_level(level))


def reset_level(token: contextvars.Token[Level | None]) -> None:
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
    values: tuple[Any, ...], arguments: dict[str, Any], level: Level, capture: bool
) -> dict[str, Any]:
    """Summarize one inbound handoff without losing single-value type detail."""
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


def current_execution_stack() -> tuple[InvocationFrame, ...]:
    """Return the active decorated/autotraced invocation stack for this context."""
    return _execution_stack.get()


def use_execution_stack(
    stack: tuple[InvocationFrame, ...],
) -> contextvars.Token[tuple[InvocationFrame, ...]]:
    """Replace the active invocation stack for this context."""
    return _execution_stack.set(stack)


def push_execution_frame(frame: InvocationFrame) -> contextvars.Token[tuple[InvocationFrame, ...]]:
    """Push one active invocation frame onto the execution stack."""
    return use_execution_stack((*_execution_stack.get(), frame))


def reset_execution_frame(token: contextvars.Token[tuple[InvocationFrame, ...]]) -> None:
    """Restore the execution stack saved by :func:`push_execution_frame`."""
    _execution_stack.reset(token)


def _record_enter_arguments(
    trace: Trace,
    meta: NodeMeta,
    arguments: dict[str, Any],
    values: tuple[Any, ...],
) -> contextvars.Token[tuple[InvocationFrame, ...]]:
    """Register a call, preferring its direct caller over value lineage.

    A nested decorated call is an authoritative handoff from its active caller.
    At the top level, the trace root is the implicit orchestration caller.
    Fingerprints remain available to ``derive()`` but never bypass either
    execution boundary in the default graph.
    """
    caller_stack = _execution_stack.get()
    caller = caller_stack[-1] if caller_stack else None
    level = current_level()
    capture = _capture_values_enabled()
    handoff_details = _argument_handoff_details(values, arguments, level, capture)
    producer_handoffs: list[tuple[str, str | None, str, str, Any]] = []
    # Data Flow intentionally observes every matching argument even when an
    # active caller provides the authoritative Application Flow handoff.
    for parameter_name, argument_value in arguments.items():
        if argument_value is None:
            continue
        for producer_id, producer_invocation_id, provenance in trace.lookup_producers(fingerprint(argument_value)):
            producer_handoffs.append((producer_id, producer_invocation_id, provenance, parameter_name, argument_value))

    application_source = caller.node_id if caller is not None else trace.root_node_id
    application_edge_key = (application_source, meta.id)
    data_edge_keys = {
        (producer_id, meta.id)
        for producer_id, _producer_invocation_id, _provenance, _parameter_name, _argument_value in producer_handoffs
    }
    node_snapshot = _snapshot_node(trace.nodes.get(meta.id))
    application_edge_snapshot = _snapshot_edge(trace.edges.get(application_edge_key))
    data_edge_snapshots = {edge_key: _snapshot_edge(trace.data_edges.get(edge_key)) for edge_key in data_edge_keys}
    invocation_sequence_before = trace._invocation_sequence
    graph_version_before = trace.graph_version
    exec_version_before = trace.exec_version
    events_len_before = len(trace.events)
    enter_payload: dict[str, str] | None = None
    payload_max_invocations: int | None = None
    payload_max_bytes: int | None = None
    if capture:
        from pylier.config import get_settings

        settings = get_settings()
        enter_payload = {"arguments": serialize_value(arguments, limit=None), "result": ""}
        payload_max_invocations = settings.payload_max_invocations
        payload_max_bytes = settings.payload_max_bytes

    invocation_id: str | None = None
    execution_token: contextvars.Token[tuple[InvocationFrame, ...]] | None = None

    def rollback() -> None:
        with trace._cond:
            if node_snapshot is None:
                trace.nodes.pop(meta.id, None)
            else:
                trace.nodes[meta.id] = node_snapshot.node
                _restore_node(node_snapshot)
            if invocation_id is not None:
                if stored_payload := trace._invocation_payloads.pop(invocation_id, None):
                    trace._payload_bytes -= stored_payload[1]
                trace.invocations.pop(invocation_id, None)
            trace.edges.pop(application_edge_key, None)
            if application_edge_snapshot is not None:
                trace.edges[application_edge_key] = application_edge_snapshot.edge
                _restore_edge(application_edge_snapshot)
            for edge_key, edge_snapshot in data_edge_snapshots.items():
                trace.data_edges.pop(edge_key, None)
                if edge_snapshot is not None:
                    trace.data_edges[edge_key] = edge_snapshot.edge
                    _restore_edge(edge_snapshot)
            del trace.events[events_len_before:]
            trace._invocation_sequence = invocation_sequence_before
            trace.graph_version = graph_version_before
            trace.exec_version = exec_version_before

    try:
        trace.get_or_create_node(_make_node(meta))
        invocation_id = trace.next_invocation_id()
        execution_token = push_execution_frame(InvocationFrame(meta.id, invocation_id))
        trace.create_invocation(invocation_id, meta.id, caller.invocation_id if caller else None, list(arguments))
        fired: list[dict[str, str]] = []
        for producer_id, producer_invocation_id, provenance, parameter_name, argument_value in producer_handoffs:
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
        if enter_payload is not None and payload_max_invocations is not None and payload_max_bytes is not None:
            # Store the retained full invocation payload only after every
            # fallible enter mutation succeeds so rollback stays O(1) in the
            # size of the retained payload FIFO.
            trace.store_invocation_payload(
                invocation_id,
                enter_payload,
                payload_max_invocations,
                payload_max_bytes,
            )
        return execution_token
    except Exception:
        if execution_token is not None:
            with contextlib.suppress(Exception):
                reset_execution_frame(execution_token)
        rollback()
        raise


def record_enter(
    trace: Trace, meta: NodeMeta, args: tuple[Any, ...], kwargs: dict[str, Any]
) -> contextvars.Token[tuple[InvocationFrame, ...]]:
    arguments = dict(zip(meta.parameter_names, args, strict=False))
    arguments.update(kwargs)
    values = (*args, *kwargs.values())
    return _record_enter_arguments(trace, meta, arguments, values)


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
        trace.complete_invocation(
            current_invocation.invocation_id if current_invocation else None,
            duration_ms=ms,
            result_type=None,
            result_size=None,
            result_preview=None,
            exception=repr(exc),
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
    # Computed unconditionally: the nested-return data edge and the
    # completion preview below both use `level`, and a recursive decorated
    # call (caller.node_id == meta.id) skips the application-flow edge block
    # above — without this hoist `level` would be unbound there.
    level = current_level()
    # ``return_target`` is always a non-None node id (the caller, or the trace
    # root when this is a top-level call); only the self-loop is skipped.
    if return_target != meta.id:
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
    trace.complete_invocation(
        current_invocation.invocation_id if current_invocation else None,
        duration_ms=ms,
        result_type=return_type,
        result_size=size_of(result),
        result_preview=preview_of(result) if level >= Level.DEBUG else None,
        exception=None,
    )
    if _capture_values_enabled():
        from pylier.config import get_settings

        settings = get_settings()
        invocation_id = current_invocation.invocation_id if current_invocation else None
        _state, payload = trace.invocation_payload(invocation_id) if invocation_id else ("missing", None)
        trace.store_invocation_payload(
            invocation_id,
            {"arguments": (payload or {}).get("arguments", "{}"), "result": serialize_value(result, limit=None)},
            settings.payload_max_invocations,
            settings.payload_max_bytes,
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


def record_call[T](meta: NodeMeta, func: Callable[..., T], args: tuple[Any, ...], kwargs: dict[str, Any]) -> T:
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
        reset_execution_frame(execution_token)


async def record_call_async[T](
    meta: NodeMeta, func: Callable[..., Awaitable[T]], args: tuple[Any, ...], kwargs: dict[str, Any]
) -> T:
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
        reset_execution_frame(execution_token)


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


def make_frame_meta(
    frame: FrameType,
    level: Level,
    tags: tuple[str, ...] = (),
    *,
    parameter_names: tuple[str, ...] | None = None,
) -> NodeMeta:
    """Build node metadata from a live Python frame for autotrace."""
    code = frame.f_code
    module = frame.f_globals.get("__name__", "unknown") or "unknown"
    name = getattr(code, "co_qualname", code.co_name)
    if parameter_names is None:
        parameter_names = tuple(frame_arguments(frame))
    return NodeMeta(
        id=f"{module}.{name}",
        name=name,
        module=module,
        level=level,
        tags=tags,
        parameter_names=parameter_names,
        is_async=bool(code.co_flags & (inspect.CO_COROUTINE | inspect.CO_ASYNC_GENERATOR)),
    )


def frame_arguments(frame: FrameType) -> dict[str, Any]:
    """Return bound business arguments from a live frame.

    Conventional ``self`` / ``cls`` parameters stay implicit so autotrace uses
    the same empty-call rules the public API documents.
    """
    arg_info = inspect.getargvalues(frame)
    arguments: dict[str, Any] = {}
    for parameter_name in arg_info.args:
        if parameter_name in {"self", "cls"}:
            continue
        arguments[parameter_name] = arg_info.locals[parameter_name]
    if arg_info.varargs:
        varargs = arg_info.locals.get(arg_info.varargs, ())
        if varargs:
            arguments[arg_info.varargs] = varargs
    if arg_info.keywords:
        keyword_arguments = arg_info.locals.get(arg_info.keywords, {})
        for keyword_name, keyword_value in keyword_arguments.items():
            arguments[keyword_name] = keyword_value
    return arguments


def is_decorated_code(code: CodeType) -> bool:
    """Return whether ``code`` belongs to an explicit ``@pylier.node`` wrapper target."""
    return code in _decorated_code_objects


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


@overload
def node_decorator[**P, R](func: Callable[P, R], /) -> Callable[P, R]: ...


@overload
def node_decorator[**P, R](
    *,
    level: Level | str = Level.INFO,
    _tags: Sequence[str] = (),
) -> Callable[[Callable[P, R]], Callable[P, R]]: ...


def node_decorator[**P, R](
    func: Callable[P, R] | None = None,
    *,
    level: Level | str = Level.INFO,
    _tags: Sequence[str] = (),
) -> Callable[P, R] | Callable[[Callable[P, R]], Callable[P, R]]:
    """Decorate a sync/async callable as a pipeline node.

    The wrapped callable preserves its original parameter and return types
    (via :class:`inspect.Parameter`-level ``ParamSpec``), so IDE autocompletion
    and call-site type checks work unchanged after ``@pylier.node``.

    Args:
        level: Per-node capture level. ``"core"`` nodes record even at the
            lowest global level; ``"trace"`` only when verbosity is maxed.
        _tags: Logfire-style labels attached to this node. They support node
            inspection and client-side node filtering; inferred edges stay
            tag-less.
    """
    resolved_level = _coerce_level(level)
    normalized_tags = _normalize_tags(_tags)

    def wrap(fn: Callable[P, R]) -> Callable[P, R]:
        code = getattr(fn, "__code__", None)
        if isinstance(code, CodeType):
            _decorated_code_objects.add(code)
        meta = make_meta(fn, resolved_level, normalized_tags)
        if inspect.iscoroutinefunction(fn):
            # ParamSpec preserves the call signature; the casts below only
            # satisfy this single generic implementation signature. The public
            # overloads above define the user-facing type, which is exact.
            @functools.wraps(fn)
            async def async_wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
                return cast("R", await record_call_async(meta, fn, args, kwargs))

            # Attach declared metadata for the declared-but-uncalled node
            # registry fast-follow; setattr keeps the wrapper's typed surface.
            # B010: setattr (not direct assignment) avoids a type-checker error on
            # the functools.wraps _Wrapped type while attaching node metadata.
            setattr(async_wrapper, "pylier_meta", meta)  # noqa: B010
            return cast("Callable[P, R]", async_wrapper)

        @functools.wraps(fn)
        def sync_wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
            return record_call(meta, fn, args, kwargs)

        # See the async branch for why setattr is used here too.
        setattr(sync_wrapper, "pylier_meta", meta)  # noqa: B010
        return sync_wrapper

    if func is not None and callable(func):
        return wrap(func)
    return wrap
