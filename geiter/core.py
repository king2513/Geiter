from __future__ import annotations

import json
import hashlib
import time
import uuid
import functools
import threading
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from . import __version__


DEFAULT_ACTION_LEASE_SECONDS = 3600

try:  # pragma: no cover - platform import guard
    import fcntl  # type: ignore
except ImportError:  # pragma: no cover - Windows fallback
    fcntl = None  # type: ignore

try:  # pragma: no cover - platform import guard
    import msvcrt  # type: ignore
except ImportError:  # pragma: no cover - POSIX fallback
    msvcrt = None  # type: ignore


class FileLock:
    """A reentrant, cross-process advisory lock backed by a sidecar lock file.

    The lock protects the read-modify-write cycle around ``state.json`` so two
    agents (or two processes) cannot interleave and lose an update. It uses
    ``msvcrt.locking`` on Windows and ``fcntl.flock`` elsewhere, writing to a
    dedicated ``.geiter/state.lock`` file so the state file itself is never
    held open across the critical section.

    Reentrancy is tracked with thread-local state, not instance-wide state, so
    two threads sharing one store still contend for the real file lock while a
    single thread can safely nest calls such as ``iterate`` -> ``inspect`` ->
    ``add``.
    """

    def __init__(self, path: Path, timeout: float = 10.0, poll_interval: float = 0.01):
        self.path = path
        self.timeout = timeout
        self.poll_interval = poll_interval
        self._local = threading.local()

    @property
    def _depth(self) -> int:
        return getattr(self._local, "depth", 0)

    @_depth.setter
    def _depth(self, value: int) -> None:
        self._local.depth = value

    @property
    def _handle(self) -> Any:
        return getattr(self._local, "handle", None)

    @_handle.setter
    def _handle(self, value: Any) -> None:
        self._local.handle = value

    def _acquire_file(self, handle: Any) -> None:
        if fcntl is not None:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        elif msvcrt is not None:  # pragma: no cover - Windows path
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)

    def _release_file(self, handle: Any) -> None:
        if fcntl is not None:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        elif msvcrt is not None:  # pragma: no cover - Windows path
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)

    def __enter__(self) -> FileLock:
        if self._depth > 0:
            self._depth += 1
            return self
        self.path.parent.mkdir(parents=True, exist_ok=True)
        deadline = time.monotonic() + self.timeout
        handle = open(self.path, "a+b")
        while True:
            try:
                self._acquire_file(handle)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    handle.close()
                    raise TimeoutError(f"could not acquire geiter state lock: {self.path}")
                time.sleep(self.poll_interval)
        self._handle = handle  # type: ignore[misc]
        self._depth = 1
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        if self._depth == 0:
            return
        self._depth -= 1
        if self._depth == 0:
            handle = self._handle
            if handle is not None:
                try:
                    self._release_file(handle)
                finally:
                    handle.close()
                    self._handle = None


