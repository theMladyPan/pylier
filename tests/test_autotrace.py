"""Autotrace behavior: sys.monitoring-driven flat API over recorder semantics."""

from __future__ import annotations

import asyncio
import importlib
import inspect
import json
import math
import queue
import sys
import textwrap
import threading
import types
from pathlib import Path

import pytest

import pylier
from pylier.model import PHASE_EXCEPTION


def _load_autotrace_module():
    try:
        return importlib.import_module("pylier.autotrace")
    except ModuleNotFoundError:
        return None


@pytest.fixture(autouse=True)
def _reset_autotrace_state():
    module = _load_autotrace_module()
    if module is not None:
        module._reset_for_tests()
    yield
    module = _load_autotrace_module()
    if module is not None:
        assert module._frame_state_count_for_tests() == 0
        module._reset_for_tests()


@pytest.fixture
def module_factory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.syspath_prepend(str(tmp_path))
    created_top_levels: set[str] = set()

    def create(files: dict[str, str]) -> dict[str, types.ModuleType]:
        created_modules: dict[str, types.ModuleType] = {}
        for relative_path, source in files.items():
            path = tmp_path / relative_path
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(textwrap.dedent(source), encoding="utf-8")
            created_top_levels.add(relative_path.split("/", 1)[0])
        importlib.invalidate_caches()
        for top_level in created_top_levels:
            for module_name in [name for name in sys.modules if name == top_level or name.startswith(f"{top_level}.")]:
                sys.modules.pop(module_name, None)
        for relative_path in files:
            if not relative_path.endswith(".py") or relative_path.endswith("__init__.py"):
                continue
            module_name = relative_path[:-3].replace("/", ".")
            created_modules[module_name] = importlib.import_module(module_name)
        return created_modules

    yield create

    for top_level in created_top_levels:
        for module_name in [name for name in sys.modules if name == top_level or name.startswith(f"{top_level}.")]:
            sys.modules.pop(module_name, None)
    importlib.invalidate_caches()


def _node_names(trace: pylier.Trace) -> set[str]:
    return {node.name for node in trace.nodes.values()}


def _application_pairs(trace: pylier.Trace) -> set[tuple[str, str]]:
    pairs: set[tuple[str, str]] = set()
    for source, target in trace.edges:
        source_name = "root" if source == trace.root_node_id else trace.nodes[source].name.rsplit(".", 1)[-1]
        target_name = "root" if target == trace.root_node_id else trace.nodes[target].name.rsplit(".", 1)[-1]
        pairs.add((source_name, target_name))
    return pairs


def _data_pairs(trace: pylier.Trace) -> set[tuple[str, str]]:
    pairs: set[tuple[str, str]] = set()
    for source, target in trace.data_edges:
        pairs.add((trace.nodes[source].name.rsplit(".", 1)[-1], trace.nodes[target].name.rsplit(".", 1)[-1]))
    return pairs


def _assert_valid_trace_references(trace: pylier.Trace) -> None:
    valid_invocation_ids = set(trace.invocations)
    valid_node_ids = set(trace.nodes) | {trace.root_node_id}
    for invocation in trace.invocations.values():
        assert invocation.node_id in trace.nodes
        assert invocation.parent_invocation_id is None or invocation.parent_invocation_id in valid_invocation_ids
    for edge in [*trace.edges.values(), *trace.data_edges.values()]:
        assert edge.source in valid_node_ids
        assert edge.target in valid_node_ids
        for handoff in edge.handoffs:
            for key in (
                "invocation_id",
                "parent_invocation_id",
                "producer_invocation_id",
                "consumer_invocation_id",
            ):
                handoff_id = handoff.get(key)
                assert handoff_id is None or handoff_id in valid_invocation_ids
    for event in trace.events:
        assert event.node_id in trace.nodes
        assert event.invocation_id is None or event.invocation_id in valid_invocation_ids
        assert event.parent_invocation_id is None or event.parent_invocation_id in valid_invocation_ids


