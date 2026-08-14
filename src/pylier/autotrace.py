"""Process-global autotracing built on ``sys.monitoring``.

This keeps pylier's flat API and recorder semantics while auto-instrumenting
public Python callables inside the inferred application scope.
"""

from __future__ import annotations

import contextlib
import contextvars
import inspect
import math
import re
import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from types import CodeType, FrameType
from typing import Any, Literal

from pylier.config import get_settings
from pylier.model import PHASE_ARGUMENTS, Event, Invocation, Level, Node, Trace
from pylier.recorder import (
    InvocationFrame,
    NodeMeta,
    _record_enter_arguments,
    _sink_event,
    current_execution_stack,
    current_level,
    current_trace,
    frame_arguments,
    is_decorated_code,
    make_frame_meta,
    record_exit,
    reset_execution_frame,
    reset_trace,
    use_execution_stack,
    use_trace,
)

__all__ = ["autotrace"]

_MODULE_NAME_RE = re.compile(r"^[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*$")
_CAPTURE_MODE = Literal["warmup", "direct", "buffered"]


@dataclass(frozen=True)
class _Scope:
    module_prefixes: tuple[str, ...] = ()
    source_root: Path | None = None


@dataclass(frozen=True)
class _Config:
    allow_empty: bool
    min_exec_time: float
    filter_prefix: str | None
    scope: _Scope


@dataclass
class _CodeState:
    promoted: bool
    signature: inspect.Signature | None = None
    signature_loaded: bool = False


@dataclass
class _FrameState:
    mode: _CAPTURE_MODE
    meta: NodeMeta
    arguments: dict[str, Any]
    outer_trace: Trace | None
    staged_trace: Trace | None
    start_perf: float
    anchor_node_id: str | None
    anchor_invocation_id: str | None
    code_flags: int
    owner_execution_stack: tuple[InvocationFrame, ...] = ()
    invocation_frame: InvocationFrame | None = None
    execution_token: contextvars.Token[tuple[InvocationFrame, ...]] | None = None
    trace_token: contextvars.Token[Trace | None] | None = None
    yielded_business_value: bool = False
    active: bool = False


@dataclass
class _RuntimeState:
    tool_id: int
    config: _Config
    code_states: dict[CodeType, _CodeState] = field(default_factory=dict)
    frame_states: dict[int, _FrameState] = field(default_factory=dict)


_state: _RuntimeState | None = None


def autotrace(
    *,
    allow_empty: bool = False,
    min_exec_time: float | None = None,
    filter_prefix: str | None = None,
    modules: str | Sequence[str] | None = None,
) -> None:
    """Install process-global pylier autotracing for public Python callables.

    Args:
        allow_empty: Keep successful no-meaningful-input ``-> None`` calls
            instead of buffering and omitting them. Parameters whose runtime
            value equals their declared default are treated as omitted for this
            filter; use ``allow_empty=True`` if you need those calls retained.
        min_exec_time: Promotion threshold in seconds. ``None`` and ``0``
            capture immediately; positive values time warm-up calls until one
            reaches the threshold, then trace later calls.
        filter_prefix: Skip callables whose simple declared name starts with
            this prefix.
        modules: Optional module-name scope override. Each entry matches that
            exact module and its submodules.

    Raises:
        RuntimeError: If autotrace is already active with different settings or
            the runtime lacks ``sys.monitoring`` support.
        TypeError: If an argument has the wrong type.
        ValueError: If an argument has an invalid value.
    """
    global _state

    monitoring = _require_runtime_support()
    config = _normalize_config(
        allow_empty=allow_empty,
        min_exec_time=min_exec_time,
        filter_prefix=filter_prefix,
        modules=modules,
        caller_frame=_caller_frame(),
    )
    if _state is not None:
        if _state.config == config:
            return
        raise RuntimeError("pylier.autotrace is already active with a different configuration")

    tool_id = _reserve_tool_id(monitoring)
    runtime = _RuntimeState(tool_id=tool_id, config=config)
    try:
        mask = 0
        for event_name, callback in _CALLBACKS.items():
            event = getattr(monitoring.events, event_name)
            monitoring.register_callback(tool_id, event, callback)
            mask |= event
        monitoring.set_events(tool_id, mask)
        _state = runtime
    except Exception:
        _disable_runtime(monitoring, tool_id)
        raise