def synchronized(method: Any) -> Any:
    """Serialize a mutating store method across processes.

    The wrapped method runs while holding the store's state lock, so a full
    read-modify-write cycle is atomic with respect to other processes.
    """

    @functools.wraps(method)
    def wrapper(self: GeiterStore, *args: Any, **kwargs: Any) -> Any:
        with self.lock:
            return method(self, *args, **kwargs)

    return wrapper


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

    @synchronized
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

    @synchronized
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
            "version": __version__,
            "identity": self.read()["identity"],
            "state_schema": "geiter/v1",
            "report_schemas": [
                "geiter/analysis-v1",
                "geiter/score-v1",
                "geiter/introspect-v1",
                "geiter/health-v1",
                "geiter/matrix-v1",
                "geiter/comparison-v1",
                "geiter/doctor-v1",
                "geiter/report-v1",
                "geiter/context-v1",
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
                "run": "python -m geiter run --provider <name> [provider options] --json",
                "regression": (
                    "python -m geiter regression --provider <name> "
                    "[provider options] [--baseline-id <id>] --json"
                ),
                "resume": "python -m geiter resume <run-id> --provider <name> [provider options] --json",
                "iteration": "python -m geiter iterate --json",
                "context": "python -m geiter context --json",
                "score": "python -m geiter score --json",
                "introspect": "python -m geiter introspect --json",
                "actions": "python -m geiter action list --json",
                "propose_action": "python -m geiter action propose --type <type> --prompt <text> --json",
            },
            "providers": {
                "built_in": {
                    "fixture": {"network": False, "deterministic": True},
                    "jsonl": {"network": False, "deterministic": True},
                    "http_json": {
                        "network": True,
                        "deterministic": False,
                        "request": "POST {\"prompt\":\"...\"}",
                        "response": "{\"answer\":\"...\",\"citations\":[]}",
                        "retryable_http_statuses": [408, 425, 429, "5xx"],
                    },
                },
                "plugin_entrypoint": "geiter.providers",
            },
            "regression_gate": {
                "schema": "geiter/gate-v1",
                "comparison_statuses": ["no_baseline", "unknown_baseline", "compared"],
                "verdicts": ["improved", "flat", "regressed", "mixed", "insufficient_data"],
                "passing_verdicts": ["improved", "flat"],
                "persisted_on": "runs[].data.gate",
                "latest_report_field": "latest_gate",
            },
            "self_assessment": {
                "score_schema": "geiter/score-v1",
                "introspect_schema": "geiter/introspect-v1",
                "experience_schema": "geiter/experience-v1",
                "score_range": [0, 100],
                "score_direction": "higher_is_better",
                "score_statuses": ["scored", "insufficient_data"],
                "insufficient_data_rule": (
                    "fewer than the minimum usable sample produces no north_star value"
                ),
                "dimensions": ["mention", "target_citation", "citation_rank", "coverage"],
                "introspect_statuses": ["actionable", "healthy"],
                "read_only": True,
            },
            "autonomous_execution": {
                "schema": "geiter/execution-v1",
                "surface": "local sandbox directory (.geiter/../sandbox)",
                "cycle": ["propose", "snapshot", "apply", "re-observe", "gate", "accept_or_revert"],
                "arbiter": "regression gate",
                "on_gate_failure": "revert the surface to its pre-change snapshot",
                "safety": (
                    "edits only a local sandbox surface, never external or real content; "
                    "a change is kept only when the evidence gate accepts it"
                ),
                "entrypoint": "python -m geiter execute --heading <heading> --body <body> --json",
            },
            "change_governance": {
                "schema": "geiter/approvals-v1",
                "policy_levels": ["auto", "approve", "deny"],
                "default_for_unknown_surfaces": "approve",
                "trusted_surface": "sandbox",
                "approval_states": ["pending_approval", "approved", "rejected"],
                "guarantee": (
                    "a surface other than the sandbox is never mutated without an explicit "
                    "policy decision; approval authorizes the attempt, never the outcome, "
                    "because the regression gate still arbitrates"
                ),
                "entrypoints": {
                    "propose": "python -m geiter govern propose --surface-id <id> --heading <h> --body <b> --json",
                    "approve": "python -m geiter govern approve <request-id> --approver <who> --json",
                    "reject": "python -m geiter govern reject <request-id> --approver <who> --json",
                    "list": "python -m geiter govern list --json",
                },
            },
            "action_queue": {
                "record_kind": "agent.action",
                "statuses": ["open", "in_progress", "completed", "skipped"],
                "virtual_filters": ["stale"],
                "operations": ["list", "propose", "complete", "skip", "reclaim"],
                "resolution_evidence_required": True,
                "resolution_evidence_rule": "at least one non-empty scalar value",
                "ordering": "priority_then_created_at",
                "resolution_outcomes": ["completed", "skipped"],
                "active_status": "in_progress",
                "claim_ownership": "only the same claimant may repeat a claim; other claimants are rejected",
                "default_lease_seconds": DEFAULT_ACTION_LEASE_SECONDS,
                "recovery": "stale in_progress actions can be reclaimed to open",
            },
            "principles": [
                "evidence-first",
                "replay-before-reach",
                "agent-first",
                "external-effects-require-policy",
                "concurrent-writes-are-serialized",
            ],
            "concurrency": {
                "state_lock": ".geiter/state.lock",
                "mechanism": "reentrant advisory lock (fcntl.flock / msvcrt.locking)",
                "guarantee": "state read-modify-write cycles are atomic across processes",
                "on_contention": "raise TimeoutError instead of corrupting state",
            },
        }

    def status(self) -> dict[str, Any]:
        state = self.read()
        return {
            "schema": state["schema"],
            "identity": state["identity"],
            "updated_at": state["updated_at"],
            "counts": {key: len(state.get(key, [])) for key in (
                "goals", "memories", "prompts", "observations", "hypotheses",
                "actions", "measurements", "learnings", "iterations",
                "baselines", "experiments", "runs",
            )},
            "open_action_count": len(self.list_actions()),
            "in_progress_action_count": len(self.list_actions("in_progress")),
            "stale_action_count": len(self.stale_actions()),
        }

    def context(self) -> dict[str, Any]:
        """Return a bounded, read-only bootstrap packet for an agent caller."""
        report = self.report()
        latest_iteration = report["latest_iteration"]
        latest_learning = report["latest_learnings"][-1] if report["latest_learnings"] else None
        latest_gate = report.get("latest_gate")
        next_prompt = (
            latest_learning.get("data", {}).get("next_prompt")
            if latest_learning else None
        )
        gate_context = self._gate_context(latest_gate)
        next_action = (
            latest_gate.get("action", {}).get("prompt")
            if latest_gate and not latest_gate.get("ok") and latest_gate.get("action")
            else report["analysis"].get("next_action")
        )
        return {
            "schema": "geiter/context-v1",
            "generated_at": now(),
            "identity": report["identity"],
            "capabilities": self.capabilities(),
            "status": self.status(),
            "decision": {
                "next_action": next_action,
                "next_prompt": next_prompt or next_action,
                "open_actions": report["open_actions"][:10],
                "in_progress_actions": report["in_progress_actions"][:10],
                "stale_actions": report["stale_actions"][:10],
                "approved_experiment_count": sum(
                    item["data"].get("status") == "approved"
                    for item in report["experiments"]
                ),
                "latest_gate": gate_context,
            },
            "quality": {
                "health": report["health"],
                "doctor": report["doctor"],
            },
            "self_assessment": {
                "score": report["score"]["status"],
                "north_star": report["score"]["north_star"],
                "weakest_dimension": report["score"]["weakest_dimension"],
                "next_opportunity": report["introspect"]["next_action"],
                "experience": report["experience"],
            },
            "latest_iteration": latest_iteration,
            "latest_learning": latest_learning,
        }

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

    @staticmethod
    def _parse_timestamp(value: Any) -> datetime | None:
        if not isinstance(value, str):
            return None
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            return None
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)

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

    @synchronized
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

    @synchronized
    def record_observation(
        self,
        prompt_id: str,
        provider: str,
        answer: str,
        citations: list[str] | None = None,
        target: str | None = None,
        run_id: str | None = None,
    ) -> dict[str, Any]:
        state = self.read()
        prompt = next((item for item in state["prompts"] if item["id"] == prompt_id), None)
        if prompt is None:
            raise ValueError(f"unknown prompt id: {prompt_id}")
        if run_id is not None:
            run = next((item for item in state.get("runs", []) if item["id"] == run_id), None)
            if run is None:
                raise ValueError(f"unknown run id: {run_id}")
            if prompt_id not in run["data"].get("prompt_ids", []):
                raise ValueError(f"prompt {prompt_id} is not part of run {run_id}")
        clean_citations = list(dict.fromkeys(citations or []))
        normalized_answer = answer.strip()
        answer_sha256 = hashlib.sha256(normalized_answer.encode("utf-8")).hexdigest()
        invalid_citations = [citation for citation in clean_citations if not urlparse(citation).scheme]
        quality = {
            "answer_present": bool(normalized_answer),
            "citations_valid": not invalid_citations,
            "duplicate": any(
                item["data"].get("prompt_id") == prompt_id
                and item["data"].get("provider") == provider
                and item["data"].get("answer_sha256") == answer_sha256
                and (
                    item["data"].get("run_id") == run_id
                    if run_id is not None
                    else item["data"].get("run_id") is None
                )
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
        observation_data = {
            "prompt_id": prompt_id,
            "prompt": prompt["data"]["text"],
            "provider": provider,
            "answer": normalized_answer,
            "answer_sha256": answer_sha256,
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
        }
        if run_id is not None:
            observation_data["run_id"] = run_id
        record = Record(
            uid("obs"),
            now(),
            "retrieval.observation",
            observation_data,
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
