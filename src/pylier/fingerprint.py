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
from typing import Any

_PREVIEW_LIMIT = 80


def type_name(value: Any) -> str:
    cls = type(value)
    return getattr(cls, "__qualname__", None) or cls.__name__


def size_of(value: Any) -> int | None:
    try:
        return len(value)
    except TypeError:
        return None


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


__all__ = ["fingerprint", "type_name", "size_of", "preview_of"]
