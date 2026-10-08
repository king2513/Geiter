"""Geiter actions responsibilities.

    The persistent agent action queue with owner-aware claims, leases,
    stale recovery, and evidence-backed resolution.

    This module is one part of the store composition. ``GeiterStore`` mixes it in,
    so behavior is unchanged while each responsibility lives in its own file.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any
from .core import synchronized, DEFAULT_ACTION_LEASE_SECONDS, now


class ActionsMixin:

    @synchronized
    def propose_action(
        self,
        action_type: str,
        prompt: str,
        priority: str = "normal",
        source: str = "agent",
        dedupe_key: str | None = None,
        evidence: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Persist an actionable next step without executing external effects."""
        if not isinstance(action_type, str) or not action_type.strip():
            raise ValueError("action_type must be a non-empty string")
        if not isinstance(prompt, str) or not prompt.strip():
            raise ValueError("prompt must be a non-empty string")
        if priority not in {"normal", "high", "critical"}:
            raise ValueError(f"unsupported action priority: {priority}")
        state = self.read()
        key = dedupe_key or f"{source}:{action_type}:{prompt}"
        existing = next(
            (
                item for item in state.get("actions", [])
                if item.get("kind") == "agent.action"
                and item["data"].get("dedupe_key") == key
                and item["data"].get("status") == "open"
            ),
            None,
        )
        if existing:
            return existing
        return self.add(
            "actions",
            "agent.action",
            {
                "type": action_type,
                "prompt": prompt,
                "priority": priority,
                "source": source,
                "dedupe_key": key,
                "status": "open",
                "evidence": evidence or {},
            },
        )

    def list_actions(
        self,
        status: str | None = "open",
        stale_only: bool = False,
        at: str | None = None,
    ) -> list[dict[str, Any]]:
        actions = [
            item for item in self.list_records("actions")
            if item.get("kind") == "agent.action"
        ]
        if status == "stale":
            status = "in_progress"
            stale_only = True
        if status is not None:
            actions = [item for item in actions if item["data"].get("status") == status]
        if stale_only:
            actions = [item for item in actions if self.is_action_stale(item, at=at)]
        rank = {"critical": 0, "high": 1, "normal": 2}
        return sorted(actions, key=lambda item: (rank.get(item["data"].get("priority"), 9), item["created_at"]))

    def is_action_stale(self, action: dict[str, Any], at: str | None = None) -> bool:
        """Return whether an in-progress action has outlived its claim lease."""
        if action.get("kind") != "agent.action" or action.get("data", {}).get("status") != "in_progress":
            return False
        data = action["data"]
        reference = self._parse_timestamp(at) if at else datetime.now(timezone.utc)
        if reference is None:
            raise ValueError("at must be an ISO-8601 timestamp")
        expires_at = self._parse_timestamp(data.get("lease_expires_at"))
        if expires_at is not None:
            return reference >= expires_at
        claimed_at = self._parse_timestamp(data.get("claimed_at"))
        if claimed_at is None:
            return False
        return reference >= claimed_at + timedelta(
            seconds=data.get("claim_lease_seconds", DEFAULT_ACTION_LEASE_SECONDS)
        )

    @synchronized
    def claim_action(
        self,
        action_id: str,
        iteration_id: str,
        lease_seconds: int | float = DEFAULT_ACTION_LEASE_SECONDS,
    ) -> dict[str, Any]:
        if lease_seconds < 0:
            raise ValueError("lease_seconds must be non-negative")
        state = self.read()
        action = next((item for item in state.get("actions", []) if item["id"] == action_id), None)
        if action is None or action.get("kind") != "agent.action":
            raise ValueError(f"unknown action id: {action_id}")
        status = action["data"].get("status")
        if status == "open":
            claimed_at = now()
            action["data"]["status"] = "in_progress"
            action["data"]["claimed_at"] = claimed_at
            action["data"]["claimed_by"] = iteration_id
            action["data"]["claim_lease_seconds"] = lease_seconds
            action["data"]["lease_expires_at"] = (
                datetime.fromisoformat(claimed_at) + timedelta(seconds=lease_seconds)
            ).isoformat()
            self._write(state)
            self.event("actions.claimed", action)
        elif status == "in_progress":
            owner = action["data"].get("claimed_by")
            if owner != iteration_id:
                raise ValueError(
                    f"action {action_id} is already claimed by {owner or 'another agent'}"
                )
        elif status not in {"completed", "skipped"}:
            raise ValueError(f"invalid action status: {status}")
        return action

    def stale_actions(self, at: str | None = None) -> list[dict[str, Any]]:
        return self.list_actions("in_progress", stale_only=True, at=at)

    @synchronized
    def reclaim_action(
        self,
        action_id: str,
        evidence: dict[str, Any] | None = None,
        at: str | None = None,
    ) -> dict[str, Any]:
        state = self.read()
        action = next((item for item in state.get("actions", []) if item["id"] == action_id), None)
        if action is None or action.get("kind") != "agent.action":
            raise ValueError(f"unknown action id: {action_id}")
        status = action["data"].get("status")
        if status in {"open", "completed", "skipped"}:
            return action
        if status != "in_progress":
            raise ValueError(f"invalid action status: {status}")
        if not self.is_action_stale(action, at=at):
            raise ValueError(f"action {action_id} lease is still active")

        data = action["data"]
        data["last_claimed_at"] = data.get("claimed_at")
        data["last_claimed_by"] = data.get("claimed_by")
        data["last_lease_expires_at"] = data.get("lease_expires_at")
        data["status"] = "open"
        data["reclaimed_at"] = now()
        data["reclaim_count"] = data.get("reclaim_count", 0) + 1
        data["reclaim_evidence"] = evidence or {"reason": "lease_expired"}
        for key in ("claimed_at", "claimed_by", "claim_lease_seconds", "lease_expires_at"):
            data.pop(key, None)
        self._write(state)
        self.event("actions.reclaimed", action)
        return action

    def recover_stale_actions(
        self,
        evidence: dict[str, Any] | None = None,
        at: str | None = None,
    ) -> list[dict[str, Any]]:
        """Requeue every expired in-progress action and return recovered records."""
        stale = self.stale_actions(at=at)
        return [self.reclaim_action(item["id"], evidence=evidence, at=at) for item in stale]

    @synchronized
    def resolve_action(
        self,
        action_id: str,
        outcome: str,
        evidence: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        state = self.read()
        action = next((item for item in state.get("actions", []) if item["id"] == action_id), None)
        if action is None or action.get("kind") != "agent.action":
            raise ValueError(f"unknown action id: {action_id}")
        if outcome not in {"completed", "skipped"}:
            raise ValueError(f"unsupported action outcome: {outcome}")
        if action["data"].get("status") in {"completed", "skipped"}:
            return action
        if not isinstance(evidence, dict) or not any(
            isinstance(value, (str, int, float, bool)) and (
                not isinstance(value, str) or bool(value.strip())
            )
            for value in evidence.values()
        ):
            raise ValueError("action resolution requires non-empty evidence")
        action["data"]["status"] = outcome
        action["data"]["resolved_at"] = now()
        action["data"]["resolution"] = outcome
        if evidence:
            action["data"]["resolution_evidence"] = evidence
            action["data"]["completion_evidence" if outcome == "completed" else "skip_evidence"] = evidence
        self._write(state)
        self.event(f"actions.{outcome}", action)
        return action

    def complete_action(self, action_id: str, evidence: dict[str, Any] | None = None) -> dict[str, Any]:
        return self.resolve_action(action_id, "completed", evidence)

    def skip_action(self, action_id: str, evidence: dict[str, Any] | None = None) -> dict[str, Any]:
        return self.resolve_action(action_id, "skipped", evidence)
