"""Core data model: nodes, edges, events, traces, and capture levels.

These types are the single source of truth shared by the recorder, the
tracing backends (in-memory / sidecar / OTel receiver), and the render core.
"""

from __future__ import annotations

import threading
from collections import OrderedDict
from dataclasses import dataclass, field
from enum import IntEnum
from uuid import uuid4


class Level(IntEnum):
    """Capture verbosity tiers.

    Ranks increase with verbosity. A node is recorded only when its declared
    level rank is <= the active global level rank, so raising the global level
    to ``DEBUG`` enables core+info+debug nodes. Metadata richness (size and
    preview) is driven by the effective level; declared node tags are retained:

    * ``CORE``  - structural identity only (node + bare edges), no payload detail
    * ``INFO``  - + payload type + size
    * ``DEBUG`` - + short value preview
    * ``TRACE`` - + detailed previews / fingerprints
    """

    CORE = 0
    INFO = 10
    DEBUG = 20
    TRACE = 30


@dataclass
class Node:
    """A pipeline node. One-to-one with a decorated function/method."""

    id: str
    name: str
    module: str
    level: Level = Level.INFO
    tags: tuple[str, ...] = ()
    calls: int = 0
    is_async: bool = False
    last_ms: float | None = None
    avg_ms: float | None = None
    kind: str = "pylier"
    is_start: bool = False
    # Raw imported OpenTelemetry span payload. Decorator nodes leave this empty.
    otel: dict = field(default_factory=dict)


@dataclass
class Edge:
    """A directed data flow between two nodes."""

    source: str
    target: str
    payload_type: str = "unknown"
    size: int | None = None
    preview: str | None = None
    # Member types are populated only for heterogeneous tuples with 2–3
    # distinct types; the renderer uses them for a compact gradient.
    payload_types: tuple[str, ...] = ()
    count: int = 1
    # full serialized payload — only populated when capture_values is on;
    # binary payloads are truncated to a summary (see fingerprint.serialize_value)
    value: str | None = None
    # ``control`` links external request roots to captured algorithms. Data
    # handoffs remain the default and are still inferred by fingerprinting.
    kind: str = "data"
    # Relation-specific debugger data, e.g. an imported OTel parent span ID.
    metadata: dict = field(default_factory=dict)


@dataclass
class Event:
    """A raw recorder event (enter/exit of a node call).

    These drive execution-aware animation: a node pulses from its ``enter`` to
    its ``exit``; inbound edges on an ``enter`` are the data handoffs that
    should fire on screen at that moment.
    """

    ts: float
    node_id: str
    kind: str  # "enter" | "exit"
    fingerprint: str | None = None
    return_type: str | None = None
    # edges materialized/observed at this event: {"source","target"} pairs
    edges: list[dict[str, str]] = field(default_factory=list)


