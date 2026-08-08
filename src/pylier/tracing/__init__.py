"""Tracing sinks.

The in-memory trace is built directly by the recorder. Sinks are optional
*event emitters* that receive already edge-resolved events for offline replay
or the live viewer server. They never fingerprint values themselves — edges are
resolved in the recorder before anything is emitted.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from pylier.tracing.sidecar import SidecarBackend

# A sink receives an edge-resolved recorder event (never a raw value).
Sink = Callable[[dict[str, Any]], None]

__all__ = ["Sink", "SidecarBackend"]
