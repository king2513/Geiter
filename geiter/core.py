from __future__ import annotations

import json
import hashlib
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
        if not hypothesis:
            if state.get("goals"):
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
                "next_prompt": "Use the latest observation and learning to choose the next smallest experiment.",
            },
        )
        result = {
            "id": iteration_id,
            "created_at": now(),
            "observation": observation,
            "hypothesis": hypothesis_record,
            "action": action,
            "measurement": measurement,
            "learning": learning,
        }
        state = self.read()
        state["iterations"].append(result)
        self._write(state)
        self.event("iteration.completed", {"iteration_id": iteration_id})
        return result

    def events(self, limit: int = 20) -> list[dict[str, Any]]:
        if not self.events_path.exists():
            return []
        lines = self.events_path.read_text(encoding="utf-8").splitlines()
        return [json.loads(line) for line in lines[-limit:]]

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
                "target": target or state["identity"]["name"],
                "metrics": {
                    "mention": int(mention),
                    "citation": int(bool(clean_citations)),
                    "target_citation": int(bool(positions)),
                    "citation_position": positions[0] + 1 if positions else None,
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
            if item["kind"] == "retrieval.observation"
        ]
        return self._analyze_observations(state, observations)

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
        current = self._analyze_observations(state, current_observations)
        before = baseline["data"]["metrics"]
        after = current["metrics"]
        delta: dict[str, float | None] = {}
        for key in ("mention_rate", "citation_rate", "target_citation_rate", "mean_citation_position"):
            delta[key] = None if before.get(key) is None or after.get(key) is None else round(after[key] - before[key], 4)
        return {
            "schema": "geiter/comparison-v1",
            "status": "compared",
            "baseline": baseline,
            "current": current,
            "delta": delta,
            "verdict": self._verdict(delta),
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
            "doctor": doctor,
            "latest_iteration": state["iterations"][-1] if state["iterations"] else None,
            "event_count": len(self.events(limit=100000)),
            "baseline_count": len(state.get("baselines", [])),
            "experiment_count": len(state.get("experiments", [])),
        }
