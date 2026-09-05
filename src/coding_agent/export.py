from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
from typing import Protocol

from coding_agent.domain import Event


class EventReader(Protocol):
    def list_events(self, session_id: str) -> list[Event]:
        ...


def export_events(events: list[Event], destination: str | Path) -> Path:
    """Atomically materialize committed events as a rebuildable JSONL projection."""

    path = Path(destination)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary_path = Path(handle.name)
            for event in events:
                handle.write(
                    json.dumps(event.to_dict(), ensure_ascii=False, sort_keys=True)
                )
                handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
        temporary_path = None
        return path
    finally:
        if temporary_path is not None:
            try:
                temporary_path.unlink()
            except FileNotFoundError:
                pass


def export_trace(
    event_reader: EventReader,
    session_id: str,
    destination: str | Path | None = None,
) -> Path:
    path = Path(destination) if destination is not None else None
    if path is None:
        trace_path = getattr(event_reader, "trace_path", None)
        if trace_path is None:
            raise ValueError("destination is required for an event reader without trace_path")
        path = Path(trace_path(session_id))
    return export_events(event_reader.list_events(session_id), path)