def test_autotrace_is_exported_with_the_public_signature():
    assert "autotrace" in pylier.__all__
    signature = inspect.signature(pylier.autotrace)
    assert list(signature.parameters) == ["allow_empty", "min_exec_time", "filter_prefix", "modules"]
    assert all(parameter.kind is inspect.Parameter.KEYWORD_ONLY for parameter in signature.parameters.values())
    assert signature.parameters["allow_empty"].default is False
    assert signature.parameters["min_exec_time"].default is None
    assert signature.parameters["filter_prefix"].default is None
    assert signature.parameters["modules"].default is None


@pytest.mark.parametrize(
    ("kwargs", "error", "message"),
    [
        ({"min_exec_time": True}, TypeError, "min_exec_time"),
        ({"min_exec_time": -0.1}, ValueError, "min_exec_time"),
        ({"min_exec_time": math.nan}, ValueError, "min_exec_time"),
        ({"min_exec_time": math.inf}, ValueError, "min_exec_time"),
        ({"filter_prefix": ""}, ValueError, "filter_prefix"),
        ({"modules": 123}, TypeError, "modules"),
        ({"modules": ""}, ValueError, "modules"),
        ({"modules": [""]}, ValueError, "modules"),
        ({"modules": ["not-valid!"]}, ValueError, "modules"),
    ],
)
def test_autotrace_validates_arguments(kwargs, error, message):
    with pytest.raises(error, match=message):
        pylier.autotrace(**kwargs)


def test_autotrace_is_idempotent_for_identical_calls_and_rejects_conflicts():
    pylier.autotrace(allow_empty=True, min_exec_time=0, filter_prefix="skip_", modules=["tests"])
    pylier.autotrace(allow_empty=True, min_exec_time=None, filter_prefix="skip_", modules="tests")

    with pytest.raises(RuntimeError, match="already active"):
        pylier.autotrace(allow_empty=False, modules=["tests"])


@pytest.mark.parametrize("missing_attribute", ["monitoring", "_getframe"])
def test_autotrace_fails_at_activation_when_runtime_support_is_missing(
    monkeypatch: pytest.MonkeyPatch, missing_attribute: str
):
    module = importlib.import_module("pylier.autotrace")
    original = getattr(sys, missing_attribute)
    monkeypatch.delattr(sys, missing_attribute)
    try:
        with pytest.raises(RuntimeError, match="autotrace requires"):
            module.autotrace(modules=["tests"])
    finally:
        monkeypatch.setattr(sys, missing_attribute, original, raising=False)


def test_autotrace_infers_the_callers_package_and_captures_modules_imported_before_and_after_activation(module_factory):
    modules = module_factory(
        {
            "demoapp/__init__.py": "",
            "demoapp/bootstrap.py": """
            import pylier

            def activate() -> None:
                pylier.autotrace()
            """,
            "demoapp/before.py": """
            def before() -> str:
                return "before"
            """,
            "demoapp/after.py": """
            def after() -> str:
                return "after"
            """,
            "otherapp/__init__.py": "",
            "otherapp/mod.py": """
            def outside() -> str:
                return "outside"
            """,
        }
    )
    bootstrap = modules["demoapp.bootstrap"]
    before = modules["demoapp.before"]
    outside = modules["otherapp.mod"]

    bootstrap.activate()
    after = importlib.import_module("demoapp.after")

    with pylier.trace("inferred") as trace:
        assert before.before() == "before"
        assert after.after() == "after"
        assert outside.outside() == "outside"

    names = _node_names(trace)
    assert "before" in names
    assert "after" in names
    assert "outside" not in names