def _normalize_config(
    *,
    allow_empty: bool,
    min_exec_time: float | None,
    filter_prefix: str | None,
    modules: str | Sequence[str] | None,
    caller_frame: FrameType,
) -> _Config:
    if not isinstance(allow_empty, bool):
        raise TypeError("allow_empty must be a bool")
    if filter_prefix is not None and not isinstance(filter_prefix, str):
        raise TypeError("filter_prefix must be a string or None")
    if filter_prefix == "":
        raise ValueError("filter_prefix must not be empty")
    return _Config(
        allow_empty=allow_empty,
        min_exec_time=_normalize_min_exec_time(min_exec_time),
        filter_prefix=filter_prefix,
        scope=_normalize_scope(modules, caller_frame),
    )


def _normalize_min_exec_time(value: float | None) -> float:
    if value is None:
        return 0.0
    if isinstance(value, bool):
        raise TypeError("min_exec_time must be a float or None")
    seconds = float(value)
    if not math.isfinite(seconds) or seconds < 0:
        raise ValueError("min_exec_time must be a finite number >= 0")
    return seconds


def _normalize_scope(modules: str | Sequence[str] | None, caller_frame: FrameType) -> _Scope:
    if modules is not None:
        if isinstance(modules, (bytes, bytearray)) or not isinstance(modules, (str, Sequence)):
            raise TypeError("modules must be a module name or sequence of module names")
        names = [modules] if isinstance(modules, str) else list(modules)
        if not names:
            raise ValueError("modules must not be empty")
        normalized = sorted({_normalize_module_name(name) for name in names})
        return _Scope(module_prefixes=tuple(normalized))

    package_name = caller_frame.f_globals.get("__package__") or ""
    if package_name:
        return _Scope(module_prefixes=(package_name.split(".", 1)[0],))

    filename = caller_frame.f_code.co_filename
    if filename and not filename.startswith("<"):
        return _Scope(source_root=Path(filename).resolve().parent)
    return _Scope(module_prefixes=("__main__",))


def _normalize_module_name(name: object) -> str:
    if not isinstance(name, str):
        raise TypeError("modules must contain only strings")
    if not name or not _MODULE_NAME_RE.fullmatch(name):
        raise ValueError("modules must contain valid Python module names")
    return name


def _resolve_frame_signature(code_state: _CodeState, frame: FrameType) -> inspect.Signature | None:
    if code_state.signature_loaded:
        return code_state.signature
    code_state.signature_loaded = True
    callable_object = _resolve_frame_callable(frame)
    if callable_object is None:
        return None
    with contextlib.suppress(TypeError, ValueError):
        code_state.signature = inspect.signature(callable_object)
    return code_state.signature


def _resolve_frame_callable(frame: FrameType) -> Any | None:
    code = frame.f_code
    qualname = getattr(code, "co_qualname", code.co_name)
    parts = qualname.split(".")
    if not parts or "<locals>" in parts:
        return None
    current_object = frame.f_globals.get(parts[0])
    if current_object is None:
        return None
    for part in parts[1:]:
        try:
            current_object = inspect.getattr_static(current_object, part)
        except AttributeError:
            return None
    if isinstance(current_object, (staticmethod, classmethod)):
        current_object = current_object.__func__
    elif isinstance(current_object, property):
        current_object = current_object.fget
    if getattr(current_object, "__code__", None) is not code:
        return None
    return current_object if callable(current_object) else None


def _matches_default_value(value: object, default: object) -> bool:
    # ponytail: frame locals cannot distinguish omitted defaults from an
    # explicit call that passed the default value across sync/coroutine/
    # generator frames; add call-site capture only if that precision matters
    # enough to justify a larger engine.
    try:
        matched = value == default
    except Exception:
        return value is default
    return matched if isinstance(matched, bool) else value is default


