"""Core pylier behavior: node decoration, edge inference, levels, rendering."""

from __future__ import annotations

import asyncio
import http.client
import json
import socket
from pathlib import Path

import pytest

import pylier
from pylier.model import Level, Node, Trace


def test_decorated_nodes_emit_nested_otel_spans_without_logfire():
    @pylier.node
    def child(value: str) -> str:
        return value.upper()

    @pylier.node
    def parent(value: str) -> str:
        return child(value)

    with pylier.trace("otel") as traced:
        assert parent("ok") == "OK"

    spans = {span.name.rsplit(".", 1)[-1]: span for span in traced.spans.values()}
    assert traced.otel_trace_id is not None
    assert spans["parent"].trace_id == traced.otel_trace_id
    assert spans["child"].parent_span_id == spans["parent"].span_id


def test_otel_spans_are_visible_while_still_running():
    from opentelemetry import trace as otel_trace

    with (
        pylier.trace("live-otel") as traced,
        otel_trace.get_tracer("example").start_as_current_span("GET /stream"),
    ):
        span = next(item for item in traced.spans.values() if item.name == "GET /stream")
        assert span.status == "running"
        assert span.ended_ns is None


def test_standard_otel_spans_are_captured_in_the_active_pylier_trace():
    from opentelemetry import trace as otel_trace

    with (
        pylier.trace("external-otel") as traced,
        otel_trace.get_tracer("example").start_as_current_span("GET /ocr"),
    ):
        pass

    external_span = next(span for span in traced.spans.values() if span.name == "GET /ocr")
    assert external_span.trace_id == traced.otel_trace_id
    assert external_span.parent_span_id is not None


def test_sync_edge_inferred_from_returned_value():
    @pylier.node
    def producer():
        return {"doc": "hello"}

    @pylier.node
    def consumer(payload):
        return payload["doc"]

    with pylier.trace("t") as tr:
        out = consumer(producer())

    assert out == "hello"
    ids = list(tr.nodes)
    assert len(ids) == 2
    assert len(tr.edges) == 1
    (src, tgt), edge = next(iter(tr.edges.items()))
    assert src.endswith("producer")
    assert tgt.endswith("consumer")
    assert edge.payload_type == "dict"


def test_derive_infers_multiple_sources_for_a_computed_value():
    @pylier.node
    def load_title():
        return "Hello"

    @pylier.node
    def load_body():
        return "world"

    @pylier.node
    def index_document(document: str):
        return document.upper()

    with pylier.trace() as trace:
        title = load_title()
        body = load_body()
        document = pylier.derive(title + " " + body, from_=[title, body])
        assert isinstance(document, str)
        assert document == "Hello world"
        index_document(document)

    index_node_id = next(node_id for node_id, node in trace.nodes.items() if node.name.endswith("index_document"))
    inbound_source_names = {
        trace.nodes[source_id].name.rsplit(".", 1)[-1]
        for source_id, target_id in trace.edges
        if target_id == index_node_id
    }
    assert inbound_source_names == {"load_title", "load_body"}


def test_derive_preserves_transitive_lineage_and_deduplicates_sources():
    @pylier.node
    def load_title():
        return "Hello"

    @pylier.node
    def load_body():
        return "world"

    @pylier.node
    def index_document(document: str):
        return document

    with pylier.trace() as trace:
        title = load_title()
        body = load_body()
        combined = pylier.derive(title + body, from_=[title, body, title])
        wrapped = pylier.derive(f"<{combined}>", from_=[combined])
        index_document(wrapped)

    index_node_id = next(node_id for node_id, node in trace.nodes.items() if node.name.endswith("index_document"))
    inbound_edges = [edge for edge in trace.edges.values() if edge.target == index_node_id]
    assert {trace.nodes[edge.source].name.rsplit(".", 1)[-1] for edge in inbound_edges} == {
        "load_title",
        "load_body",
    }
    assert all(edge.count == 1 for edge in inbound_edges)


def test_derive_warns_and_keeps_known_sources_when_a_source_is_untraced():
    @pylier.node
    def load_title():
        return "Hello"

    @pylier.node
    def index_document(document: str):
        return document

    with pylier.trace() as trace:
        title = load_title()
        with pytest.warns(RuntimeWarning, match="1 declared source"):
            document = pylier.derive(title + " external", from_=[title, " external"])
        index_document(document)

    assert len(trace.edges) == 1
    edge = next(iter(trace.edges.values()))
    assert trace.nodes[edge.source].name.endswith("load_title")
    assert trace.nodes[edge.target].name.endswith("index_document")