def test_autotrace_modules_override_matches_exact_boundaries_and_filter_prefix(module_factory):
    modules = module_factory(
        {
            "app/__init__.py": "",
            "app/work.py": """
            def keep() -> str:
                return "keep"

            def skip_hidden() -> str:
                return "skip"
            """,
            "apple/__init__.py": "",
            "apple/work.py": """
            def keep() -> str:
                return "apple"
            """,
        }
    )

    pylier.autotrace(modules=["app"], filter_prefix="skip_")

    with pylier.trace("modules") as trace:
        assert modules["app.work"].keep() == "keep"
        assert modules["app.work"].skip_hidden() == "skip"
        assert modules["apple.work"].keep() == "apple"

    names = _node_names(trace)
    assert "keep" in names
    assert "skip_hidden" not in names
    assert all(not node.module.startswith("apple") for node in trace.nodes.values())


def test_autotrace_traces_public_functions_and_all_method_forms_but_not_private_callables(module_factory):
    modules = module_factory(
        {
            "shapeapp/__init__.py": "",
            "shapeapp/mod.py": """
            class Shape:
                def _private(self) -> str:
                    return "private"

                def method(self) -> str:
                    return "method"

                @classmethod
                def make(cls) -> str:
                    return cls.__name__

                @staticmethod
                def static() -> str:
                    return "static"

                @property
                def title(self) -> str:
                    return "shape"

            def public() -> str:
                return "public"
            """,
        }
    )
    mod = modules["shapeapp.mod"]
    shape = mod.Shape()

    pylier.autotrace(modules=["shapeapp"], allow_empty=True)

    with pylier.trace("methods") as trace:
        assert mod.public() == "public"
        assert shape.method() == "method"
        assert mod.Shape.make() == "Shape"
        assert mod.Shape.static() == "static"
        assert shape.title == "shape"
        assert shape._private() == "private"

    names = _node_names(trace)
    assert {"public", "Shape.method", "Shape.make", "Shape.static", "Shape.title"} <= names
    assert "Shape._private" not in names


def test_explicitly_decorated_functions_are_not_double_recorded(module_factory):
    modules = module_factory(
        {
            "dupeapp/__init__.py": "",
            "dupeapp/mod.py": """
            import pylier

            @pylier.node
            def decorated() -> str:
                return "decorated"

            def caller() -> str:
                return decorated()
            """,
        }
    )
    mod = modules["dupeapp.mod"]

    pylier.autotrace(modules=["dupeapp"], allow_empty=True)

    with pylier.trace("dupes") as trace:
        assert mod.caller() == "decorated"

    names = [node.name for node in trace.nodes.values()]
    assert names.count("decorated") == 1
    decorated_invocations = [
        invocation for invocation in trace.invocations.values() if invocation.node_id.endswith("decorated")
    ]
    assert len(decorated_invocations) == 1


def test_autotrace_matches_decorated_sync_application_and_data_flow(module_factory):
    modules = module_factory(
        {
            "autoapp/__init__.py": "",
            "autoapp/mod.py": """
            def produce() -> dict[str, str]:
                return {"doc": "hello"}

            def consume(payload: dict[str, str]) -> str:
                return payload["doc"]

            def boom() -> None:
                raise RuntimeError("boom")
            """,
            "decorapp/__init__.py": "",
            "decorapp/mod.py": """
            import pylier

            @pylier.node
            def produce() -> dict[str, str]:
                return {"doc": "hello"}

            @pylier.node
            def consume(payload: dict[str, str]) -> str:
                return payload["doc"]

            @pylier.node
            def boom() -> None:
                raise RuntimeError("boom")
            """,
        }
    )
    auto = modules["autoapp.mod"]
    decorated = modules["decorapp.mod"]

    pylier.autotrace(modules=["autoapp"], allow_empty=True)
    with pylier.trace("auto") as auto_trace:
        with pytest.raises(RuntimeError, match="boom"):
            auto.boom()
        assert auto.consume(auto.produce()) == "hello"

    autotrace_module = importlib.import_module("pylier.autotrace")
    autotrace_module._reset_for_tests()

    with pylier.trace("decorated") as decorated_trace:
        with pytest.raises(RuntimeError, match="boom"):
            decorated.boom()
        assert decorated.consume(decorated.produce()) == "hello"

    assert _application_pairs(auto_trace) == _application_pairs(decorated_trace)
    assert _data_pairs(auto_trace) == _data_pairs(decorated_trace)
    exception_edge = next(edge for edge in auto_trace.edges.values() if edge.payload_type == "exception")
    assert exception_edge.metadata["phase"] == PHASE_EXCEPTION


