"""JSONL sidecar sink.

Appends edge-resolved events to a JSONL file for offline inspection and future
cross-process replay. This is the offline half of the logfire-style transport;
the current live viewer observes only its in-memory trace and does not tail a
sidecar file.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

__all__ = ["SidecarBackend"]


class SidecarBackend:
    """Append event dicts as JSON lines to ``path/sidecar_name``.

    Args:
        directory: Directory to write the sidecar into. Created if missing.
        sidecar_name: JSONL filename within ``directory``.
    """

    def __init__(self, directory: Path, sidecar_name: str) -> None:
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.path = self.directory / sidecar_name

    def __call__(self, event: dict[str, Any]) -> None:
        line = json.dumps(event, default=str, ensure_ascii=False)
        # append-only; line buffering so a tailing viewer sees events promptly
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
            fh.flush()

    def read_events(self) -> list[dict[str, Any]]:
        """Return all events written so far (used by the viewer server)."""
        if not self.path.exists():
            return []
        events: list[dict[str, Any]] = []
        with self.path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    events.append(json.loads(line))
        return events
