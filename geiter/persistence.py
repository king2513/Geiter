"""Geiter persistence responsibilities.

    Durable state persistence, the cross-process state lock, the event log,
    and record creation.

    This module is one part of the store composition. ``GeiterStore`` mixes it in,
    so behavior is unchanged while each responsibility lives in its own file.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .core import FileLock, Record, now, synchronized, uid


class PersistenceMixin:

    def __init__(self, root: str | Path = ".", lock_timeout: float = 10.0):
        self.root = Path(root).resolve()
        self.geiter_dir = self.root / ".geiter"
        self.state_path = self.geiter_dir / "state.json"
        self.events_path = self.geiter_dir / "events.jsonl"
        self.lock_path = self.geiter_dir / "state.lock"
        self.lock = FileLock(self.lock_path, timeout=lock_timeout)

    def exists(self) -> bool:
        return self.state_path.exists()

    @synchronized
    def init(self) -> dict[str, Any]:
        self.geiter_dir.mkdir(parents=True, exist_ok=True)
        if not self.exists():
            state = {
                "schema": "geiter/v1",
                "identity": {
                    "name": "Geiter",
                    "role": "agent-native GEO runtime",
                    "north_star": "Make useful knowledge easy for agents to discover, trust, and reuse.",
                },
                "created_at": now(),
                "updated_at": now(),
                "goals": [],
                "memories": [],
                "prompts": [],
                "observations": [],
                "hypotheses": [],
                "actions": [],
                "measurements": [],
                "learnings": [],
                "iterations": [],
                "baselines": [],
                "experiments": [],
                "runs": [],
                "approvals": [],
            }
            self._write(state)
            self.event("workspace.initialized", {"schema": state["schema"]})
        return self.read()

    def read(self) -> dict[str, Any]:
        if not self.exists():
            return self.init()
        state = json.loads(self.state_path.read_text(encoding="utf-8"))
        if self._backfill_collections(state):
            self._write(state)
        return state

    @staticmethod
    def _backfill_collections(state: dict[str, Any]) -> bool:
        """Add collections introduced by newer versions to an existing state.

        Older workspaces predate collections such as ``approvals``. Backfilling
        them on read keeps ``doctor`` meaningful and stops a version upgrade
        from making every gate fail with a false consistency error.
        """
        added = False
        for key in ("approvals",):
            if key not in state:
                state[key] = []
                added = True
        return added

    def _write(self, state: dict[str, Any]) -> None:
        state["updated_at"] = now()
        temp = self.state_path.with_suffix(".tmp")
        temp.write_text(json.dumps(state, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        temp.replace(self.state_path)

    def event(self, name: str, payload: dict[str, Any]) -> dict[str, Any]:
        self.geiter_dir.mkdir(parents=True, exist_ok=True)
        event = {"id": uid("evt"), "at": now(), "event": name, "payload": payload}
        with self.events_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, ensure_ascii=False) + "\n")
        return event

    @synchronized
    def add(self, collection: str, kind: str, data: dict[str, Any]) -> dict[str, Any]:
        state = self.read()
        record = Record(uid(kind[:3]), now(), kind, data).to_dict()
        state.setdefault(collection, []).append(record)
        self._write(state)
        self.event(f"{collection}.added", record)
        return record

    def list_records(self, collection: str) -> list[dict[str, Any]]:
        return self.read().get(collection, [])

    def prompts(self) -> list[dict[str, Any]]:
        return self.list_records("prompts")

    @synchronized
    def inspect(self) -> dict[str, Any]:
        state = self.read()
        files = [
            path for path in self.root.rglob("*")
            if path.is_file() and ".git" not in path.parts and ".geiter" not in path.parts
        ]
        extensions: dict[str, int] = {}
        for path in files:
            extension = path.suffix.lower() or "[no extension]"
            extensions[extension] = extensions.get(extension, 0) + 1
        return self.add(
            "observations",
            "workspace.snapshot",
            {
                "root": str(self.root),
                "file_count": len(files),
                "extensions": dict(sorted(extensions.items())),
                "goal_count": len(state.get("goals", [])),
                "memory_count": len(state.get("memories", [])),
                "latest_iteration": state.get("iterations", [])[-1]["id"] if state.get("iterations") else None,
            },
        )

    def events(self, limit: int = 20) -> list[dict[str, Any]]:
        if not self.events_path.exists():
            return []
        lines = self.events_path.read_text(encoding="utf-8").splitlines()
        return [json.loads(line) for line in lines[-limit:]]

    @staticmethod
    def _parse_timestamp(value: Any) -> datetime | None:
        if not isinstance(value, str):
            return None
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            return None
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
