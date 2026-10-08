"""Geiter gate responsibilities.

    The regression gate that arbitrates whether a change may be kept.

    This module is one part of the store composition. ``GeiterStore`` mixes it in,
    so behavior is unchanged while each responsibility lives in its own file.
"""

from __future__ import annotations

from typing import Any
from .core import now


class GateMixin:

    def regression_gate(
        self,
        run: dict[str, Any],
        baseline_id: str | None = None,
    ) -> dict[str, Any]:
        """Evaluate a provider run, GEO delta, and workspace health as one gate."""
        health = self.health()
        doctor = self.doctor()
        summary = run["data"].get("summary", {})
        run_observation_ids = {
            attempt.get("observation_id")
            for attempt in run["data"].get("attempts", [])
            if attempt.get("observation_id")
        }
        run_observations = [
            item
            for item in self.read().get("observations", [])
            if item.get("kind") == "retrieval.observation"
            and (
                item.get("data", {}).get("run_id") == run["id"]
                or item.get("id") in run_observation_ids
            )
        ]
        unusable_run_observations = [
            item for item in run_observations
            if not item.get("data", {}).get("quality", {}).get("usable", True)
        ]
        comparison = self.compare(
            baseline_id,
            current_run_id=run["id"],
            current_observation_ids=run_observation_ids,
        )
        comparison_status = comparison.get("status")
        comparison_verdict = comparison.get("verdict")
        comparison_ok = comparison_status == "no_baseline" or comparison_verdict in {"improved", "flat"}
        checks = [
            {
                "name": "prompts_present",
                "ok": summary.get("expected", 0) > 0,
                "detail": summary.get("expected", 0),
            },
            {
                "name": "run_completed",
                "ok": run["data"].get("status") == "completed",
                "detail": run["data"].get("status"),
            },
            {
                "name": "observations_usable",
                "ok": (
                    summary.get("succeeded", 0) == len(run_observations)
                    and not unusable_run_observations
                ),
                "detail": {
                    "run_observation_count": len(run_observations),
                    "unusable_count": len(unusable_run_observations),
                    "workspace_unusable_count": health["unusable_count"],
                },
            },
            {
                "name": "workspace_consistent",
                "ok": doctor["ok"],
                "detail": doctor["next_action"],
            },
            {
                "name": "baseline_comparison",
                "ok": comparison_ok,
                "detail": {
                    "status": comparison_status,
                    "verdict": comparison_verdict,
                    "baseline_id": comparison.get("baseline", {}).get("id")
                    if comparison.get("baseline")
                    else baseline_id,
                    "pair_count": comparison.get("pairing", {}).get("pair_count", 0),
                },
            },
        ]
        failed = [check for check in checks if not check["ok"]]
        if not failed:
            action = {
                "type": "continue",
                "priority": "normal",
                "prompt": (
                    "Record the passing GEO run as evidence, then save a new baseline "
                    "before the next change."
                    if comparison_status == "compared"
                    else "Save the passing run as evidence, then compare the next observation batch."
                ),
            }
        elif not checks[0]["ok"]:
            action = {
                "type": "add_prompts",
                "priority": "high",
                "prompt": "Add at least one deterministic retrieval prompt before running the regression gate.",
            }
        elif not checks[1]["ok"]:
            action = {
                "type": "repair_provider_run",
                "priority": "high",
                "prompt": "Inspect failed provider attempts, repair the adapter or fixture, then resume or rerun.",
            }
        elif not checks[2]["ok"]:
            action = {
                "type": "repair_observations",
                "priority": "high",
                "prompt": "Repair unusable observations before trusting GEO comparisons.",
            }
        elif not checks[3]["ok"]:
            action = {
                "type": "repair_workspace",
                "priority": "critical",
                "prompt": "Run doctor, repair workspace consistency failures, then rerun the regression gate.",
            }
        elif comparison_status == "unknown_baseline":
            action = {
                "type": "select_valid_baseline",
                "priority": "high",
                "prompt": (
                    "Select an existing baseline or save a new one, then rerun the "
                    "regression gate with that baseline reference."
                ),
            }
        elif comparison_verdict == "regressed":
            action = {
                "type": "recover_regression",
                "priority": "high",
                "prompt": (
                    "Investigate the GEO regression, restore the weakest metric, "
                    "then rerun this provider against the same baseline."
                ),
            }
        elif comparison_verdict == "mixed":
            action = {
                "type": "investigate_mixed_metrics",
                "priority": "high",
                "prompt": (
                    "Investigate the mixed GEO deltas and isolate the smallest change "
                    "that improves the losing metric before expanding scope."
                ),
            }
        else:
            action = {
                "type": "collect_comparison_evidence",
                "priority": "high",
                "prompt": (
                    "Collect at least two valid prompt/provider pairs after the selected "
                    "baseline before treating this regression result as directional."
                ),
            }
        evidence = {
            "run_id": run["id"],
            "baseline_id": comparison.get("baseline", {}).get("id") if comparison.get("baseline") else baseline_id,
            "failed_checks": [check["name"] for check in failed],
            "comparison_status": comparison_status,
            "comparison_verdict": comparison_verdict,
            "comparison_delta": comparison.get("delta"),
        }
        gate = {
            "schema": "geiter/gate-v1",
            "ok": not failed,
            "run_id": run["id"],
            "provider": run["data"]["provider"],
            "summary": summary,
            "baseline_id": evidence["baseline_id"],
            "comparison": comparison,
            "checks": checks,
            "failed_checks": [check["name"] for check in failed],
            "action": action,
            "action_record": self.propose_action(
                action["type"],
                action["prompt"],
                action["priority"],
                source="regression_gate",
                dedupe_key=f"regression:{run['id']}:{action['type']}",
                evidence=evidence,
            ),
            "health": health,
            "doctor": doctor,
            "next_action": "Regression gate passed." if not failed
            else (
                "Select a valid baseline before trusting this regression run."
                if comparison_status == "unknown_baseline"
                else (
                    "Repair failed checks before trusting this regression run."
                    if comparison_status != "compared"
                    or comparison_verdict not in {"regressed", "mixed", "insufficient_data"}
                    else f"Comparison verdict is {comparison_verdict}; follow the typed action before proceeding."
                )
            ),
        }
        persisted_gate = {
            **self._gate_context(gate),
            "generated_at": now(),
            "summary": summary,
            "comparison": comparison,
        }
        persisted_gate["action"] = action
        persisted_gate["action_id"] = gate["action_record"]["id"]
        state = self.read()
        stored_run = next((item for item in state.get("runs", []) if item["id"] == run["id"]), None)
        if stored_run is not None:
            stored_run["data"]["gate"] = persisted_gate
            self._write(state)
            self.event(
                "runs.gated",
                {
                    "run_id": run["id"],
                    "ok": gate["ok"],
                    "baseline_id": gate["baseline_id"],
                    "comparison_status": comparison_status,
                    "comparison_verdict": comparison_verdict,
                    "failed_checks": gate["failed_checks"],
                    "action_id": gate["action_record"]["id"],
                },
            )
        run["data"]["gate"] = persisted_gate
        return gate