def _autotrace_arguments(frame: FrameType, code_state: _CodeState) -> dict[str, Any]:
    signature = _resolve_frame_signature(code_state, frame)
    if signature is None:
        return frame_arguments(frame)

    arguments: dict[str, Any] = {}
    local_values = frame.f_locals
    for parameter_name, parameter in signature.parameters.items():
        if parameter_name in {"self", "cls"}:
            continue
        if parameter.kind is inspect.Parameter.VAR_POSITIONAL:
            varargs = tuple(local_values.get(parameter_name, ()))
            if varargs:
                arguments[parameter_name] = varargs
            continue
        if parameter.kind is inspect.Parameter.VAR_KEYWORD:
            keyword_arguments = local_values.get(parameter_name, {})
            for keyword_name, keyword_value in keyword_arguments.items():
                arguments[keyword_name] = keyword_value
            continue
        if parameter_name not in local_values:
            continue
        parameter_value = local_values[parameter_name]
        if parameter.default is not inspect.Parameter.empty and _matches_default_value(
            parameter_value,
            parameter.default,
        ):
            continue
        arguments[parameter_name] = parameter_value
    return arguments


def _record_trace(frame_state: _FrameState) -> Trace | None:
    return frame_state.staged_trace or frame_state.outer_trace


def _activate_frame(frame_state: _FrameState) -> bool:
    if frame_state.active:
        return True
    if frame_state.invocation_frame is None:
        return False
    try:
        trace = _record_trace(frame_state)
        if trace is not None:
            frame_state.trace_token = use_trace(trace)
        frame_state.execution_token = use_execution_stack(
            frame_state.owner_execution_stack or (frame_state.invocation_frame,)
        )
        frame_state.active = True
        return True
    except Exception:
        _deactivate_frame(frame_state)
        raise


def _is_business_yield(frame_state: _FrameState, value: object) -> bool:
    if frame_state.code_flags & inspect.CO_ASYNC_GENERATOR:
        return type(value).__name__ == "async_generator_wrapped_value"
    return bool(frame_state.code_flags & inspect.CO_GENERATOR)


def _handle_py_start(code: CodeType, _instruction_offset: int) -> None:
    runtime = _state
    if runtime is None:
        return
    frame = _callback_frame(code)
    if frame is None:
        return
    try:
        _start_frame(runtime, frame)
    except Exception:
        runtime.frame_states.pop(id(frame), None)


def _handle_py_resume(code: CodeType, _instruction_offset: int) -> None:
    with contextlib.suppress(Exception):
        _resume_frame(code)


def _handle_py_throw(code: CodeType, _instruction_offset: int, _value: object) -> None:
    with contextlib.suppress(Exception):
        _resume_frame(code)


def _handle_py_yield(code: CodeType, _instruction_offset: int, value: object) -> None:
    runtime = _state
    if runtime is None:
        return
    frame = _callback_frame(code)
    if frame is None:
        return
    frame_state = runtime.frame_states.get(id(frame))
    if frame_state is None or not frame_state.active:
        return
    try:
        if _is_business_yield(frame_state, value):
            frame_state.yielded_business_value = True
    except Exception:
        pass
    finally:
        _deactivate_frame(frame_state)


def _handle_py_return(code: CodeType, _instruction_offset: int, value: object) -> None:
    with contextlib.suppress(Exception):
        _finish_frame(code, value, None)


def _handle_py_unwind(code: CodeType, _instruction_offset: int, value: object) -> None:
    with contextlib.suppress(Exception):
        _finish_frame(code, None, value if isinstance(value, BaseException) else RuntimeError(repr(value)))


