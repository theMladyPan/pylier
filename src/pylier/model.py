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
    to ``DEBUG`` enables core+info+debug nodes. Metadata richness (size,
    preview, tags) is driven by the effective level too:

    * ``CORE``  - structural identity only (node + bare edges), no payload detail
    * ``INFO``  - + payload type + size
    * ``DEBUG`` - + short value preview + user tags
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
    tags: dict[str, str] = field(default_factory=dict)
    calls: int = 0


@dataclass
class Edge:
    """A directed data flow between two nodes."""

    source: str
    target: str
    payload_type: str = "unknown"
    size: int | None = None
    preview: str | None = None
    tags: dict[str, str] = field(default_factory=dict)
    count: int = 1


@dataclass
class Event:
    """A raw recorder event (enter/exit of a node call)."""

    ts: float
    node_id: str
    kind: str  # "enter" | "exit"
    fingerprint: str | None = None
    return_type: str | None = None


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
        # live-change notification: bumped (under _cond) whenever nodes/edges
        # change so push-based viewers (SSE) can stream updates without polling.
        self.version: int = 0
        self._cond = threading.Condition()

    def _bump(self) -> None:
        """Notify waiters that the graph changed. Caller must hold ``_cond``."""
        self.version += 1
        self._cond.notify_all()

    def wait_for_change(self, since: int, timeout: float) -> int:
        """Block until the graph version exceeds ``since`` or ``timeout`` elapses.

        Returns the current version (== ``since`` on timeout, i.e. no change).
        Used by the SSE viewer to push updates only when the graph actually
        changes instead of polling on a fixed interval.
        """
        with self._cond:
            if self.version <= since:
                self._cond.wait(timeout=timeout)
            return self.version

    def snapshot(self) -> dict:
        """Thread-safe copy of the current graph dict (acquires the change lock)."""
        with self._cond:
            return self.to_graph_dict()

    def get_or_create_node(self, node: Node) -> Node:
        with self._cond:
            existing = self.nodes.get(node.id)
            if existing is None:
                self.nodes[node.id] = node
                self._bump()
                return node
            existing.calls += 1
            self._bump()
            return existing

    def add_edge(
        self,
        source: str,
        target: str,
        *,
        payload_type: str,
        size: int | None,
        preview: str | None,
        tags: dict[str, str],
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
                    tags=dict(tags),
                )
                self.edges[key] = edge
            else:
                edge.count += 1
                if size is not None:
                    edge.size = size
                if preview is not None:
                    edge.preview = preview
                edge.tags.update(tags)
            self._bump()
            return edge

    def register_return(self, fingerprint: str, node_id: str) -> None:
        self._fp_index[fingerprint] = node_id

    def lookup_source(self, fingerprint: str) -> str | None:
        return self._fp_index.get(fingerprint)

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
                        "tags": n.tags,
                        "calls": n.calls,
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
                        "tags": e.tags,
                        "count": e.count,
                    }
                    for e in self.edges.values()
                ],
                "categories": categories,
                "dataTypes": data_types,
            }


__all__ = ["Level", "Node", "Edge", "Event", "Trace"]
