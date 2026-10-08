"""Geiter experiments responsibilities.

    Policy-gated experiment proposals, approvals, and structured results.

    This module is one part of the store composition. ``GeiterStore`` mixes it in,
    so behavior is unchanged while each responsibility lives in its own file.
"""

from __future__ import annotations

from typing import Any
from .core import synchronized, now


class ExperimentsMixin:

    @synchronized
    def propose_experiment(self, hypothesis: str, change: str, risk: str = "low") -> dict[str, Any]:
        if not hypothesis.strip() or not change.strip():
            raise ValueError("experiment hypothesis and change are required")
        experiment = self.add(
            "experiments",
            "experiment.proposal",
            {
                "hypothesis": hypothesis.strip(),
                "change": change.strip(),
                "risk": risk,
                "status": "proposed",
                "approval_required": True,
                "external_side_effects": False,
            },
        )
        self.event("experiments.proposed", experiment)
        return experiment

    @synchronized
    def approve_experiment(self, experiment_id: str) -> dict[str, Any]:
        state = self.read()
        experiment = next((item for item in state.get("experiments", []) if item["id"] == experiment_id), None)
        if experiment is None:
            raise ValueError(f"unknown experiment id: {experiment_id}")
        if experiment["data"].get("external_side_effects"):
            raise ValueError("experiments with external side effects require an explicit adapter")
        experiment["data"]["status"] = "approved"
        experiment["data"]["approved_at"] = now()
        self._write(state)
        self.event("experiments.approved", experiment)
        return experiment

    @synchronized
    def record_experiment_result(
        self,
        experiment_id: str,
        outcome: str,
        evidence: str,
        baseline_id: str | None = None,
    ) -> dict[str, Any]:
        if outcome not in {"supported", "rejected", "inconclusive"}:
            raise ValueError("outcome must be supported, rejected, or inconclusive")
        state = self.read()
        experiment = next((item for item in state.get("experiments", []) if item["id"] == experiment_id), None)
        if experiment is None:
            raise ValueError(f"unknown experiment id: {experiment_id}")
        if experiment["data"].get("status") != "approved":
            raise ValueError("experiment must be approved before recording a result")
        comparison = self.compare(baseline_id)
        result = self.add(
            "learnings",
            "experiment.result",
            {
                "experiment_id": experiment_id,
                "outcome": outcome,
                "evidence": evidence.strip(),
                "comparison": comparison,
                "recorded_at": now(),
            },
        )
        state = self.read()
        experiment = next(item for item in state["experiments"] if item["id"] == experiment_id)
        experiment["data"]["status"] = "completed"
        experiment["data"]["outcome"] = outcome
        experiment["data"]["result_id"] = result["id"]
        self._write(state)
        self.event("experiments.completed", {"experiment_id": experiment_id, "result_id": result["id"]})
        return result