def test_derive_rejects_a_single_string_as_the_source_iterable():
    with pytest.raises(TypeError, match="iterable of source values"):
        pylier.derive("derived", from_="source")


def test_logfire_style_node_tags_are_serialized_on_nodes_only():
    @pylier.node
    def emit():
        return "tagged-value"

    @pylier.node(_tags=[" ingest ", "critical", "ingest"])
    def handle(event):
        return event

    with pylier.trace() as tr:
        handle(emit())

    node = next(node for node in tr.nodes.values() if node.name.endswith("handle"))
    assert node.tags == ("ingest", "critical")
    graph = tr.to_graph_dict()
    tagged = next(node for node in graph["nodes"] if node["name"].endswith("handle"))
    assert tagged["tags"] == ["ingest", "critical"]
    assert "tags" not in graph["links"][0]


@pytest.mark.parametrize(
    ("tags", "error"),
    [(["valid", ""], ValueError), (["valid", 1], TypeError)],
)
def test_node_tags_are_validated(tags, error):
    with pytest.raises(error):
        pylier.node(_tags=tags)(lambda: None)


def test_heterogeneous_tuple_records_member_types_for_rendering():
    @pylier.node
    def emit():
        return (True, 3, "three")

    @pylier.node
    def handle(payload):
        return payload

    with pylier.trace() as tr:
        handle(emit())

    edge = next(iter(tr.edges.values()))
    assert edge.payload_type == "tuple"
    assert edge.payload_types == ("bool", "int", "str")


def test_string_and_binary_edges_are_serialized_without_tags():
    @pylier.node
    def emit_text():
        return "text"

    @pylier.node
    def emit_binary():
        return b"binary"

    @pylier.node
    def handle_text(payload):
        return payload

    @pylier.node
    def handle_binary(payload):
        return payload

    with pylier.trace() as tr:
        handle_text(emit_text())
        handle_binary(emit_binary())

    graph = tr.to_graph_dict()
    assert {edge["payload"] for edge in graph["links"]} == {"str", "bytes"}
    assert all(edge["payload_types"] == [] for edge in graph["links"])
    assert all("tags" not in edge for edge in graph["links"])


def test_branching_pipeline_inferred():
    @pylier.node
    def load():
        return "raw"

    @pylier.node
    def branch_a(data):
        return data + "-a"

    @pylier.node
    def branch_b(data):
        return data + "-b"

    @pylier.node
    def merge(a, b):
        return a + "|" + b

    with pylier.trace() as tr:
        raw = load()
        merge(branch_a(raw), branch_b(raw))

    assert len(tr.nodes) == 4
    # load -> a, load -> b, a -> merge, b -> merge
    assert len(tr.edges) == 4
    targets_of_load = {tgt for (src, tgt), e in tr.edges.items() if src.endswith("load")}
    assert {t.rsplit(".", 1)[-1] for t in targets_of_load} == {"branch_a", "branch_b"}


def test_async_node_infers_edge():
    @pylier.node
    async def fetch():
        await asyncio.sleep(0)
        return [1, 2, 3]

    @pylier.node
    async def summarize(data):
        return sum(data)

    async def run():
        with pylier.trace("async") as tr:
            result = await summarize(await fetch())
            return result, tr

    result, tr = asyncio.run(run())
    assert result == 6
    assert len(tr.edges) == 1
    assert next(iter(tr.edges.values())).payload_type == "list"


def test_level_filters_uncaptured_nodes():
    @pylier.node(level="trace")
    def verbose():
        return "v"

    @pylier.node
    def core():
        return verbose()

    # default level is INFO; the "trace"-level node is not captured
    with pylier.trace() as tr:
        core()

    assert len(tr.nodes) == 1
    assert next(iter(tr.nodes)).endswith("core")


def test_set_level_enables_verbose_node():
    @pylier.node(level="trace")
    def verbose():
        return "v"

    @pylier.node
    def core():
        return verbose()

    with pylier.set_level("trace"), pylier.trace() as tr:
        core()

    assert len(tr.nodes) == 2


