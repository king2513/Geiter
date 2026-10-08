"""Geiter analysis responsibilities.

    Read-only analysis: GEO metrics, observation health, coverage matrix,
    north-star scoring, self-introspection, and reporting.

    This module is one part of the store composition. ``GeiterStore`` mixes it in,
    so behavior is unchanged while each responsibility lives in its own file.
"""

from __future__ import annotations

from typing import Any
from .core import now


class AnalysisMixin:

    def analyze(self) -> dict[str, Any]:
        state = self.read()
        observations = [
            item for item in state["observations"]
            if item["kind"] == "retrieval.observation" and item["data"].get("quality", {}).get("usable", True)
        ]
        return self._analyze_observations(state, observations)

    def score(self) -> dict[str, Any]:
        """Aggregate retrieval metrics into one direction-safe north-star score.

        Every dimension is normalized so that higher is always better, so a rising
        score always means the knowledge surface is easier to discover and cite.
        Dimensions with too few samples to be meaningful are reported as
        ``insufficient_data`` instead of being folded into a misleading total.
        """
        analysis = self.analyze()
        metrics = analysis["metrics"]
        coverage = analysis["coverage"]
        sample_size = metrics["sample_size"]
        minimum_sample = 2
        dimensions = [
            {
                "name": "mention",
                "value": metrics["mention_rate"],
                "weight": 0.25,
                "sample_size": sample_size,
                "description": "How often providers mention the target by name.",
            },
            {
                "name": "target_citation",
                "value": metrics["target_citation_rate"],
                "weight": 0.35,
                "sample_size": sample_size,
                "description": "How often providers attribute a citation to the target; the strongest trust signal.",
            },
            {
                "name": "citation_rank",
                "value": metrics["mean_citation_reciprocal_rank"],
                "weight": 0.20,
                "sample_size": sample_size,
                "description": "Direction-safe citation rank quality (1 / position).",
            },
            {
                "name": "coverage",
                "value": (
                    round(
                        coverage["observed_prompts"] / coverage["prompts"], 4
                    )
                    if coverage["prompts"]
                    else None
                ),
                "weight": 0.20,
                "sample_size": coverage["prompts"],
                "description": "Share of configured prompts that have at least one usable observation.",
            },
        ]
        scored = [item for item in dimensions if item["value"] is not None]
        total_weight = sum(item["weight"] for item in scored)
        sufficient = sample_size >= minimum_sample
        if not scored or not sufficient or total_weight == 0:
            north_star = None
        else:
            north_star = round(
                sum(item["value"] * item["weight"] for item in scored) / total_weight * 100,
                2,
            )
        ranked = sorted(scored, key=lambda item: (item["value"], -item["weight"]))
        weakest_dimension = ranked[0]["name"] if ranked else None
        return {
            "schema": "geiter/score-v1",
            "generated_at": now(),
            "status": "scored" if north_star is not None else "insufficient_data",
            "north_star": north_star,
            "minimum_sample": minimum_sample,
            "sample_size": sample_size,
            "dimensions": dimensions,
            "weakest_dimension": weakest_dimension,
            "next_action": (
                f"Improve the weakest dimension '{weakest_dimension}' before expanding coverage."
                if weakest_dimension and sufficient
                else "Collect more usable observations before trusting a north-star score."
            ),
        }

    def introspect(self) -> dict[str, Any]:
        """Answer "what should I improve first?" from coverage, health, and score.

        The result is a ranked list of evidence-backed improvement opportunities so
        an agent can pick the smallest highest-value change instead of guessing.
        This method is read-only: it never mutates workspace state.
        """
        state = self.read()
        score = self.score()
        analysis = self.analyze()
        health = self.health()
        matrix = self.analyze_matrix()
        opportunities: list[dict[str, Any]] = []

        unobserved = analysis["coverage"]["unobserved_prompt_ids"]
        if unobserved:
            opportunities.append({
                "opportunity": "observe_uncovered_prompts",
                "priority": "high",
                "evidence": {"unobserved_prompt_ids": unobserved},
                "detail": (
                    f"{len(unobserved)} configured prompts have never been observed; "
                    "their retrieval behavior is unknown."
                ),
            })

        if health["unusable_count"]:
            opportunities.append({
                "opportunity": "repair_unusable_observations",
                "priority": "high",
                "evidence": {"unusable_count": health["unusable_count"]},
                "detail": (
                    f"{health['unusable_count']} observations are unusable and excluded from "
                    "aggregates; repair them before trusting the score."
                ),
            })

        for cell in matrix["weakest_cells"]:
            cell_metrics = cell["analysis"]["metrics"]
            if cell_metrics["target_citation_rate"] == 0 and cell_metrics["sample_size"]:
                opportunities.append({
                    "opportunity": "strengthen_attribution",
                    "priority": "normal",
                    "evidence": {
                        "provider": cell["provider"],
                        "intent": cell["intent"],
                        "target_citation_rate": cell_metrics["target_citation_rate"],
                    },
                    "detail": (
                        f"Cell {cell['provider']}::{cell['intent']} is mentioned but never "
                        "attributed a citation; strengthen attributable source material."
                    ),
                })

        if not health["providers"]:
            opportunities.append({
                "opportunity": "connect_provider",
                "priority": "critical",
                "evidence": {"provider_count": 0},
                "detail": "No provider observations exist yet; add prompts and record a first batch.",
            })

        if score.get("weakest_dimension") and score.get("status") == "scored":
            opportunities.append({
                "opportunity": "improve_weakest_dimension",
                "priority": "normal",
                "evidence": {"weakest_dimension": score["weakest_dimension"]},
                "detail": score["next_action"],
            })

        rank = {"critical": 0, "high": 1, "normal": 2}
        opportunities.sort(key=lambda item: rank.get(item["priority"], 9))
        return {
            "schema": "geiter/introspect-v1",
            "generated_at": now(),
            "status": "actionable" if opportunities else "healthy",
            "opportunity_count": len(opportunities),
            "opportunities": opportunities,
            "score": {
                "status": score["status"],
                "north_star": score["north_star"],
                "weakest_dimension": score["weakest_dimension"],
            },
            "next_action": (
                opportunities[0]["detail"] if opportunities
                else "No structural gaps detected; expand prompt and provider coverage."
            ),
        }

    def health(self) -> dict[str, Any]:
        state = self.read()
        observations = [item for item in state.get("observations", []) if item["kind"] == "retrieval.observation"]
        providers: dict[str, dict[str, Any]] = {}
        for item in observations:
            provider = item["data"]["provider"]
            bucket = providers.setdefault(provider, {"provider": provider, "observations": 0, "usable": 0, "duplicates": 0, "invalid": 0})
            bucket["observations"] += 1
            quality = item["data"].get("quality", {})
            bucket["usable"] += int(quality.get("usable", True))
            bucket["duplicates"] += int(quality.get("duplicate", False))
            bucket["invalid"] += int(not quality.get("citations_valid", True) or not quality.get("answer_present", True))
        for bucket in providers.values():
            bucket["usable_rate"] = round(bucket["usable"] / bucket["observations"], 4) if bucket["observations"] else 0.0
        run_failures: dict[str, dict[str, int]] = {}
        for run in state.get("runs", []):
            provider = run["data"]["provider"]
            bucket = run_failures.setdefault(provider, {"failed": 0, "retryable": 0, "non_retryable": 0})
            for attempt in run["data"].get("attempts", []):
                if attempt["status"] == "failed":
                    bucket["failed"] += 1
                    bucket["retryable"] += int(attempt.get("retryable") is True)
                    bucket["non_retryable"] += int(attempt.get("retryable") is False)
        for provider, bucket in providers.items():
            bucket["run_failures"] = run_failures.get(provider, {"failed": 0, "retryable": 0, "non_retryable": 0})
        unusable = sum(item["observations"] - item["usable"] for item in providers.values())
        return {
            "schema": "geiter/health-v1",
            "generated_at": now(),
            "observation_count": len(observations),
            "usable_count": sum(item["usable"] for item in providers.values()),
            "unusable_count": unusable,
            "providers": sorted(providers.values(), key=lambda item: item["provider"]),
            "next_action": "Repair unusable provider observations before trusting GEO comparisons." if unusable else "Observation quality is healthy.",
        }

    def analyze_matrix(self) -> dict[str, Any]:
        state = self.read()
        observations = [item for item in state["observations"] if item["kind"] == "retrieval.observation"]
        groups: dict[str, list[dict[str, Any]]] = {}
        prompt_map = {item["id"]: item for item in state["prompts"]}
        for observation in observations:
            prompt = prompt_map.get(observation["data"]["prompt_id"], {})
            intent = prompt.get("data", {}).get("intent") or "unspecified"
            key = f"{observation['data']['provider']}::{intent}"
            groups.setdefault(key, []).append(observation)
        cells = []
        for key, items in sorted(groups.items()):
            provider, intent = key.split("::", 1)
            analysis = self._analyze_observations(state, items)
            cells.append({
                "provider": provider,
                "intent": intent,
                "analysis": analysis,
            })
        weakest = sorted(
            cells,
            key=lambda cell: (
                cell["analysis"]["metrics"]["target_citation_rate"] is None,
                cell["analysis"]["metrics"]["target_citation_rate"] or 0,
                cell["analysis"]["metrics"]["mention_rate"] or 0,
            ),
        )[:3]
        return {
            "schema": "geiter/matrix-v1",
            "generated_at": now(),
            "cells": cells,
            "cell_count": len(cells),
            "weakest_cells": weakest,
            "next_action": (
                "Add prompts or provider coverage for the weakest cells."
                if weakest else "Add prompts and provider observations to build the coverage matrix."
            ),
        }

    @staticmethod
    def _analyze_observations(state: dict[str, Any], observations: list[dict[str, Any]]) -> dict[str, Any]:
        count = len(observations)
        metrics = {
            "sample_size": count,
            "mention_rate": round(sum(item["data"]["metrics"]["mention"] for item in observations) / count, 4) if count else None,
            "citation_rate": round(sum(item["data"]["metrics"]["citation"] for item in observations) / count, 4) if count else None,
            "target_citation_rate": round(sum(item["data"]["metrics"]["target_citation"] for item in observations) / count, 4) if count else None,
            "mean_citation_position": round(
                sum(item["data"]["metrics"]["citation_position"] for item in observations if item["data"]["metrics"]["citation_position"] is not None)
                / max(1, sum(item["data"]["metrics"]["citation_position"] is not None for item in observations)),
                2,
            ) if count else None,
            "mean_citation_reciprocal_rank": round(
                sum(item["data"]["metrics"].get("citation_reciprocal_rank", 0.0) for item in observations) / count,
                4,
            ) if count else None,
        }
        observed_prompt_ids = {item["data"]["prompt_id"] for item in observations}
        return {
            "schema": "geiter/analysis-v1",
            "generated_at": now(),
            "metrics": metrics,
            "coverage": {
                "prompts": len(state["prompts"]),
                "observed_prompts": len(observed_prompt_ids),
                "unobserved_prompt_ids": [
                    item["id"] for item in state["prompts"] if item["id"] not in observed_prompt_ids
                ],
            },
            "observations": observations,
            "next_action": AnalysisMixin._recommendation(metrics, count),
        }

    @staticmethod
    def _recommendation(metrics: dict[str, Any], count: int) -> str:
        if count == 0:
            return "Add prompts and record provider observations before drawing conclusions."
        if metrics["mention_rate"] == 0:
            return "Improve entity clarity and authoritative coverage for the observed prompts."
        if metrics["target_citation_rate"] == 0:
            return "Strengthen attributable, crawlable source material; mention alone is not evidence of citation."
        return "Expand prompt and provider coverage, then compare the next observation batch against this baseline."

    def doctor(self) -> dict[str, Any]:
        state = self.read()
        checks: list[dict[str, Any]] = []

        def check(name: str, ok: bool, detail: str) -> None:
            checks.append({"name": name, "ok": ok, "detail": detail})

        check("schema", state.get("schema") == "geiter/v1", str(state.get("schema")))
        required = {
            "identity", "goals", "memories", "prompts", "observations",
            "hypotheses", "actions", "measurements", "learnings", "iterations",
            "approvals",
        }
        missing = sorted(required.difference(state))
        check("collections", not missing, f"missing={missing}" if missing else "all required collections present")
        check("state_file", self.state_path.exists(), str(self.state_path))
        check("event_log", self.events_path.exists(), str(self.events_path))

        invalid_refs: list[str] = []
        prompt_ids = {item["id"] for item in state.get("prompts", [])}
        for item in state.get("observations", []):
            prompt_id = item.get("data", {}).get("prompt_id")
            if item.get("kind") == "retrieval.observation" and prompt_id not in prompt_ids:
                invalid_refs.append(f"{item.get('id')}->{prompt_id}")
        check("references", not invalid_refs, "all observation prompt references resolve" if not invalid_refs else str(invalid_refs))

        events = self.events(limit=100000)
        check("event_log_json", len(events) >= 1, f"{len(events)} events readable")
        failed = [item for item in checks if not item["ok"]]
        return {
            "schema": "geiter/doctor-v1",
            "generated_at": now(),
            "ok": not failed,
            "checks": checks,
            "next_action": "Repair failed checks before trusting autonomous iteration." if failed else "Workspace is internally consistent.",
        }

    def report(self) -> dict[str, Any]:
        state = self.read()
        analysis = self.analyze()
        doctor = self.doctor()
        latest_gate = self._latest_gate(state)
        return {
            "schema": "geiter/report-v1",
            "generated_at": now(),
            "identity": state["identity"],
            "goals": state["goals"],
            "memory_count": len(state["memories"]),
            "analysis": analysis,
            "score": self.score(),
            "introspect": self.introspect(),
            "experience": self._experience_bounded(),            "matrix": self.analyze_matrix(),
            "health": self.health(),
            "doctor": doctor,
            "latest_gate": latest_gate,
            "latest_iteration": state["iterations"][-1] if state["iterations"] else None,
            "event_count": len(self.events(limit=100000)),
            "baseline_count": len(state.get("baselines", [])),
            "experiment_count": len(state.get("experiments", [])),
            "run_count": len(state.get("runs", [])),
            "runs": state.get("runs", [])[-10:],
            "open_action_count": len(self.list_actions()),
            "open_actions": self.list_actions()[:20],
            "in_progress_action_count": len(self.list_actions("in_progress")),
            "in_progress_actions": self.list_actions("in_progress")[:20],
            "stale_action_count": len(self.stale_actions()),
            "stale_actions": self.stale_actions()[:20],
            "experiments": state.get("experiments", []),
            "latest_learnings": state.get("learnings", [])[-10:],
        }

    @staticmethod
    def _gate_context(gate: dict[str, Any] | None) -> dict[str, Any] | None:
        if not gate:
            return None
        comparison = gate.get("comparison", {})
        pairing = comparison.get("pairing", {})
        return {
            "schema": gate.get("schema", "geiter/gate-v1"),
            "run_id": gate.get("run_id"),
            "provider": gate.get("provider"),
            "ok": gate.get("ok"),
            "baseline_id": gate.get("baseline_id"),
            "comparison_status": comparison.get("status"),
            "comparison_verdict": comparison.get("verdict"),
            "comparison_delta": comparison.get("delta"),
            "pair_count": pairing.get("pair_count", 0),
            "failed_checks": gate.get("failed_checks", []),
            "action": gate.get("action"),
            "next_action": gate.get("next_action"),
        }

    def _experience_bounded(self) -> dict[str, Any]:
        """Return a bounded playbook summary without mutating state."""
        try:
            from .execute import experience  # local import avoids a cycle

            report = experience(self)
        except Exception:  # pragma: no cover - never break report on this
            return {"status": "unavailable", "total_executions": 0, "strategies": []}
        return {
            "status": "available",
            "total_executions": report["total_executions"],
            "strategy_count": report["strategy_count"],
            "top_strategy": report["strategies"][0] if report["strategies"] else None,
            "recommendation": report["recommendation"],
        }

    @staticmethod
    def _latest_gate(state: dict[str, Any]) -> dict[str, Any] | None:
        gated_runs = [
            item for item in state.get("runs", [])
            if item.get("data", {}).get("gate")
        ]
        if not gated_runs:
            return None
        return max(
            (item["data"]["gate"] for item in gated_runs),
            key=lambda gate: gate.get("generated_at", ""),
        )