def _start_frame(runtime: _RuntimeState, frame: FrameType) -> None:
    if current_level() < Level.INFO or not _should_trace_frame(frame, runtime.config):
        return
    code_state = runtime.code_states.setdefault(
        frame.f_code,
        _CodeState(promoted=runtime.config.min_exec_time == 0.0),
    )
    arguments = _autotrace_arguments(frame, code_state)
    meta = make_frame_meta(frame, Level.INFO, parameter_names=tuple(arguments))
    start_perf = time.perf_counter()
    frame_key = id(frame)
    code_flags = frame.f_code.co_flags
    if runtime.config.min_exec_time > 0 and not code_state.promoted:
        runtime.frame_states[frame_key] = _FrameState(
            mode="warmup",
            meta=meta,
            arguments=arguments,
            outer_trace=None,
            staged_trace=None,
            start_perf=start_perf,
            anchor_node_id=None,
            anchor_invocation_id=None,
            code_flags=code_flags,
        )
        return

    outer_trace = current_trace()
    anchor_node_id, anchor_invocation_id = _anchor_for_current_context(outer_trace)
    values = tuple(arguments.values())
    if not runtime.config.allow_empty and not arguments:
        staged_trace = Trace(name=outer_trace.name)
        trace_token = use_trace(staged_trace)
        try:
            execution_token = _record_enter_arguments(staged_trace, meta, arguments, values)
        except Exception:
            reset_trace(trace_token)
            raise
        owner_execution_stack = current_execution_stack()
        invocation_frame = owner_execution_stack[-1]
        runtime.frame_states[frame_key] = _FrameState(
            mode="buffered",
            meta=meta,
            arguments=arguments,
            outer_trace=outer_trace,
            staged_trace=staged_trace,
            start_perf=start_perf,
            anchor_node_id=anchor_node_id,
            anchor_invocation_id=anchor_invocation_id,
            code_flags=code_flags,
            owner_execution_stack=owner_execution_stack,
            invocation_frame=invocation_frame,
            execution_token=execution_token,
            trace_token=trace_token,
            active=True,
        )
        return

    execution_token = _record_enter_arguments(outer_trace, meta, arguments, values)
    owner_execution_stack = current_execution_stack()
    invocation_frame = owner_execution_stack[-1]
    runtime.frame_states[frame_key] = _FrameState(
        mode="direct",
        meta=meta,
        arguments=arguments,
        outer_trace=outer_trace,
        staged_trace=None,
        start_perf=start_perf,
        anchor_node_id=anchor_node_id,
        anchor_invocation_id=anchor_invocation_id,
        code_flags=code_flags,
        owner_execution_stack=owner_execution_stack,
        invocation_frame=invocation_frame,
        execution_token=execution_token,
        active=True,
    )


def _resume_frame(code: CodeType) -> None:
    runtime = _state
    if runtime is None:
        return
    frame = _callback_frame(code)
    if frame is None:
        return
    frame_state = runtime.frame_states.get(id(frame))
    if frame_state is None or frame_state.mode == "warmup" or frame_state.active:
        return
    _activate_frame(frame_state)


def _finish_frame(code: CodeType, value: object | None, exc: BaseException | None) -> None:
    runtime = _state
    if runtime is None:
        return
    frame = _callback_frame(code)
    if frame is None:
        return
    frame_state = runtime.frame_states.pop(id(frame), None)
    if frame_state is None:
        return
    try:
        elapsed_ms = (time.perf_counter() - frame_state.start_perf) * 1000.0
        if frame_state.mode == "warmup":
            if elapsed_ms / 1000.0 >= runtime.config.min_exec_time:
                runtime.code_states[code].promoted = True
            return
        if not frame_state.active and not _activate_frame(frame_state):
            return
        trace = _record_trace(frame_state)
        if trace is None:
            return
        record_exit(trace, frame_state.meta, value, exc, elapsed_ms)
        _deactivate_frame(frame_state)
        if (
            frame_state.mode == "buffered"
            and frame_state.outer_trace is not None
            and frame_state.staged_trace is not None
        ):
            _merge_buffered_trace(
                frame_state.outer_trace,
                frame_state.staged_trace,
                keep_parent=exc is not None or value is not None or frame_state.yielded_business_value,
                omitted_invocation_id=frame_state.invocation_frame.invocation_id
                if frame_state.invocation_frame
                else None,
                omitted_node_id=frame_state.meta.id,
                anchor_node_id=frame_state.anchor_node_id or frame_state.outer_trace.root_node_id,
                anchor_invocation_id=frame_state.anchor_invocation_id,
            )
    finally:
        _deactivate_frame(frame_state)