def test_render_writes_self_contained_html(tmp_path: Path):
    @pylier.node
    def a():
        return [1, 2]

    @pylier.node
    def b(data):
        return sum(data)

    with pylier.trace("render-test") as tr:
        b(a())

    out = pylier.render(tmp_path / "out.html", trace=tr)
    assert out.exists()
    html = out.read_text(encoding="utf-8")
    assert "d3@7" in html
    assert "render-test" in html
    # embedded JSON must be valid: extract the RAW object literal
    start = html.index("const RAW = ") + len("const RAW = ")
    obj_start = html.index("{", start)
    depth = 0
    i = obj_start
    while i < len(html):
        if html[i] == "{":
            depth += 1
        elif html[i] == "}":
            depth -= 1
            if depth == 0:
                break
        i += 1
    graph = json.loads(html[obj_start : i + 1])
    assert graph["name"] == "render-test"
    assert len(graph["nodes"]) == 2
    assert len(graph["links"]) == 1
    assert "TYPE_COLOR" in html
    assert "tag-options" in html
    assert "--edge-glow-alpha" in html
    assert "duration(1000)" in html
    assert ".velocityDecay(0.55)" in html
    assert "sim.alpha(prevNodeIds.size ? 0.12 : 0.65).restart()" in html
    assert "trace-start" in html
    assert "traceStartNode" in html
    assert 'name="edge-mode"' in html
    assert "directRoute" in html
    assert 'state.edgeMode === "direct" ? directRoute(d) : linkRoute(d)' in html
    assert "pylier-edge-mode" in html
    assert 'id="b-console"' in html
    assert 'id="event-console"' in html
    assert "SSE_LOG_LIMIT = 100" in html
    assert "logSseEvent" in html
    assert "requestAnimationFrame(flushSseLog)" in html
    assert "pylier-console-collapsed" in html
    assert 'class="foot-note"' not in html
    assert "payload_kind" not in html


def test_render_embedded_data_block_is_valid_json(tmp_path: Path):
    """The file://-fallback embedded-data block must be valid JSON and the guard
    literal must not collide with the injected payload. Regression: the
    placeholder substring appeared inside a JS string literal and broke the
    whole script with 'Unexpected identifier'."""
    import re
    import shutil
    import subprocess
    import tempfile

    @pylier.node
    def a():
        return {"name": "doc"}

    @pylier.node
    def b(doc):
        return doc["name"]

    with pylier.trace("fallback") as tr:
        b(a())

    out = pylier.render(tmp_path / "out.html", trace=tr)
    html = out.read_text(encoding="utf-8")
    m = re.search(r'<script id="embedded-data"[^>]*>(.*?)</script>', html, re.S)
    assert m, "embedded-data block missing"
    embedded = json.loads(m.group(1))
    assert embedded["name"] == "fallback"
    # the main inline script must be syntactically valid JS (no token leakage)
    scripts = re.findall(r"<script>(.*?)</script>", html, re.S)
    assert scripts, "inline script missing"
    if not shutil.which("node"):
        return  # node not available in this env; JSON checks above still ran
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as f:
        f.write(scripts[-1])
        js_path = f.name
    r = subprocess.run(["node", "--check", js_path], capture_output=True, text=True)
    assert r.returncode == 0, f"inline JS invalid: {r.stderr}"


def test_trace_isolation_between_contexts():
    @pylier.node
    def f():
        return 1

    @pylier.node
    def g(x):
        return x + 1

    with pylier.trace("first") as tr1:
        g(f())

    with pylier.trace("second") as tr2:
        g(f())

    assert len(tr1.nodes) == 2 and len(tr2.nodes) == 2
    assert tr1 is not tr2


def test_sidecar_writes_events(tmp_path: Path):
    @pylier.node(_tags=["source"])
    def produce():
        return "payload"

    @pylier.node
    def consume(data):
        return data.upper()

    sidecar_file = tmp_path / "trace.jsonl"
    with pylier.trace("sidecar", sidecar=sidecar_file):
        consume(produce())

    assert sidecar_file.exists()
    lines = [json.loads(line) for line in sidecar_file.read_text().splitlines() if line.strip()]
    # one exit-event per captured node
    assert len(lines) == 2
    assert all("node_id" in ev and "edges" in ev for ev in lines)
    assert any(event["tags"] == ["source"] for event in lines)


def test_size_and_preview_captured_at_debug():
    @pylier.node
    def produce():
        return [1, 2, 3]

    @pylier.node
    def consume(data):
        return sum(data)

    with pylier.set_level("debug"), pylier.trace() as tr:
        consume(produce())

    edge = next(iter(tr.edges.values()))
    assert edge.size == 3
    assert edge.preview is not None


