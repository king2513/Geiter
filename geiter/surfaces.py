"""Geiter surfaces responsibilities.

    Agent-facing discovery surfaces: status, bounded context, capabilities,
    and client connection configuration.

    This module is one part of the store composition. ``GeiterStore`` mixes it in,
    so behavior is unchanged while each responsibility lives in its own file.
"""

from __future__ import annotations

from typing import Any
from .core import DEFAULT_ACTION_LEASE_SECONDS, now
from . import __version__


class SurfacesMixin:

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