def _deactivate_frame(frame_state: _FrameState) -> None:
    if frame_state.execution_token is not None:
        with contextlib.suppress(Exception):
            reset_execution_frame(frame_state.execution_token)
        frame_state.execution_token = None
    if frame_state.trace_token is not None:
        with contextlib.suppress(Exception):
            reset_trace(frame_state.trace_token)
        frame_state.trace_token = None
    frame_state.active = False


def _should_trace_frame(frame: FrameType, config: _Config) -> bool:
    code = frame.f_code
    if is_decorated_code(code) or not (code.co_flags & inspect.CO_OPTIMIZED):
        return False
    simple_name = code.co_name
    if simple_name.startswith("_") or simple_name.startswith("<"):
        return False
    if config.filter_prefix is not None and simple_name.startswith(config.filter_prefix):
        return False
    module_name = frame.f_globals.get("__name__", "") or ""
    if module_name == "pylier.autotrace" or module_name.startswith("pylier."):
        return False
    if config.scope.module_prefixes:
        return any(
            module_name == prefix or module_name.startswith(f"{prefix}.") for prefix in config.scope.module_prefixes
        )
    if config.scope.source_root is None:
        return False
    filename = code.co_filename
    if not filename or filename.startswith("<"):
        return False
    try:
        Path(filename).resolve().relative_to(config.scope.source_root)
        return True
    except ValueError:
        return False


def _anchor_for_current_context(trace: Trace) -> tuple[str, str | None]:
    stack = current_execution_stack()
    if stack:
        caller = stack[-1]
        return caller.node_id, caller.invocation_id
    return trace.root_node_id, None


def _callback_frame(code: CodeType) -> FrameType | None:
    for depth in range(1, 6):
        with contextlib.suppress(ValueError):
            frame = sys._getframe(depth)
            if frame.f_code is code:
                return frame
    return None


def _caller_frame() -> FrameType:
    return sys._getframe(2)


def _require_runtime_support() -> Any:
    monitoring = getattr(sys, "monitoring", None)
    if monitoring is None or not hasattr(sys, "_getframe"):
        raise RuntimeError("pylier.autotrace requires sys.monitoring and sys._getframe at runtime")
    return monitoring


def _reserve_tool_id(monitoring: Any) -> int:
    for tool_id in (3, 4):
        if monitoring.get_tool(tool_id) is None:
            monitoring.use_tool_id(tool_id, "pylier.autotrace")
            return tool_id
    raise RuntimeError("pylier.autotrace could not reserve a free sys.monitoring tool ID")


# Defined after the handlers; ``_EVENT_NAMES`` is derived so a rename updates
# one place (used by ``_disable_runtime`` for callback deregistration).
_CALLBACKS: dict[str, Callable[..., None]] = {
    "PY_START": _handle_py_start,
    "PY_RESUME": _handle_py_resume,
    "PY_THROW": _handle_py_throw,
    "PY_RETURN": _handle_py_return,
    "PY_YIELD": _handle_py_yield,
    "PY_UNWIND": _handle_py_unwind,
}
_EVENT_NAMES = tuple(_CALLBACKS)


def _disable_runtime(monitoring: Any, tool_id: int) -> None:
    with contextlib.suppress(Exception):
        monitoring.set_events(tool_id, 0)
    for event_name in _EVENT_NAMES:
        with contextlib.suppress(Exception):
            monitoring.register_callback(tool_id, getattr(monitoring.events, event_name), None)
    with contextlib.suppress(Exception):
        monitoring.free_tool_id(tool_id)


