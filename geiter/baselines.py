"""Geiter baselines responsibilities.

    Baseline snapshots and paired before/after comparisons.

    This module is one part of the store composition. ``GeiterStore`` mixes it in,
    so behavior is unchanged while each responsibility lives in its own file.
"""

from __future__ import annotations

from typing import Any


class BaselinesMixin:

    def save_baseline(self, label: str | None = None) -> dict[str, Any]:
        analysis = self.analyze()
        baseline = self.add(
            "baselines",
            "analysis.baseline",
            {
                "label": label or f"baseline-{len(self.list_records('baselines')) + 1}",
                "analysis_schema": analysis["schema"],
                "metrics": analysis["metrics"],
                "coverage": analysis["coverage"],
                "observation_ids": [item["id"] for item in analysis["observations"]],
            },
        )
        self.event("baselines.saved", baseline)
        return baseline

    def compare(
        self,
        baseline_id: str | None = None,
        current_run_id: str | None = None,
        current_observation_ids: set[str] | None = None,
    ) -> dict[str, Any]:
        state = self.read()
        baselines = state.get("baselines", [])
        if not baselines:
            if baseline_id is not None:
                return {
                    "schema": "geiter/comparison-v1",
                    "status": "unknown_baseline",
                    "baseline_id": baseline_id,
                    "next_action": "The requested baseline does not exist; save or select a valid baseline.",
                }
            return {
                "schema": "geiter/comparison-v1",
                "status": "no_baseline",
                "next_action": "Save a baseline before comparing future observations.",
            }
        if baseline_id is None:
            baseline = baselines[-1]
        else:
            baseline = next((item for item in baselines if item["id"] == baseline_id), None)
            if baseline is None:
                return {
                    "schema": "geiter/comparison-v1",
                    "status": "unknown_baseline",
                    "baseline_id": baseline_id,
                    "next_action": "The requested baseline does not exist; select a valid baseline.",
                }
        baseline_observation_ids = set(baseline["data"].get("observation_ids", []))
        current_observations = [
            item for item in state.get("observations", [])
            if item["kind"] == "retrieval.observation"
            and item["id"] not in baseline_observation_ids
            and (
                (
                    item["id"] in current_observation_ids
                    and item["data"].get("quality", {}).get("usable", True)
                )
                if current_observation_ids is not None
                else current_run_id is None
                or (
                    item["data"].get("run_id") == current_run_id
                    and item["data"].get("quality", {}).get("usable", True)
                )
            )
        ]
        baseline_observations = [
            item for item in state.get("observations", [])
            if item["kind"] == "retrieval.observation"
            and item["id"] in baseline_observation_ids
            and item["data"].get("quality", {}).get("usable", True)
        ]
        def group(items):
            grouped = {}
            for item in items:
                grouped.setdefault((item["data"]["prompt_id"], item["data"]["provider"]), []).append(item)
            return grouped
        baseline_groups = group(baseline_observations)
        current_groups = group(current_observations)
        pairs = []
        paired_before = []
        paired_after = []
        for key in sorted(set(baseline_groups) & set(current_groups)):
            count = min(len(baseline_groups[key]), len(current_groups[key]))
            for before_item, after_item in zip(baseline_groups[key][-count:], current_groups[key][-count:]):
                paired_before.append(before_item)
                paired_after.append(after_item)
                pairs.append({"prompt_id": key[0], "provider": key[1], "baseline_observation_id": before_item["id"], "current_observation_id": after_item["id"]})
        before_analysis = self._analyze_observations(state, paired_before)
        current = self._analyze_observations(state, paired_after)
        before = before_analysis["metrics"]
        after = current["metrics"]
        delta: dict[str, float | None] = {}
        for key in ("mention_rate", "citation_rate", "target_citation_rate", "mean_citation_reciprocal_rank"):
            delta[key] = None if before.get(key) is None or after.get(key) is None else round(after[key] - before[key], 4)
        verdict = self._verdict(delta) if len(pairs) >= 2 else "insufficient_data"
        return {
            "schema": "geiter/comparison-v1",
            "status": "compared",
            "baseline": baseline,
            "scope": {
                "current_run_id": current_run_id,
                "current_observation_count": len(current_observations),
            },
            "baseline_paired": before_analysis,
            "current": current,
            "pairing": {"pair_count": len(pairs), "minimum_pairs": 2, "pairs": pairs},
            "delta": delta,
            "verdict": verdict,
            "next_action": self._comparison_action(delta),
        }

    @staticmethod
    def _verdict(delta: dict[str, float | None]) -> str:
        values = [value for value in delta.values() if value is not None]
        if not values:
            return "insufficient_data"
        if all(value == 0 for value in values):
            return "flat"
        if all(value >= 0 for value in values) and any(value > 0 for value in values):
            return "improved"
        if all(value <= 0 for value in values) and any(value < 0 for value in values):
            return "regressed"
        return "mixed"

    @staticmethod
    def _comparison_action(delta: dict[str, float | None]) -> str:
        if delta.get("target_citation_rate") is not None and delta["target_citation_rate"] < 0:
            return "Prioritize source attribution before increasing prompt volume."
        if delta.get("mention_rate") is not None and delta["mention_rate"] < 0:
            return "Investigate entity clarity and answer coverage before publishing changes."
        return "Use the verdict to select the smallest next experiment and save a new baseline after it."
