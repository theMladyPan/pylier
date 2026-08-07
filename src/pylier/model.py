"""Core data model: nodes, edges, events, traces, and capture levels.

These types are the single source of truth shared by the recorder, the
tracing backends (in-memory / sidecar / OTel receiver), and the render core.
"""

from __future__ import annotations

import threading
from collections import OrderedDict
from dataclasses import dataclass, field
from enum import IntEnum


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
        self.name = name
        self.nodes: OrderedDict[str, Node] = OrderedDict()
        self.edges: OrderedDict[tuple[str, str], Edge] = OrderedDict()
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

    def _bump_graph(self) -> None:
        """Notify topology change (new node/edge). Caller holds ``_cond``."""
        self.graph_version += 1
        self._cond.notify_all()

    def _bump_exec(self) -> None:
        """Notify execution event appended. Caller holds ``_cond``."""
        self.exec_version += 1
        self._cond.notify_all()

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
        payload_type: str,
        size: int | None,
        preview: str | None,
        payload_types: tuple[str, ...] = (),
        value: str | None = None,
    ) -> Edge:
        with self._cond:
            key = (source, target)
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
                )
                self.edges[key] = edge
                self._bump_graph()
            else:
                edge.count += 1
                if size is not None:
                    edge.size = size
                if preview is not None:
                    edge.preview = preview
                if value is not None:
                    edge.value = value
                if payload_types:
                    edge.payload_types = payload_types
            return edge

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
                "name": self.name,
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


__all__ = ["Level", "Node", "Edge", "Event", "Trace"]
