"""Value fingerprinting for edge inference.

When a decorated node returns, its value is fingerprinted and registered as
originating from that node. When a later decorated node receives an argument
with a matching fingerprint, an edge ``prev -> this`` is recorded. This is the
"inferred from data flow" mechanism: callers never declare edges by hand.

Fingerprints are content-addressed (type + content hash), not object-identity,
so transformed-but-equal copies still link through the pipeline. Hashable values
use the process-stable builtin ``hash``; unhashable containers fall back to a
SHA-1 of a best-effort repr. Fingerprints are only meaningful within a single
recorder process (the sidecar/OTel path stores edges that were already
resolved, so cross-process stability is not required).
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

_PREVIEW_LIMIT = 80
_VALUE_LIMIT = 2000


def type_name(value: Any) -> str:
    cls = type(value)
    return getattr(cls, "__qualname__", None) or cls.__name__


def tuple_member_type_names(value: Any) -> tuple[str, ...]:
    """Return 2–3 distinct member types for a heterogeneous tuple.

    This small structural detail lets the renderer distinguish a tuple carrying
    a few different values without capturing or serializing those values.
    """
    if not isinstance(value, tuple):
        return ()
    member_types = tuple(dict.fromkeys(type_name(member) for member in value))
    return member_types if 2 <= len(member_types) <= 3 else ()


def size_of(value: Any) -> int | None:
    try:
        return len(value)
    except TypeError:
        return None


def serialize_value(value: Any, limit: int | None = _VALUE_LIMIT) -> str:
    """Best-effort full serialization of a payload for edge inspection.

    Logfire-style: capture whatever was passed. JSON-encodable values are
    serialized; others fall back to repr. Binary payloads are truncated to a
    summary (never embedded raw). Output is capped at ``limit`` chars.
    """
    if isinstance(value, (bytes, bytearray, memoryview)):
        head = bytes(value[:16]).hex()
        return f"<{type_name(value)} {len(value)} bytes: {head}…>"
    try:
        text = json.dumps(value, default=str, ensure_ascii=False, indent=2)
    except Exception:
        text = preview_of(value, limit)
    if limit is not None and len(text) > limit:
        text = text[: limit - 1] + "…"
    return text


def preview_of(value: Any, limit: int = _PREVIEW_LIMIT) -> str:
    try:
        text = repr(value)
    except Exception as exc:  # pragma: no cover - defensive against bad __repr__
        text = f"<unrepresentable: {exc!r}>"
    if len(text) > limit:
        text = text[: limit - 1] + "…"
    return text


def fingerprint(value: Any) -> str:
    """Return a stable content fingerprint for ``value``.

    Args:
        value: Any value returned by or passed to a decorated node.

    Returns:
        A short string ``"<type>:<hash>"`` identifying the value's content.
    """
    fp = _content_hash(value)
    return f"{type_name(value)}:{fp}"


def _content_hash(value: Any) -> str:
    try:
        return format(hash(value), "x")
    except TypeError:
        pass
    try:
        payload = repr(value).encode("utf-8", errors="replace")
    except Exception:
        payload = type_name(value).encode("utf-8")
    return hashlib.sha1(payload, usedforsecurity=False).hexdigest()[:12]


__all__ = ["fingerprint", "type_name", "tuple_member_type_names", "size_of", "preview_of", "serialize_value"]