def _merge_buffered_trace(
    target_trace: Trace,
    staged_trace: Trace,
    *,
    keep_parent: bool,
    omitted_invocation_id: str | None,
    omitted_node_id: str,
    anchor_node_id: str,
    anchor_invocation_id: str | None,
) -> None:
    omitted_ids = {omitted_invocation_id} if omitted_invocation_id and not keep_parent else set()
    invocation_id_map: dict[str, str] = {}

    for staged_invocation_id, invocation in staged_trace.invocations.items():
        if staged_invocation_id in omitted_ids:
            continue
        invocation_id_map[staged_invocation_id] = target_trace.next_invocation_id()
        staged_node = staged_trace.nodes[invocation.node_id]
        target_trace.get_or_create_node(
            Node(
                id=staged_node.id,
                name=staged_node.name,
                module=staged_node.module,
                level=staged_node.level,
                tags=staged_node.tags,
                is_async=staged_node.is_async,
            )
        )
        if invocation.duration_ms is not None:
            target_trace.record_latency(invocation.node_id, invocation.duration_ms)
        _store_invocation(
            target_trace,
            invocation,
            invocation_id_map[staged_invocation_id],
            _translate_parent_invocation_id(
                invocation.parent_invocation_id, invocation_id_map, omitted_ids, anchor_invocation_id
            ),
        )

    for edge in staged_trace.edges.values():
        for handoff in edge.handoffs:
            if omitted_ids and handoff.get("invocation_id") in omitted_ids:
                continue
            source = target_trace.root_node_id if edge.source == staged_trace.root_node_id else edge.source
            target = target_trace.root_node_id if edge.target == staged_trace.root_node_id else edge.target
            translated_handoff = dict(handoff)
            translated_handoff["invocation_id"] = _translate_invocation_id(
                translated_handoff.get("invocation_id"),
                invocation_id_map,
            )
            translated_handoff["parent_invocation_id"] = _translate_parent_invocation_id(
                translated_handoff.get("parent_invocation_id"),
                invocation_id_map,
                omitted_ids,
                anchor_invocation_id,
            )
            if omitted_ids and handoff.get("parent_invocation_id") in omitted_ids:
                if edge.metadata.get("phase") == PHASE_ARGUMENTS:
                    source = anchor_node_id
                else:
                    target = anchor_node_id
            target_trace.add_edge(
                source,
                target,
                payload_type=edge.payload_type,
                size=edge.size,
                preview=edge.preview,
                payload_types=edge.payload_types,
                value=edge.value,
                metadata=dict(edge.metadata),
                handoff=translated_handoff,
            )

    for edge in staged_trace.data_edges.values():
        for handoff in edge.handoffs:
            if omitted_ids and (
                handoff.get("producer_invocation_id") in omitted_ids
                or handoff.get("consumer_invocation_id") in omitted_ids
            ):
                continue
            translated_handoff = dict(handoff)
            translated_handoff["producer_invocation_id"] = _translate_invocation_id(
                translated_handoff.get("producer_invocation_id"),
                invocation_id_map,
            )
            translated_handoff["consumer_invocation_id"] = _translate_invocation_id(
                translated_handoff.get("consumer_invocation_id"),
                invocation_id_map,
            )
            target_trace.add_data_edge(
                edge.source,
                edge.target,
                payload_type=edge.payload_type,
                size=edge.size,
                preview=edge.preview,
                payload_types=edge.payload_types,
                value=edge.value,
                metadata=dict(edge.metadata),
                handoff=translated_handoff,
            )

    for fingerprint, (node_id, invocation_id) in staged_trace._fp_index.items():
        if omitted_ids and invocation_id in omitted_ids:
            continue
        target_trace.register_return(fingerprint, node_id, _translate_invocation_id(invocation_id, invocation_id_map))

    for fingerprint, source_ids in staged_trace._derived_sources.items():
        kept_sources = tuple(
            source_id for source_id in source_ids if not (omitted_ids and source_id == omitted_node_id)
        )
        if kept_sources:
            target_trace.register_derived_sources(fingerprint, kept_sources)

    for event in staged_trace.events:
        if event.invocation_id in omitted_ids:
            continue
        translated_edges = [dict(edge) for edge in event.edges]
        for translated_edge in translated_edges:
            if translated_edge.get("source") == staged_trace.root_node_id:
                translated_edge["source"] = target_trace.root_node_id
            if translated_edge.get("target") == staged_trace.root_node_id:
                translated_edge["target"] = target_trace.root_node_id
        if omitted_ids and event.parent_invocation_id in omitted_ids:
            for translated_edge in translated_edges:
                if translated_edge.get("source") == omitted_node_id:
                    translated_edge["source"] = anchor_node_id
        target_trace.record_event(
            Event(
                ts=event.ts,
                node_id=event.node_id,
                kind=event.kind,
                fingerprint=event.fingerprint,
                return_type=event.return_type,
                invocation_id=_translate_invocation_id(event.invocation_id, invocation_id_map),
                parent_invocation_id=_translate_parent_invocation_id(
                    event.parent_invocation_id,
                    invocation_id_map,
                    omitted_ids,
                    anchor_invocation_id,
                ),
                edges=translated_edges,
            )
        )

    _merge_payloads(target_trace, staged_trace, invocation_id_map)
    _emit_buffered_sinks(target_trace, staged_trace, invocation_id_map, omitted_ids)


