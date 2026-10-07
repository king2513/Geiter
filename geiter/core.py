from __future__ import annotations

import json
import hashlib
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def uid(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


@dataclass
class Record:
    id: str
    created_at: str
    kind: str
    data: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class GeiterStore:
    """Durable JSON store with an append-only event log."""

    def __init__(self, root: str | Path = "."):
        self.root = Path(root).resolve()
        self.geiter_dir = self.root / ".geiter"
        self.state_path = self.geiter_dir / "state.json"
        self.events_path = self.geiter_dir / "events.jsonl"

    def exists(self) -> bool:
        return self.state_path.exists()

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
            }
            self._write(state)
            self.event("workspace.initialized", {"schema": state["schema"]})
        return self.read()

    def read(self) -> dict[str, Any]:
        if not self.exists():
            return self.init()
        return json.loads(self.state_path.read_text(encoding="utf-8"))

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

    def iterate(self, hypothesis: str | None = None) -> dict[str, Any]:
        state = self.read()
        iteration_id = uid("itr")
        observation = self.inspect()
        comparison = self.compare()
        pending_experiments = [
            item for item in state.get("experiments", [])
            if item["data"].get("status") == "approved"
        ]
        if not hypothesis:
            if pending_experiments:
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
                "pending_experiment_ids": [item["id"] for item in pending_experiments],
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
                "next_prompt": self._next_prompt(comparison, pending_experiments),
            },
        )
        result = {
            "id": iteration_id,
            "created_at": now(),
            "observation": observation,
            "hypothesis": hypothesis_record,
            "action": action,
            "comparison": comparison,
            "measurement": measurement,
            "learning": learning,
        }
        state = self.read()
        state["iterations"].append(result)
        self._write(state)
        self.event("iteration.completed", {"iteration_id": iteration_id})
        return result

    @staticmethod
    def _next_prompt(comparison: dict[str, Any], pending_experiments: list[dict[str, Any]]) -> str:
        if pending_experiments:
            return "Collect post-change observations and record the approved experiment result."
        if comparison.get("status") == "no_baseline":
            return "Save a baseline, then run the smallest reproducible observation batch."
        if comparison.get("verdict") == "regressed":
            return comparison.get("next_action", "Investigate the regression before expanding scope.")
        return "Use the latest comparison to choose the smallest next experiment and save a new baseline."

    def events(self, limit: int = 20) -> list[dict[str, Any]]:
        if not self.events_path.exists():
            return []
        lines = self.events_path.read_text(encoding="utf-8").splitlines()
        return [json.loads(line) for line in lines[-limit:]]

    def start_run(
        self,
        provider: str,
        prompt_ids: list[str],
        target: str | None = None,
        max_attempts: int = 1,
    ) -> dict[str, Any]:
        if max_attempts < 1:
            raise ValueError("max_attempts must be at least 1")
        run = self.add(
            "runs",
            "provider.run",
            {
                "provider": provider,
                "prompt_ids": prompt_ids,
                "target": target,
                "status": "running",
                "max_attempts": max_attempts,
                "attempts": [],
            },
        )
        self.event("runs.started", run)
        return run

    def record_run_attempt(
        self,
        run_id: str,
        prompt_id: str,
        status: str,
        observation_id: str | None = None,
        error: str | None = None,
        duration_ms: int | None = None,
        error_type: str | None = None,
        retryable: bool | None = None,
    ) -> dict[str, Any]:
        state = self.read()
        run = next((item for item in state.get("runs", []) if item["id"] == run_id), None)
        if run is None:
            raise ValueError(f"unknown run id: {run_id}")
        attempt = {
            "prompt_id": prompt_id,
            "attempt_number": sum(item["prompt_id"] == prompt_id for item in run["data"]["attempts"]) + 1,
            "status": status,
            "observation_id": observation_id,
            "error": error,
            "error_type": error_type,
            "retryable": retryable,
            "duration_ms": duration_ms,
            "at": now(),
        }
        run["data"]["attempts"].append(attempt)
        self._write(state)
        self.event("runs.attempted", {"run_id": run_id, **attempt})
        return attempt

    def finish_run(self, run_id: str) -> dict[str, Any]:
        state = self.read()
        run = next((item for item in state.get("runs", []) if item["id"] == run_id), None)
        if run is None:
            raise ValueError(f"unknown run id: {run_id}")
        attempts = run["data"]["attempts"]
        expected = set(run["data"]["prompt_ids"])
        completed = {item["prompt_id"] for item in attempts if item["status"] == "succeeded"}
        failed = {item["prompt_id"] for item in attempts if item["status"] == "failed"}
        exhausted = {
            prompt_id for prompt_id in expected
            if sum(item["prompt_id"] == prompt_id for item in attempts) >= run["data"]["max_attempts"]
        }
        eligible = set(self.retryable_prompt_ids(run_id))
        run["data"]["status"] = (
            "completed" if expected <= completed
            else "running" if eligible
            else "partial"
        )
        run["data"]["summary"] = {
            "expected": len(expected),
            "succeeded": len(completed),
            "failed": len(failed),
            "pending": len(expected - completed - failed),
            "exhausted": len(exhausted - completed),
        }
        run["data"]["finished_at"] = now()
        self._write(state)
        self.event("runs.finished", {"run_id": run_id, "status": run["data"]["status"]})
        return run

    def retryable_prompt_ids(self, run_id: str) -> list[str]:
        state = self.read()
        run = next((item for item in state.get("runs", []) if item["id"] == run_id), None)
        if run is None:
            raise ValueError(f"unknown run id: {run_id}")
        attempts = run["data"]["attempts"]
        prompt_ids = []
        for prompt_id in run["data"]["prompt_ids"]:
            history = [item for item in attempts if item["prompt_id"] == prompt_id]
            if any(item["status"] == "succeeded" for item in history):
                continue
            if history and history[-1].get("retryable") is False:
                continue
            if len(history) < run["data"]["max_attempts"]:
                prompt_ids.append(prompt_id)
        return prompt_ids

    def run(self, run_id: str) -> dict[str, Any]:
        state = self.read()
        run = next((item for item in state.get("runs", []) if item["id"] == run_id), None)
        if run is None:
            raise ValueError(f"unknown run id: {run_id}")
        return run

    def connection_config(self) -> dict[str, Any]:
        """Return a portable stdio configuration for agent clients."""
        return {
            "name": "geiter",
            "command": "python",
            "args": ["-m", "geiter", "--root", str(self.root), "gateway"],
            "transport": "stdio",
            "cwd": str(self.root),
            "protocol": "json-rpc",
        }

    def capabilities(self) -> dict[str, Any]:
        """Return a stable machine-readable description of Geiter's contract."""
        return {
            "schema": "geiter/capabilities-v1",
            "version": "1.11.1",
            "identity": self.read()["identity"],
            "state_schema": "geiter/v1",
            "report_schemas": [
                "geiter/analysis-v1",
                "geiter/health-v1",
                "geiter/matrix-v1",
                "geiter/comparison-v1",
                "geiter/doctor-v1",
                "geiter/report-v1",
                "geiter/gate-v1",
            ],
            "transport": {
                "kind": "stdio",
                "protocol": "json-rpc",
                "protocol_version": "2025-06-18",
            },
            "entrypoints": {
                "connect": "python -m geiter connect --format generic --json",
                "gateway": "python -m geiter gateway",
                "regression": "python -m geiter regression --provider jsonl --fixture <path> --json",
                "iteration": "python -m geiter iterate --json",
            },
            "principles": [
                "evidence-first",
                "replay-before-reach",
                "agent-first",
                "external-effects-require-policy",
            ],
        }

    def regression_gate(self, run: dict[str, Any]) -> dict[str, Any]:
        """Evaluate a provider run and workspace health as one machine gate."""
        health = self.health()
        doctor = self.doctor()
        summary = run["data"].get("summary", {})
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
                "ok": health["unusable_count"] == 0,
                "detail": health["unusable_count"],
            },
            {
                "name": "workspace_consistent",
                "ok": doctor["ok"],
                "detail": doctor["next_action"],
            },
        ]
        failed = [check for check in checks if not check["ok"]]
        if not failed:
            action = {
                "type": "continue",
                "priority": "normal",
                "prompt": "Save the passing run as evidence, then compare the next observation batch.",
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
        else:
            action = {
                "type": "repair_workspace",
                "priority": "critical",
                "prompt": "Run doctor, repair workspace consistency failures, then rerun the regression gate.",
            }
        return {
            "schema": "geiter/gate-v1",
            "ok": not failed,
            "run_id": run["id"],
            "provider": run["data"]["provider"],
            "summary": summary,
            "checks": checks,
            "failed_checks": [check["name"] for check in failed],
            "action": action,
            "health": health,
            "doctor": doctor,
            "next_action": "Regression gate passed." if not failed
            else "Repair failed checks before trusting this regression run.",
        }

    def add_prompt(self, text: str, intent: str | None = None) -> dict[str, Any]:
        normalized = " ".join(text.split())
        if not normalized:
            raise ValueError("prompt text must not be empty")
        prompt_id = hashlib.sha256(normalized.casefold().encode("utf-8")).hexdigest()[:16]
        state = self.read()
        existing = next((item for item in state["prompts"] if item["id"] == prompt_id), None)
        if existing:
            return existing
        record = {
            "id": prompt_id,
            "created_at": now(),
            "kind": "prompt",
            "data": {"text": normalized, "intent": intent},
        }
        state["prompts"].append(record)
        self._write(state)
        self.event("prompts.added", record)
        return record

    def record_observation(
        self,
        prompt_id: str,
        provider: str,
        answer: str,
        citations: list[str] | None = None,
        target: str | None = None,
    ) -> dict[str, Any]:
        state = self.read()
        prompt = next((item for item in state["prompts"] if item["id"] == prompt_id), None)
        if prompt is None:
            raise ValueError(f"unknown prompt id: {prompt_id}")
        clean_citations = list(dict.fromkeys(citations or []))
        normalized_answer = answer.strip()
        invalid_citations = [citation for citation in clean_citations if not urlparse(citation).scheme]
        quality = {
            "answer_present": bool(normalized_answer),
            "citations_valid": not invalid_citations,
            "duplicate": any(
                item["data"].get("prompt_id") == prompt_id
                and item["data"].get("provider") == provider
                and item["data"].get("answer_sha256") == hashlib.sha256(normalized_answer.encode("utf-8")).hexdigest()
                for item in state.get("observations", [])
                if item.get("kind") == "retrieval.observation"
            ),
        }
        quality["usable"] = quality["answer_present"] and quality["citations_valid"] and not quality["duplicate"]
        target_text = (target or state["identity"]["name"]).casefold()
        mention = target_text in normalized_answer.casefold()
        target_tokens = [token for token in target_text.replace("-", " ").split() if token]
        positions = []
        for index, citation in enumerate(clean_citations):
            parsed = urlparse(citation)
            source_tokens = [
                token
                for chunk in (parsed.netloc, parsed.path)
                for token in chunk.casefold().replace("-", "/").replace("_", "/").replace(".", "/").split("/")
                if token
            ]
            if (
                target_tokens
                and all(token in source_tokens for token in target_tokens)
                and parsed.path != f"/{target_tokens[0]}"
            ):
                positions.append(index)
            elif (
                len(target_tokens) == 1
                and len(target_tokens[0]) <= 3
                and target_tokens[0] in source_tokens
                and target_text in parsed.netloc.casefold()
            ):
                positions.append(index)
        record = Record(
            uid("obs"),
            now(),
            "retrieval.observation",
            {
                "prompt_id": prompt_id,
                "prompt": prompt["data"]["text"],
                "provider": provider,
                "answer": normalized_answer,
                "answer_sha256": hashlib.sha256(normalized_answer.encode("utf-8")).hexdigest(),
                "citations": clean_citations,
                "quality": quality,
                "target": target or state["identity"]["name"],
                "metrics": {
                    "mention": int(mention),
                    "citation": int(bool(clean_citations)),
                    "target_citation": int(bool(positions)),
                    "citation_position": positions[0] + 1 if positions else None,
                    "citation_reciprocal_rank": round(1 / (positions[0] + 1), 4) if positions else 0.0,
                    "citation_count": len(clean_citations),
                },
            },
        ).to_dict()
        state["observations"].append(record)
        state["measurements"].append({
            "id": uid("met"),
            "created_at": record["created_at"],
            "kind": "retrieval.metrics",
            "data": {"observation_id": record["id"], **record["data"]["metrics"]},
        })
        self._write(state)
        self.event("observations.recorded", record)
        return record

    def analyze(self) -> dict[str, Any]:
        state = self.read()
        observations = [
            item for item in state["observations"]
            if item["kind"] == "retrieval.observation" and item["data"].get("quality", {}).get("usable", True)
        ]
        return self._analyze_observations(state, observations)

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

    def compare(self, baseline_id: str | None = None) -> dict[str, Any]:
        state = self.read()
        baselines = state.get("baselines", [])
        if not baselines:
            return {
                "schema": "geiter/comparison-v1",
                "status": "no_baseline",
                "next_action": "Save a baseline before comparing future observations.",
            }
        baseline = next((item for item in baselines if item["id"] == baseline_id), baselines[-1])
        baseline_observation_ids = set(baseline["data"].get("observation_ids", []))
        current_observations = [
            item for item in state.get("observations", [])
            if item["kind"] == "retrieval.observation" and item["id"] not in baseline_observation_ids
        ]
        baseline_observations = [
            item for item in state.get("observations", [])
            if item["kind"] == "retrieval.observation" and item["id"] in baseline_observation_ids
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
            "baseline_paired": before_analysis,
            "current": current,
            "pairing": {"pair_count": len(pairs), "minimum_pairs": 2, "pairs": pairs},
            "delta": delta,
            "verdict": verdict,
            "next_action": self._comparison_action(delta),
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
            "next_action": GeiterStore._recommendation(metrics, count),
        }

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

    @staticmethod
    def _verdict(delta: dict[str, float | None]) -> str:
        values = [value for value in delta.values() if value is not None]
        if not values:
            return "insufficient_data"
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
        return {
            "schema": "geiter/report-v1",
            "generated_at": now(),
            "identity": state["identity"],
            "goals": state["goals"],
            "memory_count": len(state["memories"]),
            "analysis": analysis,
            "matrix": self.analyze_matrix(),
            "health": self.health(),
            "doctor": doctor,
            "latest_iteration": state["iterations"][-1] if state["iterations"] else None,
            "event_count": len(self.events(limit=100000)),
            "baseline_count": len(state.get("baselines", [])),
            "experiment_count": len(state.get("experiments", [])),
            "run_count": len(state.get("runs", [])),
            "runs": state.get("runs", [])[-10:],
            "experiments": state.get("experiments", []),
            "latest_learnings": state.get("learnings", [])[-10:],
        }