def test_size_not_captured_at_core():
    @pylier.node(level="core")
    def produce():
        return [1, 2, 3]

    @pylier.node(level="core")
    def consume(data):
        return sum(data)

    with pylier.set_level("core"), pylier.trace() as tr:
        consume(produce())

    edge = next(iter(tr.edges.values()))
    assert edge.size is None
    assert edge.preview is None


def test_value_not_captured_by_default():
    @pylier.node
    def produce():
        return {"secret": 42}

    @pylier.node
    def consume(doc):
        return doc

    with pylier.trace() as tr:
        consume(produce())

    edge = next(iter(tr.edges.values()))
    assert edge.value is None


def test_value_captured_when_enabled(monkeypatch):
    from pylier.config import get_settings, reload_settings

    monkeypatch.setenv("PYLIER_CAPTURE_VALUES", "true")
    reload_settings()
    try:

        @pylier.node
        def produce():
            return {"doc": "hello", "n": 2}

        @pylier.node
        def consume(doc):
            return doc["doc"]

        with pylier.trace() as tr:
            consume(produce())

        edge = next(iter(tr.edges.values()))
        assert edge.value is not None
        payload = json.loads(edge.value)
        assert payload == {"doc": "hello", "n": 2}
    finally:
        get_settings.cache_clear()


def test_binary_payload_truncated(monkeypatch):
    from pylier.config import reload_settings

    monkeypatch.setenv("PYLIER_CAPTURE_VALUES", "true")
    reload_settings()
    try:

        @pylier.node
        def produce():
            return b"\x00" * 5000

        @pylier.node
        def consume(blob):
            return len(blob)

        with pylier.trace() as tr:
            consume(produce())

        edge = next(iter(tr.edges.values()))
        assert edge.value is not None
        assert edge.value.startswith("<bytes 5000 bytes:")
        assert len(edge.value) < 200  # truncated summary, never raw
    finally:
        reload_settings()


def test_events_timeline_and_edge_handoffs():
    @pylier.node
    def produce():
        return "p"

    @pylier.node
    def consume(data):
        return data

    with pylier.trace("tl") as tr:
        consume(produce())

    kinds = [e.kind for e in tr.events]
    assert kinds == ["enter", "exit", "enter", "exit"]  # nested call order
    # consume's enter event carries the fired handoff edge produce->consume
    consume_enter = tr.events[2]
    assert consume_enter.kind == "enter"
    assert len(consume_enter.edges) == 1
    fired = consume_enter.edges[0]
    assert fired["source"].endswith("produce")
    assert fired["target"].endswith("consume")
    # timeline is in the graph dict for static replay
    graph = tr.to_graph_dict()
    assert [ev["kind"] for ev in graph["events"]] == kinds
    assert graph["events"][2]["edges"] == consume_enter.edges


def test_sse_latency_update_does_not_emit_empty_exec_batches():
    trace = Trace("sse-latency")
    trace.get_or_create_node(Node(id="latency", name="latency", module="tests", level=Level.INFO))
    server = pylier.serve(trace=trace, port=0, open_browser=False)
    connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=0.2)
    try:
        connection.request("GET", "/events")
        response = connection.getresponse()
        assert response.status == 200
        assert response.readline() == b"event: tabs\n"
        while response.readline() != b"\n":
            pass
        assert response.readline() == b"event: graph\n"
        while response.readline() != b"\n":
            pass

        trace.record_latency("latency", 1.5)
        # A latency-only update may wake the SSE loop with a heartbeat, but
        # must not begin an unbounded stream of empty ``event: exec`` frames.
        assert response.readline() == b": heartbeat\n"
        assert response.readline() == b"\n"
        with pytest.raises((TimeoutError, socket.timeout)):
            response.readline()
    finally:
        connection.close()
        server.shutdown()
        server.server_close()


def test_versions_split_topology_vs_execution():
    @pylier.node
    def produce():
        return "p"

    @pylier.node
    def consume(data):
        return data

    with pylier.trace("v") as tr:
        consume(produce())
        g1, e1 = tr.graph_version, tr.exec_version
        consume(produce())  # repeats: no new topology, more events
        g2, e2 = tr.graph_version, tr.exec_version

    assert g2 == g1  # topology unchanged
    assert e2 > e1  # execution events still streamed
