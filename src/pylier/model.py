"""Core data model: nodes, edges, events, traces, and capture levels.

These types are the single source of truth shared by the recorder, the
tracing backends (in-memory / sidecar) and the render core.
"""

from __future__ import annotations

import threading
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import IntEnum
from time import time
from typing import Any
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
    is_root: bool = False
    # Compatibility field for older renderer payloads; core nodes are pylier nodes.
    kind: str = "pylier"
    is_start: bool = False


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
    # Entry/exit provenance is display metadata; every edge remains a handoff.
    kind: str = "data"
    metadata: dict[str, Any] = field(default_factory=dict)
    # Individual execution handoffs are retained even when the graph combines
    # repeated calls between the same two function nodes.
    handoffs: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class Invocation:
    """One debugger-visible decorated call, independent of aggregate edges."""

    id: str
    node_id: str
    parent_invocation_id: str | None
    arguments: list[str]
    started_at: float
    duration_ms: float | None = None
    result_type: str | None = None
    result_size: int | None = None
    result_preview: str | None = None
    exception: str | None = None
    payload_state: str = "disabled"  # disabled | available | evicted


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
    invocation_id: str | None = None
    parent_invocation_id: str | None = None
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
        self.root_node_id = f"trace.{self.id}"
        self._root_node = Node(
            id=self.root_node_id,
            name=name,
            module="trace",
            level=Level.CORE,
            calls=1,
            is_root=True,
            is_start=True,
        )
        # Keep this collection decorator-only for backwards-compatible SDK
        # inspection; serialization prepends the visual trace root.
        self.nodes: OrderedDict[str, Node] = OrderedDict()
        # One directed pair represents one handoff stream. Entry and exit use
        # opposite directions, so no relation kind is necessary.
        self.edges: OrderedDict[tuple[str, str], Edge] = OrderedDict()
        # Fingerprint-inferred producer -> consumer relations. Kept separate
        # from execution handoffs so each view has one unambiguous meaning.
        self.data_edges: OrderedDict[tuple[str, str], Edge] = OrderedDict()
        self.events: list[Event] = []
        self.invocations: OrderedDict[str, Invocation] = OrderedDict()
        # Full values never enter graph/SSE JSON. This FIFO store exists only
        # for lazy localhost inspector requests in the live viewer.
        self._invocation_payloads: OrderedDict[str, tuple[dict[str, str], int]] = OrderedDict()
        self._payload_bytes = 0
        # event sinks (e.g. SidecarBackend) notified after each resolved event.
        # Typed as callables to keep this module free of tracing-layer imports.
        self.sinks: list[Callable[[dict[str, Any]], None]] = []
        # fp -> source node id that produced this value. Latest producer wins
        # so a transformed-but-equal-content value still links to the most recent
        # origin, which is what callers actually consume.
        self._fp_index: dict[str, tuple[str, str | None]] = {}
        # Derived values retain a resolved set of producer node IDs. This stays
        # fingerprint-agnostic: recorder.py computes fingerprints and passes
        # opaque keys here.
        self._derived_sources: dict[str, tuple[str, ...]] = {}
        self._invocation_sequence = 0
        # live-change notification: two versions sharing one condition.
        # graph_version bumps only when topology changes (new node/edge) so SSE
        # pushes the full graph rarely; exec_version bumps on every enter/exit
        # event so execution animation streams in real time.
        self.graph_version: int = 0
        self.exec_version: int = 0
        self._cond = threading.Condition()
        self._listeners: list[Callable[[Trace], None]] = []

    def create_invocation(
        self, invocation_id: str, node_id: str, parent_invocation_id: str | None, arguments: list[str]
    ) -> None:
        """Create the public metadata record for one decorated call."""
        with self._cond:
            self.invocations[invocation_id] = Invocation(
                id=invocation_id,
                node_id=node_id,
                parent_invocation_id=parent_invocation_id,
                arguments=arguments,
                started_at=time(),
            )

    def complete_invocation(
        self,
        invocation_id: str | None,
        *,
        duration_ms: float | None,
        result_type: str | None,
        result_size: int | None,
        result_preview: str | None,
        exception: str | None,
    ) -> None:
        """Finish an invocation metadata record after return or exception."""
        if invocation_id is None:
            return
        with self._cond:
            invocation = self.invocations.get(invocation_id)
            if invocation is None:
                return
            invocation.duration_ms = duration_ms
            invocation.result_type = result_type
            invocation.result_size = result_size
            invocation.result_preview = result_preview
            invocation.exception = exception

    def store_invocation_payload(
        self, invocation_id: str | None, payload: dict[str, str], max_invocations: int, max_bytes: int
    ) -> None:
        """Retain one full debugger payload, evicting oldest entries first."""
        if invocation_id is None:
            return
        payload_bytes = sum(len(value.encode("utf-8", errors="replace")) for value in payload.values())
        with self._cond:
            invocation = self.invocations.get(invocation_id)
            if invocation is None:
                return
            invocation.payload_state = "available"
            if previous := self._invocation_payloads.pop(invocation_id, None):
                self._payload_bytes -= previous[1]
            self._invocation_payloads[invocation_id] = (payload, payload_bytes)
            self._payload_bytes += payload_bytes
            while self._invocation_payloads and (
                len(self._invocation_payloads) > max_invocations or self._payload_bytes > max_bytes
            ):
                evicted_id, (_, evicted_bytes) = self._invocation_payloads.popitem(last=False)
                self._payload_bytes -= evicted_bytes
                if evicted := self.invocations.get(evicted_id):
                    evicted.payload_state = "evicted"

    def invocation_payload(self, invocation_id: str) -> tuple[str, dict[str, str] | None]:
        """Return ``(state, payload)`` for a live inspector or static debug bundle."""
        with self._cond:
            invocation = self.invocations.get(invocation_id)
            if invocation is None:
                return "missing", None
            item = self._invocation_payloads.get(invocation_id)
            return invocation.payload_state, item[0] if item is not None else None

    def next_invocation_id(self) -> str:
        """Return an ID for one recorded function invocation."""
        with self._cond:
            self._invocation_sequence += 1
            return f"{self.id}:{self._invocation_sequence}"

    def add_listener(self, listener: Callable[[Trace], None]) -> None:
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

    def snapshot(self) -> dict[str, Any]:
        """Thread-safe copy of the current graph dict (acquires the change lock)."""
        with self._cond:
            return self.to_graph_dict()

    def get_or_create_node(self, node: Node) -> Node:
        with self._cond:
            existing = self.nodes.get(node.id)
            if existing is None:
                # Creating the node happens on its first decorated invocation.
                # Count that call too; otherwise node cards under-report every
                # function by one compared with invocation metadata.
                node.calls += 1
                self.nodes[node.id] = node
                self._bump_graph()
                return node
            existing.calls += 1
            # call-count changes are not topology: the viewer increments badges
            # locally from exec events, so no graph push here
            return existing

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
        metadata: dict[str, Any] | None = None,
        handoff: dict[str, Any] | None = None,
    ) -> Edge:
        return self._add_edge_to(
            self.edges,
            source,
            target,
            payload_type=payload_type,
            size=size,
            preview=preview,
            payload_types=payload_types,
            value=value,
            metadata=metadata,
            handoff=handoff,
        )

    def _add_edge_to(
        self,
        collection: OrderedDict[tuple[str, str], Edge],
        source: str,
        target: str,
        *,
        payload_type: str = "unknown",
        size: int | None = None,
        preview: str | None = None,
        payload_types: tuple[str, ...] = (),
        value: str | None = None,
        metadata: dict[str, Any] | None = None,
        handoff: dict[str, Any] | None = None,
    ) -> Edge:
        with self._cond:
            key = (source, target)
            edge = collection.get(key)
            if edge is None:
                edge = Edge(
                    source=source,
                    target=target,
                    payload_type=payload_type,
                    size=size,
                    preview=preview,
                    payload_types=payload_types,
                    value=value,
                    metadata=metadata or {},
                    handoffs=[handoff] if handoff is not None else [],
                )
                collection[key] = edge
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
                if handoff is not None:
                    edge.handoffs.append(handoff)
            return edge

    def register_return(self, fingerprint: str, node_id: str, invocation_id: str | None) -> None:
        """Register the latest direct producer for a fingerprint."""
        with self._cond:
            self._derived_sources.pop(fingerprint, None)
            self._fp_index[fingerprint] = (node_id, invocation_id)

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
            return (direct_source[0],) if direct_source is not None else ()

    def lookup_producers(self, fingerprint: str) -> tuple[tuple[str, str | None, str], ...]:
        """Return producer node, invocation, and provenance for a value."""
        with self._cond:
            derived_sources = self._derived_sources.get(fingerprint)
            if derived_sources is not None:
                return tuple((source_id, None, "derive") for source_id in derived_sources)
            direct_source = self._fp_index.get(fingerprint)
            if direct_source is None:
                return ()
            return ((direct_source[0], direct_source[1], "fingerprint"),)

    def add_data_edge(
        self,
        source: str,
        target: str,
        *,
        payload_type: str = "unknown",
        size: int | None = None,
        preview: str | None = None,
        payload_types: tuple[str, ...] = (),
        value: str | None = None,
        metadata: dict[str, Any] | None = None,
        handoff: dict[str, Any] | None = None,
    ) -> Edge:
        """Add an aggregated fingerprint-inferred producer/consumer relation."""
        return self._add_edge_to(
            self.data_edges,
            source,
            target,
            payload_type=payload_type,
            size=size,
            preview=preview,
            payload_types=payload_types,
            value=value,
            metadata=metadata,
            handoff=handoff,
        )

    @staticmethod
    def _edge_dict(edge: Edge) -> dict[str, Any]:
        """Serialize one relation identically for either graph perspective."""
        return {
            "source": edge.source,
            "target": edge.target,
            "payload": edge.payload_type,
            "size": edge.size,
            "preview": edge.preview,
            "payload_types": list(edge.payload_types),
            "count": edge.count,
            "value": edge.value,
            "kind": edge.kind,
            "metadata": edge.metadata,
            "handoffs": edge.handoffs,
        }

    def to_graph_dict(self) -> dict[str, Any]:
        """Serialize to the JSON shape consumed by the D3 renderer.

        Acquires the change lock so concurrent SSE readers (the live viewer)
        get a consistent snapshot even while the recorder is mutating nodes/edges.
        """
        with self._cond:
            graph_nodes = (self._root_node, *self.nodes.values())
            categories = sorted({n.module for n in graph_nodes})
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
                        "is_root": n.is_root,
                    }
                    for n in graph_nodes
                ],
                # ``links`` remains the application-flow alias for existing
                # renderers and sidecar consumers.
                "links": [self._edge_dict(e) for e in self.edges.values()],
                "perspectives": {
                    "application": {"links": [self._edge_dict(e) for e in self.edges.values()]},
                    "data": {"links": [self._edge_dict(e) for e in self.data_edges.values()]},
                },
                # execution timeline: drives the fire/pulse animation. Static
                # renders replay this once on load; the live viewer receives the
                # same events incrementally via SSE.
                "total_ms": sum(n.last_ms or 0 for n in graph_nodes),
                "handoffs": sum(e.count for e in self.edges.values()),
                "data_transfers": sum(e.count for e in self.data_edges.values()),
                "invocations": [
                    {
                        "id": invocation.id,
                        "node_id": invocation.node_id,
                        "parent_invocation_id": invocation.parent_invocation_id,
                        "arguments": invocation.arguments,
                        "started_at": invocation.started_at,
                        "duration_ms": invocation.duration_ms,
                        "result_type": invocation.result_type,
                        "result_size": invocation.result_size,
                        "result_preview": invocation.result_preview,
                        "exception": invocation.exception,
                        "payload_state": invocation.payload_state,
                    }
                    for invocation in self.invocations.values()
                ],
                "events": [
                    {
                        "ts": ev.ts,
                        "node_id": ev.node_id,
                        "kind": ev.kind,
                        "invocation_id": ev.invocation_id,
                        "parent_invocation_id": ev.parent_invocation_id,
                        "edges": ev.edges,
                    }
                    for ev in self.events
                ],
                "categories": categories,
                "dataTypes": data_types,
            }


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

    def snapshot_traces(self) -> list[Trace]:
        """Return a stable shallow list of retained traces for SSE diffing."""
        with self._cond:
            return list(self.traces.values())

    def to_view_dict(self) -> dict[str, Any]:
        """Serialize all retained traces in newest-first viewer order."""
        return {"traces": [trace.to_graph_dict() for trace in reversed(self.snapshot_traces())]}

    def wait_for_change(self, version: int, timeout: float) -> int:
        """Wait until retained history or one of its traces changes."""
        with self._cond:
            if self.version <= version:
                self._cond.wait(timeout=timeout)
            return self.version


__all__ = ["Level", "Node", "Edge", "Invocation", "Event", "Trace", "TraceHistory"]