def test_autotrace_respects_global_level_gating(module_factory):
    modules = module_factory(
        {
            "levelapp/__init__.py": "",
            "levelapp/mod.py": """
            def visible() -> str:
                return "visible"
            """,
        }
    )
    mod = modules["levelapp.mod"]

    pylier.autotrace(modules=["levelapp"])

    with pylier.set_level("core"), pylier.trace("core") as hidden_trace:
        assert mod.visible() == "visible"
    with pylier.trace("info") as visible_trace:
        assert mod.visible() == "visible"

    assert hidden_trace.nodes == {}
    assert _node_names(visible_trace) == {"visible"}


def test_autotrace_allow_empty_false_omits_empty_parents_but_keeps_children(module_factory):
    modules = module_factory(
        {
            "emptyapp/__init__.py": "",
            "emptyapp/mod.py": """
            def child() -> str:
                return "payload"

            def heartbeat() -> None:
                child()
                return None

            def load() -> str:
                return child()

            def save(item: str) -> None:
                child()
                return None

            def fail() -> None:
                raise ValueError("boom")
            """,
        }
    )
    mod = modules["emptyapp.mod"]

    pylier.autotrace(modules=["emptyapp"], allow_empty=False)

    with pylier.trace("empty") as trace:
        assert mod.load() == "payload"
        assert mod.heartbeat() is None
        assert mod.save("item") is None
        with pytest.raises(ValueError, match="boom"):
            mod.fail()

    names = _node_names(trace)
    assert "load" in names
    assert "child" in names
    assert "save" in names
    assert "fail" in names
    assert "heartbeat" not in names
    root_to_child = next(
        edge for edge in trace.edges.values() if edge.source == trace.root_node_id and edge.target.endswith("child")
    )
    assert root_to_child.count >= 1
    assert not any(invocation.node_id.endswith("heartbeat") for invocation in trace.invocations.values())
    assert any(edge.payload_type == "exception" for edge in trace.edges.values())


def test_autotrace_positive_min_exec_time_uses_warmup_promotion(module_factory, monkeypatch: pytest.MonkeyPatch):
    modules = module_factory(
        {
            "timedapp/__init__.py": "",
            "timedapp/mod.py": """
            def tick() -> str:
                return "tick"
            """,
        }
    )
    mod = modules["timedapp.mod"]
    autotrace_module = importlib.import_module("pylier.autotrace")
    perf_values = iter([0.0, 0.5, 1.0, 2.2, 3.0, 3.1])
    monkeypatch.setattr(autotrace_module.time, "perf_counter", lambda: next(perf_values))

    pylier.autotrace(modules=["timedapp"], min_exec_time=1.0, allow_empty=True)

    with pylier.trace("warmup") as trace:
        assert mod.tick() == "tick"
        assert mod.tick() == "tick"
        assert mod.tick() == "tick"

    invocations = [invocation for invocation in trace.invocations.values() if invocation.node_id.endswith("tick")]
    assert len(invocations) == 1
    assert _node_names(trace) == {"tick"}