class Trace:
    """An accumulated graph for one logical run.

    Holds the materialized nodes/edges plus the transient fingerprint index used
    by the recorder to infer edges. ``events`` is kept so a live viewer can
    replay the timeline; ``to_graph_dict`` produces the JSON the renderer needs.
    """

    def __init__(self, name: str = "trace") -> None:
        self.id = uuid4().hex
        self.name = name
        self.root: dict[str, str | int | None] | None = None
        self.endpoint: dict[str, str | int | None] = {"name": name, "status_code": None}
        self.root_node_id: str | None = None
        self.nodes: OrderedDict[str, Node] = OrderedDict()
        # A pair of operations can have data, call, OTel-parent, and return
        # relations simultaneously, so the relation kind is part of identity.
        self.edges: OrderedDict[tuple[str, str, str], Edge] = OrderedDict()
        self._otel_span_nodes: dict[str, str] = {}
        self._otel_trace_ids: set[str] = set()
        self.events: list[Event] = []
        # event sinks (e.g. SidecarBackend) notified after each resolved event.
        # Typed loosely to keep this module free of tracing-layer imports.
        self.sinks: list = []
        # fp -> source node id that produced this value. Latest producer wins
        # so a transformed-but-equal-content value still links to the most recent
        # origin, which is what callers actually consume.
        self._fp_index: dict[str, str] = {}
        # Derived values retain a resolved set of producer node IDs. This stays
        # fingerprint-agnostic: recorder.py computes fingerprints and passes
        # opaque keys here.
        self._derived_sources: dict[str, tuple[str, ...]] = {}
        # live-change notification: two versions sharing one condition.
        # graph_version bumps only when topology changes (new node/edge) so SSE
        # pushes the full graph rarely; exec_version bumps on every enter/exit
        # event so execution animation streams in real time.
        self.graph_version: int = 0
        self.exec_version: int = 0
        self._cond = threading.Condition()
        self._listeners: list = []

    def add_listener(self, listener) -> None:
        """Register a callback notified whenever graph or execution data changes."""
        with self._cond:
            self._listeners.append(listener)

    def _notify_listeners(self) -> None:
        for listener in tuple(self._listeners):
            listener(self)

    def _bump_graph(self) -> None:
        """Notify topology change (new node/edge). Caller holds ``_cond``."""
        self.graph_version += 1
        self._cond.notify_all()
        self._notify_listeners()

    def _bump_exec(self) -> None:
        """Notify execution event appended. Caller holds ``_cond``."""
        self.exec_version += 1
        self._cond.notify_all()
        self._notify_listeners()

    def record_event(self, event: Event) -> None:
        """Append an enter/exit event to the timeline and notify waiters."""
        with self._cond:
            self.events.append(event)
            self._bump_exec()

    def events_since(self, index: int) -> tuple[int, list[Event]]:
        """Return (total, events[index:]) — a consistent tail of the timeline."""
        with self._cond:
            return len(self.events), list(self.events[index:])

    def wait_for_change(self, graph_since: int, exec_since: int, timeout: float) -> tuple[int, int]:
        """Block until either version exceeds its ``since`` value or timeout.

        Returns ``(graph_version, exec_version)``. Used by the SSE viewer to
        push graph changes rarely and execution events in real time.
        """
        with self._cond:
            if self.graph_version <= graph_since and self.exec_version <= exec_since:
                self._cond.wait(timeout=timeout)
            return self.graph_version, self.exec_version

    def snapshot(self) -> dict:
        """Thread-safe copy of the current graph dict (acquires the change lock)."""
        with self._cond:
            return self.to_graph_dict()

    def get_or_create_node(self, node: Node) -> Node:
        with self._cond:
            existing = self.nodes.get(node.id)
            if existing is None:
                self.nodes[node.id] = node
                self._bump_graph()
                return node
            existing.calls += 1
            # call-count changes are not topology: the viewer increments badges
            # locally from exec events, so no graph push here
            return existing

    def set_request_root(
        self,
        *,
        method: str,
        route: str,
        otel_trace_id: str,
        otel_span_id: str,
    ) -> str:
        """Create the external FastAPI/OTel request root for this trace."""
        node_id = f"otel.fastapi.{otel_span_id}"
        with self._cond:
            self.name = f"{method} {route}"
            self.endpoint = {"name": self.name, "status_code": None}
            self.root = {
                "otel_trace_id": otel_trace_id,
                "otel_span_id": otel_span_id,
                "method": method,
                "route": route,
            }
            self.root_node_id = node_id
            self._otel_span_nodes[otel_span_id] = node_id
            self._otel_trace_ids.add(otel_trace_id)
            self.nodes[node_id] = Node(
                id=node_id,
                name=self.name,
                module="fastapi",
                level=Level.CORE,
                calls=1,
                kind="fastapi",
                is_start=True,
                otel={"trace_id": otel_trace_id, "span_id": otel_span_id, "kind": "SERVER"},
            )
            self._bump_graph()
        return node_id

    def set_request_status(self, status_code: int | None, ms: float) -> None:
        """Store the completed HTTP response status and request duration."""
        with self._cond:
            self.endpoint["status_code"] = status_code
            if self.root_node_id is not None and self.root_node_id in self.nodes:
                self.nodes[self.root_node_id].last_ms = ms
                self.nodes[self.root_node_id].avg_ms = ms
            self._bump_exec()

    def link_request_root(self, target: str) -> None:
        """Link an external request root to the first captured pylier node."""
        if self.root_node_id is None or target == self.root_node_id:
            return
        with self._cond:
            key = (self.root_node_id, target, "call")
            if key in self.edges:
                return
            endpoint_node = self.nodes.get(target)
            if endpoint_node is not None:
                endpoint_node.kind = "endpoint"
            self.edges[key] = Edge(
                source=self.root_node_id,
                target=target,
                payload_type="request",
                kind="call",
            )
            self._bump_graph()

    def record_latency(self, node_id: str, ms: float) -> None:
        """Update a node's latest call duration and running average.

        Exec-version only (no topology change), so the live viewer streams the
        updated latency via the next exec batch's embedded node snapshot.
        """
        with self._cond:
            n = self.nodes.get(node_id)
            if n is None:
                return
            n.last_ms = ms
            # running average over observed calls
            if n.avg_ms is None:
                n.avg_ms = ms
            else:
                n.avg_ms = n.avg_ms + (ms - n.avg_ms) / max(1, n.calls)
            self._bump_exec()

    def add_edge(
        self,
        source: str,
        target: str,
        *,
        payload_type: str = "unknown",
        size: int | None = None,
        preview: str | None = None,
        payload_types: tuple[str, ...] = (),
        value: str | None = None,
        kind: str = "data",
        metadata: dict | None = None,
    ) -> Edge:
        with self._cond:
            key = (source, target, kind)
            edge = self.edges.get(key)
            if edge is None:
                edge = Edge(
                    source=source,
                    target=target,
                    payload_type=payload_type,
                    size=size,
                    preview=preview,
                    payload_types=payload_types,
                    value=value,
                    kind=kind,
                    metadata=metadata or {},
                )
                self.edges[key] = edge
                self._bump_graph()
            else:
                edge.count += 1
                if payload_type != "unknown":
                    edge.payload_type = payload_type
                if size is not None:
                    edge.size = size
                if preview is not None:
                    edge.preview = preview
                if value is not None:
                    edge.value = value
                if payload_types:
                    edge.payload_types = payload_types
                if metadata:
                    edge.metadata.update(metadata)
            return edge

    def add_relation(
        self,
        source: str,
        target: str,
        kind: str,
        *,
        payload_type: str = "unknown",
        size: int | None = None,
        preview: str | None = None,
        value: str | None = None,
        metadata: dict | None = None,
    ) -> Edge:
        """Add an execution/imported relation, optionally carrying a return summary."""
        return self.add_edge(
            source,
            target,
            kind=kind,
            payload_type=payload_type,
            size=size,
            preview=preview,
            value=value,
            metadata=metadata,
        )

    def map_otel_span(self, trace_id: str, span_id: str, node_id: str) -> None:
        """Associate an OTel span with an existing graph node."""
        with self._cond:
            self._otel_trace_ids.add(trace_id)
            self._otel_span_nodes[span_id] = node_id

    def record_otel_span(self, span: object) -> str | None:
        """Materialize a finished SDK span as an inspectable OTel operation node.

        The bridge deliberately accepts the OTel object structurally so this
        neutral core never imports optional OTel packages.
        """
        context = span.get_span_context()
        if not context.is_valid:
            return None
        trace_id = f"{context.trace_id:032x}"
        span_id = f"{context.span_id:016x}"
        node_id = self._otel_span_nodes.get(span_id, f"otel.span.{span_id}")
        attributes = dict(getattr(span, "attributes", {}) or {})
        events = [
            {"name": event.name, "timestamp": event.timestamp, "attributes": dict(event.attributes or {})}
            for event in getattr(span, "events", ())
        ]
        links = [
            {
                "trace_id": f"{link.context.trace_id:032x}",
                "span_id": f"{link.context.span_id:016x}",
                "attributes": dict(link.attributes or {}),
            }
            for link in getattr(span, "links", ())
        ]
        status = getattr(span, "status", None)
        instrumentation_scope = getattr(span, "instrumentation_scope", None)
        resource = getattr(span, "resource", None)
        otel = {
            "trace_id": trace_id,
            "span_id": span_id,
            "parent_span_id": _span_id(getattr(span, "parent", None)),
            "kind": getattr(getattr(span, "kind", None), "name", str(getattr(span, "kind", "INTERNAL"))),
            "status": getattr(
                getattr(status, "status_code", None), "name", str(getattr(status, "status_code", "UNSET"))
            ),
            "status_description": getattr(status, "description", None),
            "attributes": attributes,
            "events": events,
            "links": links,
            "resource": dict(getattr(resource, "attributes", {}) or {}),
            "instrumentation_scope": {
                "name": getattr(instrumentation_scope, "name", None),
                "version": getattr(instrumentation_scope, "version", None),
            },
            # ASGI send/receive spans are retained for debugging but hidden from
            # the primary operation graph; users can reveal them in the viewer.
            "ui_hidden": _is_framework_transport_span(span, instrumentation_scope),
        }
        duration_ms = _span_duration_ms(span)
        with self._cond:
            self._otel_trace_ids.add(trace_id)
            self._otel_span_nodes[span_id] = node_id
            node = self.nodes.get(node_id)
            if node is None:
                node = Node(
                    id=node_id,
                    name=getattr(span, "name", "OTel span"),
                    module=otel["instrumentation_scope"]["name"] or "opentelemetry",
                    level=Level.TRACE,
                    calls=1,
                    last_ms=duration_ms,
                    avg_ms=duration_ms,
                    kind="otel",
                    otel=otel,
                )
                self.nodes[node_id] = node
                self._bump_graph()
            else:
                node.otel = otel
                node.last_ms = duration_ms
                node.avg_ms = duration_ms
                self._bump_exec()
            parent_node_id = self._otel_span_nodes.get(otel["parent_span_id"])
            if parent_node_id is not None and parent_node_id != node_id:
                self.add_relation(parent_node_id, node_id, "otel_parent", metadata={"span_id": span_id})
        return node_id

    def emit_external_event(self, event: dict) -> None:
        """Send an already-resolved imported event to sidecar/debug sinks."""
        for sink in tuple(self.sinks):
            try:
                sink(event)
            except Exception:
                continue

    def register_return(self, fingerprint: str, node_id: str) -> None:
        """Register the latest direct producer for a fingerprint."""
        with self._cond:
            self._derived_sources.pop(fingerprint, None)
            self._fp_index[fingerprint] = node_id

    def register_derived_sources(self, fingerprint: str, source_ids: tuple[str, ...]) -> None:
        """Register resolved producers for a value derived outside a node."""
        with self._cond:
            self._fp_index.pop(fingerprint, None)
            self._derived_sources[fingerprint] = source_ids

    def lookup_sources(self, fingerprint: str) -> tuple[str, ...]:
        """Return the direct or derived producer IDs for a fingerprint."""
        with self._cond:
            derived_sources = self._derived_sources.get(fingerprint)
            if derived_sources is not None:
                return derived_sources
            direct_source = self._fp_index.get(fingerprint)
            return (direct_source,) if direct_source is not None else ()

    def to_graph_dict(self) -> dict:
        """Serialize to the JSON shape consumed by the D3 renderer.

        Acquires the change lock so concurrent SSE readers (the live viewer)
        get a consistent snapshot even while the recorder is mutating nodes/edges.
        """
        with self._cond:
            categories = sorted({n.module for n in self.nodes.values()})
            data_types = sorted({e.payload_type for e in self.edges.values()})
            return {
                "id": self.id,
                "name": self.name,
                "root": self.root,
                "endpoint": self.endpoint,
                "nodes": [
                    {
                        "id": n.id,
                        "name": n.name,
                        "category": n.module,
                        "env": [],
                        "tags": list(n.tags),
                        "calls": n.calls,
                        "is_async": n.is_async,
                        "last_ms": n.last_ms,
                        "avg_ms": n.avg_ms,
                        "kind": n.kind,
                        "is_start": n.is_start,
                        "otel": n.otel,
                    }
                    for n in self.nodes.values()
                ],
                "links": [
                    {
                        "source": e.source,
                        "target": e.target,
                        "payload": e.payload_type,
                        "size": e.size,
                        "preview": e.preview,
                        "payload_types": list(e.payload_types),
                        "count": e.count,
                        "value": e.value,
                        "kind": e.kind,
                        "metadata": e.metadata,
                    }
                    for e in self.edges.values()
                ],
                # execution timeline: drives the fire/pulse animation. Static
                # renders replay this once on load; the live viewer receives the
                # same events incrementally via SSE.
                "total_ms": sum(n.last_ms or 0 for n in self.nodes.values()),
                "handoffs": sum(e.count for e in self.edges.values()),
                "events": [
                    {
                        "ts": ev.ts,
                        "node_id": ev.node_id,
                        "kind": ev.kind,
                        "edges": ev.edges,
                    }
                    for ev in self.events
                ],
                "categories": categories,
                "dataTypes": data_types,
            }


