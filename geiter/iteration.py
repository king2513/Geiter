"""Geiter iteration responsibilities.

    The self-iteration decision loop and its next-prompt selection.

    This module is one part of the store composition. ``GeiterStore`` mixes it in,
    so behavior is unchanged while each responsibility lives in its own file.
"""

from __future__ import annotations

from typing import Any
from .core import synchronized, now, uid


class IterationMixin:

    @synchronized
    def iterate(self, hypothesis: str | None = None) -> dict[str, Any]:
        recovered_actions = self.recover_stale_actions()
        state = self.read()
        iteration_id = uid("itr")
        observation = self.inspect()
        comparison = self.compare()
        latest_gate = self._latest_gate(state)
        queued_actions = self.list_actions()
        claimed_actions = self.list_actions("in_progress")
        active_action = None
        claim_conflict = None
        if queued_actions:
            try:
                active_action = self.claim_action(queued_actions[0]["id"], iteration_id)
            except ValueError as exc:
                claim_conflict = {
                    "action_id": queued_actions[0]["id"],
                    "reason": str(exc),
                }
        elif claimed_actions:
            claim_conflict = {
                "action_id": claimed_actions[0]["id"],
                "reason": (
                    f"action {claimed_actions[0]['id']} is already claimed by "
                    f"{claimed_actions[0]['data'].get('claimed_by') or 'another agent'}"
                ),
            }
        if (
            latest_gate
            and not latest_gate.get("ok")
            and active_action is None
            and claim_conflict is None
        ):
            gate_action = latest_gate.get("action", {})
            gate_action_record = self.propose_action(
                gate_action.get("type", "resolve_regression_gate"),
                gate_action.get("prompt", "Resolve the latest failed regression gate."),
                gate_action.get("priority", "high"),
                source="regression_gate",
                dedupe_key=f"regression:{latest_gate.get('run_id')}:{gate_action.get('type', 'resolve_regression_gate')}",
                evidence={
                    "run_id": latest_gate.get("run_id"),
                    "baseline_id": latest_gate.get("baseline_id"),
                    "failed_checks": latest_gate.get("failed_checks", []),
                    "reopened_by_iteration": iteration_id,
                },
            )
            try:
                active_action = self.claim_action(gate_action_record["id"], iteration_id)
                if claim_conflict and claim_conflict["action_id"] == gate_action_record["id"]:
                    claim_conflict = None
            except ValueError as exc:
                claim_conflict = {
                    "action_id": gate_action_record["id"],
                    "reason": str(exc),
                }
        gate_action_reopened = bool(
            latest_gate
            and not latest_gate.get("ok")
            and active_action
            and active_action.get("data", {}).get("evidence", {}).get("reopened_by_iteration")
            and active_action.get("data", {}).get("claimed_by") == iteration_id
        )
        pending_experiments = [
            item for item in state.get("experiments", [])
            if item["data"].get("status") == "approved"
        ]
        if not hypothesis:
            if gate_action_reopened:
                hypothesis = (
                    f"Resolve failed regression gate for run {latest_gate.get('run_id')}: "
                    f"{latest_gate.get('next_action') or 'repair the failed gate checks'}"
                )
            elif active_action:
                hypothesis = (
                    f"Advance queued action {active_action['id']}: "
                    f"{active_action['data']['prompt']}"
                )
            elif latest_gate and not latest_gate.get("ok"):
                hypothesis = (
                    f"Resolve failed regression gate for run {latest_gate.get('run_id')}: "
                    f"{latest_gate.get('next_action') or 'repair the failed gate checks'}"
                )
            elif pending_experiments:
                hypothesis = (
                    f"Complete approved experiment {pending_experiments[-1]['id']} "
                    "and record evidence before proposing another change."
                )
            elif comparison.get("status") == "compared" and comparison.get("verdict") == "regressed":
                hypothesis = f"Recover from regression: {comparison['next_action']}"
            elif state.get("goals"):
                hypothesis = f"Progress toward: {state['goals'][-1]['data']['text']}"
            else:
                hypothesis = "A clear machine-readable workspace state improves the next agent decision."
        hypothesis_record = self.add(
            "hypotheses",
            "iteration.hypothesis",
            {"iteration_id": iteration_id, "text": hypothesis, "based_on": observation["id"]},
        )
        action = self.add(
            "actions",
            "iteration.action",
            {
                "iteration_id": iteration_id,
                "type": "record",
                "description": "Persist the hypothesis and make it available to the next agent.",
                "comparison_status": comparison.get("status"),
                "comparison_verdict": comparison.get("verdict"),
                "gate_run_id": latest_gate.get("run_id") if latest_gate else None,
                "gate_ok": latest_gate.get("ok") if latest_gate else None,
                "gate_comparison_status": latest_gate.get("comparison_status") if latest_gate else None,
                "gate_comparison_verdict": latest_gate.get("comparison_verdict") if latest_gate else None,
                "pending_experiment_ids": [item["id"] for item in pending_experiments],
                "active_action_id": active_action["id"] if active_action else None,
                "active_action_status": active_action["data"]["status"] if active_action else None,
                "active_action_claimed_by": active_action["data"].get("claimed_by") if active_action else None,
                "claim_conflict": claim_conflict,
                "recovered_action_ids": [item["id"] for item in recovered_actions],
                "decision": (
                    "claimed" if active_action
                    else "claim_conflict" if claim_conflict
                    else "no_queued_action"
                ),
            },
        )
        measurement = self.add(
            "measurements",
            "iteration.measurement",
            {
                "iteration_id": iteration_id,
                "metric": "state_completeness",
                "value": sum(bool(state.get(key)) for key in ("goals", "memories", "observations", "hypotheses")),
                "unit": "populated_core_collections",
            },
        )
        learning = self.add(
            "learnings",
            "iteration.learning",
            {
                "iteration_id": iteration_id,
                "text": "The loop is useful when each decision leaves typed evidence behind.",
                "next_prompt": self._next_prompt(
                    comparison,
                    pending_experiments,
                    active_action,
                    latest_gate,
                ),
            },
        )
        result = {
            "id": iteration_id,
            "created_at": now(),
            "observation": observation,
            "hypothesis": hypothesis_record,
            "action": action,
            "active_action": active_action,
            "claim_conflict": claim_conflict,
            "recovered_actions": recovered_actions,
            "comparison": comparison,
            "latest_gate": latest_gate,
            "measurement": measurement,
            "learning": learning,
        }
        state = self.read()
        state["iterations"].append(result)
        self._write(state)
        self.event("iteration.completed", {"iteration_id": iteration_id})
        return result

    @staticmethod
    def _next_prompt(
        comparison: dict[str, Any],
        pending_experiments: list[dict[str, Any]],
        active_action: dict[str, Any] | None = None,
        latest_gate: dict[str, Any] | None = None,
    ) -> str:
        if active_action:
            return active_action["data"]["prompt"]
        if latest_gate and not latest_gate.get("ok"):
            return latest_gate.get("action", {}).get(
                "prompt",
                "Resolve the latest failed regression gate before expanding scope.",
            )
        if pending_experiments:
            return "Collect post-change observations and record the approved experiment result."
        if comparison.get("status") == "no_baseline":
            return "Save a baseline, then run the smallest reproducible observation batch."
        if comparison.get("verdict") == "regressed":
            return comparison.get("next_action", "Investigate the regression before expanding scope.")
        return "Use the latest comparison to choose the smallest next experiment and save a new baseline."