def _translate_invocation_id(invocation_id: str | None, invocation_id_map: dict[str, str]) -> str | None:
    if invocation_id is None:
        return None
    return invocation_id_map.get(invocation_id, invocation_id)


def _translate_parent_invocation_id(
    parent_invocation_id: str | None,
    invocation_id_map: dict[str, str],
    omitted_ids: set[str],
    anchor_invocation_id: str | None,
) -> str | None:
    if parent_invocation_id in omitted_ids:
        return anchor_invocation_id
    return _translate_invocation_id(parent_invocation_id, invocation_id_map)


def _store_invocation(
    target_trace: Trace, invocation: Invocation, new_id: str, parent_invocation_id: str | None
) -> None:
    # The staged trace is discarded post-merge, so aliasing its field values
    # (replace keeps references) is safe; the arguments list is copied anyway.
    with target_trace._cond:
        target_trace.invocations[new_id] = replace(
            invocation,
            id=new_id,
            parent_invocation_id=parent_invocation_id,
            arguments=list(invocation.arguments),
        )


def _merge_payloads(target_trace: Trace, staged_trace: Trace, invocation_id_map: dict[str, str]) -> None:
    settings = get_settings()
    for staged_invocation_id, new_invocation_id in invocation_id_map.items():
        state, payload = staged_trace.invocation_payload(staged_invocation_id)
        with target_trace._cond:
            target_invocation = target_trace.invocations[new_invocation_id]
            target_invocation.payload_state = state
        if state == "available" and payload is not None:
            target_trace.store_invocation_payload(
                new_invocation_id,
                payload,
                settings.payload_max_invocations,
                settings.payload_max_bytes,
            )


def _emit_buffered_sinks(
    target_trace: Trace,
    staged_trace: Trace,
    invocation_id_map: dict[str, str],
    omitted_ids: set[str],
) -> None:
    if not target_trace.sinks:
        return
    for event in staged_trace.events:
        if event.kind != "exit" or event.invocation_id in omitted_ids:
            continue
        translated_invocation_id = _translate_invocation_id(event.invocation_id, invocation_id_map)
        if translated_invocation_id is None:
            continue
        invocation = target_trace.invocations.get(translated_invocation_id)
        node = target_trace.nodes.get(event.node_id)
        if invocation is None or node is None:
            continue
        sink_event = _sink_event(
            node=node,
            trace=target_trace,
            level=current_level(),
            ts=event.ts,
            return_type=invocation.result_type,
            result_preview=invocation.result_preview,
        )
        for sink in target_trace.sinks:
            with contextlib.suppress(Exception):
                sink(sink_event)


def _reset_for_tests() -> None:
    """Disable autotrace and clear all runtime state for deterministic tests."""
    global _state

    runtime = _state
    if runtime is None:
        return
    for frame_state in tuple(runtime.frame_states.values()):
        _deactivate_frame(frame_state)
    runtime.frame_states.clear()
    monitoring = getattr(sys, "monitoring", None)
    if monitoring is not None:
        _disable_runtime(monitoring, runtime.tool_id)
    _state = None


def _frame_state_count_for_tests() -> int:
    """Return the number of live autotrace frame states."""
    return 0 if _state is None else len(_state.frame_states)