def test_autotrace_captures_values_and_sidecar_output_through_existing_recorder_paths(
    module_factory, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    from pylier.config import reload_settings

    modules = module_factory(
        {
            "payloadapp/__init__.py": "",
            "payloadapp/mod.py": """
            def produce() -> dict[str, str]:
                return {"doc": "hello"}

            def consume(payload: dict[str, str]) -> str:
                return payload["doc"]
            """,
        }
    )
    mod = modules["payloadapp.mod"]

    monkeypatch.setenv("PYLIER_CAPTURE_VALUES", "true")
    reload_settings()
    try:
        pylier.autotrace(modules=["payloadapp"], allow_empty=True)
        sidecar_file = tmp_path / "autotrace.jsonl"
        with pylier.trace("payload", sidecar=sidecar_file) as trace:
            assert mod.consume(mod.produce()) == "hello"

        edge = next(edge for edge in trace.edges.values() if edge.target.endswith("consume"))
        assert json.loads(edge.value or "{}") == {"doc": "hello"}
        lines = [json.loads(line) for line in sidecar_file.read_text(encoding="utf-8").splitlines() if line.strip()]
        assert len(lines) == 2
        assert {line["node_id"].rsplit(".", 1)[-1] for line in lines} == {"produce", "consume"}
    finally:
        monkeypatch.delenv("PYLIER_CAPTURE_VALUES")
        reload_settings()


def test_autotrace_rollback_does_not_copy_accumulated_edge_handoffs(module_factory, monkeypatch: pytest.MonkeyPatch):
    modules = module_factory(
        {
            "deepcopyapp/__init__.py": "",
            "deepcopyapp/mod.py": """
            def echo(value: str) -> str:
                return value
            """,
        }
    )
    mod = modules["deepcopyapp.mod"]

    class GuardedHandoffs(list[dict[str, object]]):
        def __deepcopy__(self, memo: dict[int, object]) -> list[dict[str, object]]:
            raise AssertionError("edge handoffs were deepcopied")

    pylier.autotrace(modules=["deepcopyapp"], allow_empty=True)

    with pylier.trace("rollback") as trace:
        assert mod.echo("first") == "first"
        assert mod.echo("second") == "second"

        edge = next(edge for edge in trace.edges.values() if edge.target.endswith("echo"))
        edge.handoffs = GuardedHandoffs(edge.handoffs)
        original_record_event = trace.record_event
        call_count = 0

        def flaky_record_event(event: pylier.Event) -> None:
            nonlocal call_count
            call_count += 1
            original_record_event(event)
            if call_count == 1:
                raise RuntimeError("enter failure")

        monkeypatch.setattr(trace, "record_event", flaky_record_event)
        assert mod.echo("third") == "third"
        assert mod.echo("fourth") == "fourth"

    assert list(trace.invocations) == [f"{trace.id}:1", f"{trace.id}:2", f"{trace.id}:3"]
    edge = next(edge for edge in trace.edges.values() if edge.target.endswith("echo"))
    assert edge.count == 3
    assert len(edge.handoffs) == 3
    _assert_valid_trace_references(trace)


def test_autotrace_handles_coroutine_generator_and_async_generator_lifecycles(module_factory):
    modules = module_factory(
        {
            "lifeapp/__init__.py": "",
            "lifeapp/mod.py": """
            import asyncio

            async def coro() -> str:
                await asyncio.sleep(0)
                await asyncio.sleep(0)
                return "ok"

            def stream():
                yield "a"
                yield "b"

            async def astream():
                yield "x"
                await asyncio.sleep(0)
                yield "y"

            def touch(value: str) -> str:
                return value.upper()
            """,
        }
    )
    mod = modules["lifeapp.mod"]

    pylier.autotrace(modules=["lifeapp"], allow_empty=True)

    async def run_async() -> tuple[pylier.Trace, list[str]]:
        with pylier.trace("life") as trace:
            assert await mod.coro() == "ok"
            generator = mod.stream()
            assert mod.touch(next(generator)) == "A"
            assert mod.touch(next(generator)) == "B"
            with pytest.raises(StopIteration):
                next(generator)
            seen: list[str] = []
            async for item in mod.astream():
                seen.append(item)
            return trace, seen

    trace, seen = asyncio.run(run_async())
    assert seen == ["x", "y"]

    stream_invocations = [
        invocation for invocation in trace.invocations.values() if invocation.node_id.rsplit(".", 1)[-1] == "stream"
    ]
    coro_invocations = [
        invocation for invocation in trace.invocations.values() if invocation.node_id.rsplit(".", 1)[-1] == "coro"
    ]
    astream_invocations = [
        invocation for invocation in trace.invocations.values() if invocation.node_id.rsplit(".", 1)[-1] == "astream"
    ]
    assert len(stream_invocations) == 1
    assert len(coro_invocations) == 1
    assert len(astream_invocations) == 1
    assert (
        next(node_id for node_id in trace.nodes if node_id.endswith("stream")),
        next(node_id for node_id in trace.nodes if node_id.endswith("touch")),
    ) not in trace.edges


def test_autotrace_cleans_up_after_generator_close_and_async_generator_close(module_factory):
    modules = module_factory(
        {
            "cleanupapp/__init__.py": "",
            "cleanupapp/mod.py": """
            async def astream():
                yield 1
                yield 2

            def stream():
                yield 1
                yield 2
            """,
        }
    )
    mod = modules["cleanupapp.mod"]
    autotrace_module = importlib.import_module("pylier.autotrace")

    pylier.autotrace(modules=["cleanupapp"], allow_empty=True)

    generator = mod.stream()
    assert next(generator) == 1
    generator.close()
    assert autotrace_module._frame_state_count_for_tests() == 0

    async def close_async_generator() -> None:
        async_generator = mod.astream()
        assert await anext(async_generator) == 1
        await async_generator.aclose()

    asyncio.run(close_async_generator())
    assert autotrace_module._frame_state_count_for_tests() == 0


def test_autotrace_monitors_new_threads_too(module_factory):
    modules = module_factory(
        {
            "threadapp/__init__.py": "",
            "threadapp/mod.py": """
            def work() -> str:
                return "done"
            """,
        }
    )
    mod = modules["threadapp.mod"]
    results: queue.Queue[pylier.Trace] = queue.Queue()

    pylier.autotrace(modules=["threadapp"], allow_empty=True)

    def run() -> None:
        with pylier.trace("thread") as trace:
            assert mod.work() == "done"
        results.put(trace)

    thread = threading.Thread(target=run)
    thread.start()
    thread.join()

    trace = results.get_nowait()
    assert _node_names(trace) == {"work"}


def test_autotrace_reserves_only_tool_ids_3_and_4():
    autotrace_module = importlib.import_module("pylier.autotrace")

    class FakeMonitoring:
        def __init__(self, occupied: set[int]):
            self.occupied = occupied
            self.used: list[int] = []

        def get_tool(self, tool_id: int) -> str | None:
            return "busy" if tool_id in self.occupied else None

        def use_tool_id(self, tool_id: int, _name: str) -> None:
            self.used.append(tool_id)

    monitoring = FakeMonitoring({3})
    assert autotrace_module._reserve_tool_id(monitoring) == 4
    assert monitoring.used == [4]

    blocked_monitoring = FakeMonitoring({3, 4})
    with pytest.raises(RuntimeError, match="free sys.monitoring tool ID"):
        autotrace_module._reserve_tool_id(blocked_monitoring)
    assert blocked_monitoring.used == []


def test_autotrace_allow_empty_false_treats_default_equal_values_as_omitted_and_keeps_generators_with_yields(
    module_factory,
):
    modules = module_factory(
        {
            "emptydefaultsapp/__init__.py": "",
            "emptydefaultsapp/mod.py": """
            import asyncio

            def default_only(flag: str = "default") -> None:
                return None

            class Worker:
                def ping(self) -> None:
                    return None

                @classmethod
                def class_ping(cls) -> None:
                    return None

            def stream():
                yield "item"

            async def astream():
                yield "item"
                await asyncio.sleep(0)

            async def coro_wait() -> None:
                await asyncio.sleep(0)
            """,
        }
    )
    mod = modules["emptydefaultsapp.mod"]

    pylier.autotrace(modules=["emptydefaultsapp"], allow_empty=False)

    async def run_async() -> list[str]:
        seen: list[str] = []
        async for item in mod.astream():
            seen.append(item)
        await mod.coro_wait()
        return seen

    with pylier.trace("empty-defaults") as trace:
        assert mod.default_only() is None
        assert mod.default_only("default") is None
        assert mod.default_only(flag="default") is None
        assert mod.default_only("custom") is None
        assert mod.Worker().ping() is None
        assert mod.Worker.class_ping() is None
        assert list(mod.stream()) == ["item"]
        assert asyncio.run(run_async()) == ["item"]

    assert _node_names(trace) == {"default_only", "stream", "astream"}
    default_only_invocations = [
        invocation for invocation in trace.invocations.values() if invocation.node_id.endswith("default_only")
    ]
    assert len(default_only_invocations) == 1
    _assert_valid_trace_references(trace)


def test_autotrace_positive_min_exec_time_promotes_after_slow_exception(
    module_factory, monkeypatch: pytest.MonkeyPatch
):
    modules = module_factory(
        {
            "slowboomapp/__init__.py": "",
            "slowboomapp/mod.py": """
            def boom() -> None:
                raise RuntimeError("boom")
            """,
        }
    )
    mod = modules["slowboomapp.mod"]
    autotrace_module = importlib.import_module("pylier.autotrace")
    perf_values = iter([0.0, 1.5, 2.0, 2.1])
    monkeypatch.setattr(autotrace_module.time, "perf_counter", lambda: next(perf_values))

    pylier.autotrace(modules=["slowboomapp"], min_exec_time=1.0, allow_empty=True)

    with pylier.trace("warmup-exception") as trace:
        with pytest.raises(RuntimeError, match="boom"):
            mod.boom()
        with pytest.raises(RuntimeError, match="boom"):
            mod.boom()

    invocations = [invocation for invocation in trace.invocations.values() if invocation.node_id.endswith("boom")]
    assert len(invocations) == 1
    assert invocations[0].exception == "RuntimeError('boom')"
    _assert_valid_trace_references(trace)


def test_autotrace_buffered_nested_calls_keep_valid_ids_sidecars_and_payloads(
    module_factory, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    from pylier.config import reload_settings

    modules = module_factory(
        {
            "bufferedapp/__init__.py": "",
            "bufferedapp/mod.py": """
            def leaf(value: str) -> str:
                return value.upper()

            def middle() -> None:
                leaf("doc")
                return None

            def outer() -> None:
                middle()
                return None
            """,
        }
    )
    mod = modules["bufferedapp.mod"]

    monkeypatch.setenv("PYLIER_CAPTURE_VALUES", "true")
    reload_settings()
    try:
        pylier.autotrace(modules=["bufferedapp"], allow_empty=False)
        sidecar_file = tmp_path / "buffered.jsonl"
        with pylier.trace("buffered", sidecar=sidecar_file) as trace:
            assert mod.outer() is None
    finally:
        monkeypatch.delenv("PYLIER_CAPTURE_VALUES")
        reload_settings()

    assert _node_names(trace) == {"leaf"}
    assert list(trace.invocations) == [f"{trace.id}:1"]
    _assert_valid_trace_references(trace)

    leaf_invocation = next(iter(trace.invocations.values()))
    payload_state, payload = trace.invocation_payload(leaf_invocation.id)
    assert payload_state == "available"
    assert payload is not None
    assert json.loads(payload["arguments"]) == {"value": "doc"}
    assert json.loads(payload["result"]) == "DOC"

    lines = [json.loads(line) for line in sidecar_file.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert len(lines) == 1
    assert lines[0]["node_id"].endswith("leaf")
    assert lines[0]["edges"][0]["handoffs"][0]["invocation_id"] == leaf_invocation.id


def test_autotrace_resume_keeps_generator_trace_isolation_across_threads(module_factory):
    modules = module_factory(
        {
            "crossgenapp/__init__.py": "",
            "crossgenapp/mod.py": """
            def foreign() -> str:
                return "foreign"

            def child() -> str:
                return "child"

            def stream():
                yield "a"
                child()
                yield "b"
            """,
        }
    )
    mod = modules["crossgenapp.mod"]
    results: queue.Queue[pylier.Trace | BaseException] = queue.Queue()

    pylier.autotrace(modules=["crossgenapp"], allow_empty=True)

    with pylier.trace("owner") as owner_trace:
        generator = mod.stream()
        assert next(generator) == "a"

        def run() -> None:
            try:
                with pylier.trace("foreign") as foreign_trace:
                    assert mod.foreign() == "foreign"
                    assert next(generator) == "b"
                    try:
                        next(generator)
                    except StopIteration:
                        pass
                    else:
                        raise AssertionError("stream should be exhausted")
                results.put(foreign_trace)
            except BaseException as exc:  # pragma: no cover - assertion relay
                results.put(exc)

        thread = threading.Thread(target=run)
        thread.start()
        thread.join()

    result = results.get_nowait()
    if isinstance(result, BaseException):
        raise result
    foreign_trace = result

    assert _node_names(owner_trace) == {"stream", "child"}
    assert _node_names(foreign_trace) == {"foreign"}
    _assert_valid_trace_references(owner_trace)
    _assert_valid_trace_references(foreign_trace)


def test_autotrace_resume_keeps_async_generator_trace_isolation_across_traces(module_factory):
    modules = module_factory(
        {
            "crossagenapp/__init__.py": "",
            "crossagenapp/mod.py": """
            def foreign() -> str:
                return "foreign"

            def child() -> str:
                return "child"

            async def astream():
                yield "a"
                child()
                yield "b"
            """,
        }
    )
    mod = modules["crossagenapp.mod"]

    pylier.autotrace(modules=["crossagenapp"], allow_empty=True)

    async def run_async() -> tuple[pylier.Trace, pylier.Trace]:
        with pylier.trace("owner") as owner_trace:
            async_generator = mod.astream()
            assert await anext(async_generator) == "a"
            with pylier.trace("foreign") as foreign_trace:
                assert mod.foreign() == "foreign"
                assert await anext(async_generator) == "b"
                with pytest.raises(StopAsyncIteration):
                    await anext(async_generator)
            return owner_trace, foreign_trace

    owner_trace, foreign_trace = asyncio.run(run_async())

    assert _node_names(owner_trace) == {"astream", "child"}
    assert _node_names(foreign_trace) == {"foreign"}
    _assert_valid_trace_references(owner_trace)
    _assert_valid_trace_references(foreign_trace)


def test_autotrace_start_callback_failure_rolls_back_partial_mutation(module_factory, monkeypatch: pytest.MonkeyPatch):
    modules = module_factory(
        {
            "startfailapp/__init__.py": "",
            "startfailapp/mod.py": """
            def work() -> str:
                return "ok"
            """,
        }
    )
    mod = modules["startfailapp.mod"]

    pylier.autotrace(modules=["startfailapp"], allow_empty=True)

    with pylier.trace("start-failure") as trace:
        original_record_event = trace.record_event
        call_count = 0

        def flaky_record_event(event: pylier.Event) -> None:
            nonlocal call_count
            call_count += 1
            original_record_event(event)
            if call_count == 1:
                raise RuntimeError("enter failure")

        monkeypatch.setattr(trace, "record_event", flaky_record_event)
        assert mod.work() == "ok"
        assert mod.work() == "ok"

    assert _node_names(trace) == {"work"}
    assert list(trace.invocations) == [f"{trace.id}:1"]
    assert [event.kind for event in trace.events] == ["enter", "exit"]
    assert next(iter(trace.nodes.values())).calls == 1
    _assert_valid_trace_references(trace)