def _is_framework_transport_span(span: object, instrumentation_scope: object | None) -> bool:
    scope_name = str(getattr(instrumentation_scope, "name", ""))
    span_name = str(getattr(span, "name", ""))
    return scope_name.startswith("opentelemetry.instrumentation.fastapi") and span_name.endswith(
        (" http send", " http receive")
    )


def _span_id(context: object | None) -> str | None:
    span_id = getattr(context, "span_id", 0)
    return f"{span_id:016x}" if span_id else None


def _span_duration_ms(span: object) -> float | None:
    start = getattr(span, "start_time", None)
    end = getattr(span, "end_time", None)
    return (end - start) / 1_000_000 if isinstance(start, int) and isinstance(end, int) else None


class TraceHistory:
    """Thread-safe retained request/run history consumed by the live viewer."""

    def __init__(self, limit: int = 100) -> None:
        self.limit = limit
        self.traces: OrderedDict[str, Trace] = OrderedDict()
        self.version = 0
        self._cond = threading.Condition()

    def _on_trace_change(self, _trace: Trace) -> None:
        with self._cond:
            self.version += 1
            self._cond.notify_all()

    def add(self, trace: Trace) -> Trace:
        """Retain ``trace`` and subscribe to its live changes."""
        with self._cond:
            if trace.id in self.traces:
                return trace
            self.traces[trace.id] = trace
            while len(self.traces) > self.limit:
                self.traces.popitem(last=False)
            self.version += 1
            self._cond.notify_all()
        # Subscribe outside the history lock: trace changes notify this history
        # while holding the trace lock, so reversing those lock orders deadlocks.
        trace.add_listener(self._on_trace_change)
        return trace

    def find_by_otel_trace_id(self, otel_trace_id: str) -> Trace | None:
        """Return the retained pylier trace associated with an OTel trace ID."""
        with self._cond:
            traces = tuple(self.traces.values())
        return next((trace for trace in reversed(traces) if otel_trace_id in trace._otel_trace_ids), None)

    def to_view_dict(self) -> dict:
        """Serialize all retained traces in newest-first viewer order."""
        with self._cond:
            traces = list(self.traces.values())
        return {"traces": [trace.to_graph_dict() for trace in reversed(traces)]}

    def wait_for_change(self, version: int, timeout: float) -> int:
        """Wait until retained history or one of its traces changes."""
        with self._cond:
            if self.version <= version:
                self._cond.wait(timeout=timeout)
            return self.version


__all__ = ["Level", "Node", "Edge", "Event", "Trace", "TraceHistory"]
