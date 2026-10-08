"""Autonomous execution for Geiter (P1).

This module lets Geiter close the loop between measurement and action. It is
deliberately conservative: every change is a declarative proposal, every
mutation happens on a local sandbox surface, and every applied change is
verified by the existing regression gate. If the gate does not improve, the
change is reverted automatically. Nothing here touches external or real
content -- the sandbox is a plain directory so the whole cycle is safe,
reversible, and testable.

The flow mirrors the self-iteration loop:

    introspect -> propose change -> apply to sandbox -> re-observe -> gate
               -> accept (keep) or revert (roll back)

Adopting the gate as the arbiter is what makes autonomous execution safe: an
agent may act on its own, but it may not *keep* an action that the evidence
gate rejects.
"""

from __future__ import annotations

import json
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .core import GeiterStore


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# A change kind is the "strategy" Geiter uses to improve the surface. P3 makes
# these learnable: effectiveness aggregates history per kind so later cycles
# prefer strategies that have actually worked.
DEFAULT_CHANGE_KIND = "append_section"
MIN_SAMPLES_FOR_PREFERENCE = 2


def effectiveness(store: GeiterStore) -> dict[str, Any]:
    """Aggregate the historical success of each change kind ("strategy").

    P1 already writes an ``execution.result`` learning for every applied change
    with the change kind, whether it was accepted, and the score before and
    after. This turns that log into a durable playbook: for each kind it
    reports attempts, accept rate, and the average north-star delta, so later
    cycles can prefer strategies that historically worked instead of always
    guessing. A kind with too few samples is reported but not yet preferred.
    """
    buckets: dict[str, dict[str, Any]] = defaultdict(
        lambda: {
            "kind": "",
            "attempts": 0,
            "accepted": 0,
            "reverted": 0,
            "score_deltas": [],
            "weakest_dimensions": [],
        }
    )
    for item in store.read().get("learnings", []):
        if item.get("kind") != "execution.result":
            continue
        data = item.get("data", {})
        verdict = data.get("verdict", {})
        kind = data.get("kind") or verdict.get("kind") or DEFAULT_CHANGE_KIND
        bucket = buckets[kind]
        bucket["kind"] = kind
        bucket["attempts"] += 1
        if data.get("accepted"):
            bucket["accepted"] += 1
        else:
            bucket["reverted"] += 1
        before = verdict.get("score_before")
        after = verdict.get("score_after")
        if isinstance(before, (int, float)) and isinstance(after, (int, float)):
            bucket["score_deltas"].append(round(after - before, 4))
        weakest = (data.get("evidence") or {}).get("weakest_dimension")
        if weakest:
            bucket["weakest_dimensions"].append(weakest)

    strategies = []
    for bucket in buckets.values():
        deltas = bucket.pop("score_deltas")
        attempts = bucket["attempts"]
        accept_rate = round(bucket["accepted"] / attempts, 4) if attempts else 0.0
        mean_delta = round(sum(deltas) / len(deltas), 4) if deltas else None
        strategies.append(
            {
                **bucket,
                "accept_rate": accept_rate,
                "mean_score_delta": mean_delta,
                "improved_count": sum(1 for value in deltas if value > 0),
                "regressed_count": sum(1 for value in deltas if value < 0),
                "preference_eligible": attempts >= MIN_SAMPLES_FOR_PREFERENCE,
            }
        )
    strategies.sort(
        key=lambda item: (item["accept_rate"], item["mean_score_delta"] or 0.0),
        reverse=True,
    )
    return {
        "schema": "geiter/experience-v1",
        "generated_at": now_iso(),
        "total_executions": sum(item["attempts"] for item in strategies),
        "strategy_count": len(strategies),
        "minimum_samples_for_preference": MIN_SAMPLES_FOR_PREFERENCE,
        "strategies": strategies,
    }


def recommend_kind(store: GeiterStore) -> dict[str, Any]:
    """Choose the change kind for the next cycle from historical effectiveness.

    Preference requires a minimum sample so a single lucky change does not lock
    the system into one strategy. When no kind is eligible yet, this falls back
    to the default and says so, keeping the loop deterministic on a cold start.
    """
    report = effectiveness(store)
    eligible = [item for item in report["strategies"] if item["preference_eligible"]]
    if not eligible:
        return {
            "kind": DEFAULT_CHANGE_KIND,
            "reason": "no_change_kind_has_enough_history_yet",
            "experience": report,
        }
    best = eligible[0]
    return {
        "kind": best["kind"],
        "reason": (
            f"highest historical accept rate ({best['accept_rate']}) across "
            f"{best['attempts']} attempts"
        ),
        "experience": report,
    }


SANDBOX_DIRNAME = "sandbox"
CHANGELOG_FILENAME = "knowledge.md"

# Authorization levels for a governed surface. The sandbox is implicitly
# trusted because it is Geiter's own scratch space; any real surface must
# declare its policy and defaults to requiring human approval.
POLICY_AUTO = "auto"
POLICY_APPROVE = "approve"
POLICY_DENY = "deny"
VALID_POLICIES = {POLICY_AUTO, POLICY_APPROVE, POLICY_DENY}

APPROVAL_PENDING = "pending_approval"
APPROVAL_APPROVED = "approved"
APPROVAL_REJECTED = "rejected"
APPROVAL_EXPIRED = "expired"


class PolicyError(Exception):
    """Raised when a change is refused by policy before it is applied."""


@dataclass
class SurfacePolicy:
    """The authorization boundary for a single knowledge surface.

    A policy answers two questions for a proposed change: is this surface
    allowed to be edited at all, and does this particular change need a human
    to approve it before it is applied. The default posture is conservative --
    an undeclared surface requires approval.
    """

    surface_id: str
    level: str = POLICY_APPROVE
    allowed_kinds: set[str] = field(default_factory=set)
    require_gate: bool = True
    max_approval_age_seconds: int = 86400

    def validate(self) -> None:
        if self.level not in VALID_POLICIES:
            raise ValueError(
                f"unsupported surface policy: {self.level} (expected one of {sorted(VALID_POLICIES)})"
            )

    def decide(self, change_kind: str) -> str:
        """Return ``auto``, ``approve``, or ``deny`` for a change of this kind."""
        self.validate()
        if self.level == POLICY_DENY:
            return POLICY_DENY
        if self.allowed_kinds and change_kind not in self.allowed_kinds:
            return POLICY_DENY
        if self.level == POLICY_AUTO:
            return POLICY_AUTO
        return POLICY_APPROVE

    def to_dict(self) -> dict[str, Any]:
        return {
            "surface_id": self.surface_id,
            "level": self.level,
            "allowed_kinds": sorted(self.allowed_kinds),
            "require_gate": self.require_gate,
            "max_approval_age_seconds": self.max_approval_age_seconds,
        }


SANDBOX_POLICY = SurfacePolicy(
    surface_id="sandbox",
    level=POLICY_AUTO,
    allowed_kinds={"append_section"},
)


def resolve_policy(surface_id: str, declared: dict[str, Any] | None = None) -> SurfacePolicy:
    """Resolve a surface policy, defaulting unknown surfaces to require approval.

    The sandbox is the only surface that is trusted by default because Geiter
    owns it and it holds no real content. Every other surface -- including any
    real knowledge base -- must be explicitly declared and starts at
    ``approve``, so a mistake can never silently mutate real content.
    """
    if surface_id == "sandbox" and not declared:
        return SANDBOX_POLICY
    policy = SurfacePolicy(
        surface_id=surface_id,
        level=(declared or {}).get("level", POLICY_APPROVE),
        allowed_kinds=set((declared or {}).get("allowed_kinds", []) or []),
        require_gate=(declared or {}).get("require_gate", True),
        max_approval_age_seconds=(declared or {}).get("max_approval_age_seconds", 86400),
    )
    policy.validate()
    return policy


@dataclass
class ChangeProposal:
    """A declarative, evidence-backed change to the knowledge surface.

    A proposal never mutates anything on its own. It carries the intent, the
    exact edit, and the evidence that motivated it, so a later gate verdict can
    be attributed to a specific, auditable change.
    """

    change_id: str
    kind: str
    rationale: str
    edits: list[dict[str, Any]] = field(default_factory=list)
    evidence: dict[str, Any] = field(default_factory=dict)
    created_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "change_id": self.change_id,
            "kind": self.kind,
            "rationale": self.rationale,
            "edits": self.edits,
            "evidence": self.evidence,
            "created_at": self.created_at,
        }


class SandboxSurface:
    """A local, reversible knowledge surface Geiter is allowed to edit.

    The surface is a single markdown file plus a snapshot directory. Every
    applied change first records a snapshot, which makes ``revert`` a pure
    filesystem restore rather than a guess.
    """

    def __init__(self, root: str | Path):
        self.root = Path(root).resolve()
        self.surface_dir = self.root / SANDBOX_DIRNAME
        self.knowledge_path = self.surface_dir / CHANGELOG_FILENAME
        self.snapshot_dir = self.surface_dir / ".snapshots"

    def ensure(self, initial_content: str | None = None) -> dict[str, Any]:
        """Create the surface if missing; never overwrite an existing surface."""
        self.surface_dir.mkdir(parents=True, exist_ok=True)
        self.snapshot_dir.mkdir(parents=True, exist_ok=True)
        created = False
        if not self.knowledge_path.exists():
            self.knowledge_path.write_text(
                initial_content or "# Knowledge surface\n\nSeed content for GEO experiments.\n",
                encoding="utf-8",
            )
            created = True
        return {
            "surface_dir": str(self.surface_dir),
            "knowledge_path": str(self.knowledge_path),
            "created": created,
        }

    def read(self) -> str:
        if not self.knowledge_path.exists():
            return ""
        return self.knowledge_path.read_text(encoding="utf-8")

    def snapshot(self, change_id: str) -> Path:
        """Persist the current surface so a change can be reverted later."""
        self.snapshot_dir.mkdir(parents=True, exist_ok=True)
        target = self.snapshot_dir / f"{change_id}.md"
        content = self.read()
        target.write_text(content, encoding="utf-8")
        return target

    def append_section(self, heading: str, body: str) -> str:
        """Append a titled section to the knowledge surface."""
        current = self.read()
        addition = f"\n## {heading}\n\n{body.strip()}\n"
        self.knowledge_path.write_text(current.rstrip() + "\n" + addition, encoding="utf-8")
        return self.knowledge_path.read_text(encoding="utf-8")

    def revert(self, change_id: str) -> dict[str, Any]:
        """Restore the surface from the snapshot taken when the change applied."""
        snapshot = self.snapshot_dir / f"{change_id}.md"
        if not snapshot.exists():
            return {"reverted": False, "reason": "no_snapshot", "change_id": change_id}
        self.knowledge_path.write_text(snapshot.read_text(encoding="utf-8"), encoding="utf-8")
        return {"reverted": True, "change_id": change_id, "restored_from": str(snapshot)}


class SurfaceProvider:
    """A deterministic provider that answers from the live sandbox surface.

    This is what makes autonomous execution honest. The provider reads the
    current surface content and only mentions and cites the target when the
    surface actually contains it, so an applied change genuinely moves the
    metrics and the gate genuinely judges that change. A provider that returned
    canned answers would make every self-verification meaningless.
    """

    name = "surface"

    def __init__(self, surface: SandboxSurface, target: str):
        self.surface = surface
        self.target = target

    def answer(self, prompt: str) -> Any:
        from .providers import ProviderAnswer

        content = self.surface.read()
        lowered = content.casefold()
        target_token = self.target.casefold()
        # A citation is only "target" attribution when the surface cites the
        # target explicitly; otherwise the provider cites the surface root.
        cites_target = f"{target_token}:" in lowered or f"{target_token} " in lowered
        terms = [token for token in target_token.replace("-", " ").split() if token]
        mentions = all(token in lowered for token in terms) if terms else False
        if mentions and cites_target:
            answer = (
                f"{self.target} is described on the knowledge surface. "
                f"Evidence: {prompt}"
            )
            citations = [f"https://sandbox.invalid/{target_token}#evidence"]
        elif mentions:
            answer = f"{self.target} appears on the knowledge surface but is not cited."
            citations = [f"https://sandbox.invalid/{target_token}"]
        else:
            answer = "The knowledge surface does not cover this topic yet."
            citations = []
        return ProviderAnswer(provider=self.name, answer=answer, citations=citations)


def surface_provider_factory(surface: SandboxSurface, target: str) -> Any:
    """Return a ``provider_factory`` suitable for :func:`execute_cycle`."""

    def factory(store: GeiterStore, active_surface: SandboxSurface) -> SurfaceProvider:
        return SurfaceProvider(active_surface, target or store.read()["identity"]["name"])

    return factory


def propose_change(
    store: GeiterStore,
    surface: SandboxSurface,
    heading: str,
    body: str,
    kind: str = "append_section",
) -> ChangeProposal:
    """Derive a concrete change from the workspace's own introspection.

    The proposal is grounded in ``introspect`` so the agent acts on evidence,
    not guesswork. It is still only a proposal; nothing is applied here.
    """
    if not heading.strip() or not body.strip():
        raise ValueError("change heading and body must be non-empty")
    introspection = store.introspect()
    change_id = f"chg_{int(time.time() * 1000):x}"
    edits = [{"op": kind, "heading": heading.strip(), "body": body.strip()}]
    proposal = ChangeProposal(
        change_id=change_id,
        kind=kind,
        rationale=(
            introspection.get("next_action")
            or "Improve the weakest dimension of the knowledge surface."
        ),
        edits=edits,
        evidence={
            "weakest_dimension": introspection.get("score", {}).get("weakest_dimension"),
            "north_star": introspection.get("score", {}).get("north_star"),
            "introspect_status": introspection.get("status"),
        },
        created_at=store.read()["updated_at"],
    )
    store.event("changes.proposed", proposal.to_dict())
    return proposal


def execute_cycle(
    store: GeiterStore,
    heading: str,
    body: str,
    provider_factory: Any,
    baseline_id: str | None = None,
    target: str | None = None,
    kind: str | None = None,
) -> dict[str, Any]:
    """Run one autonomous proposal -> apply -> verify -> accept/revert cycle.

    ``provider_factory`` is a callable that, given the store, returns a provider
    bound to the *current* surface content. This is what makes the loop honest:
    after the change is applied, the provider re-reads the mutated surface, so
    the gate judges the change's real effect rather than a canned response.

    When ``kind`` is omitted the strategy is chosen from historical
    effectiveness, which is how P3 compounding works: each cycle tends to use a
    change kind that has historically been accepted.
    """
    surface = SandboxSurface(store.root)
    surface_info = surface.ensure()
    before_score = store.score()
    selection = recommend_kind(store) if kind is None else {"kind": kind, "reason": "explicit"}

    proposal = propose_change(store, surface, heading, body, kind=selection["kind"])
    snapshot_path = surface.snapshot(proposal.change_id)
    applied_path = surface.append_section(heading, body)
    store.event(
        "changes.applied",
        {"change_id": proposal.change_id, "snapshot": str(snapshot_path)},
    )

    # Re-observe against the mutated surface and let the gate arbitrate.
    prompts = store.prompts()
    provider = provider_factory(store, surface)
    from .providers import run_provider  # local import to avoid a cycle

    run = run_provider(store, prompts, provider, target, max_attempts=1)
    gate = store.regression_gate(run["run"], baseline_id)
    after_score = store.score()

    accepted = bool(gate.get("ok"))
    verdict = {
        "change_id": proposal.change_id,
        "accepted": accepted,
        "gate_ok": gate.get("ok"),
        "gate_failed_checks": gate.get("failed_checks", []),
        "comparison_verdict": gate.get("comparison", {}).get("verdict"),
        "score_before": before_score.get("north_star"),
        "score_after": after_score.get("north_star"),
        "gate_action": gate.get("action"),
    }

    if accepted:
        store.event("changes.accepted", verdict)
    else:
        revert = surface.revert(proposal.change_id)
        verdict["revert"] = revert
        store.event("changes.reverted", verdict)

    # Record the outcome as a learning so the next iteration can learn from it.
    learning = store.add(
        "learnings",
        "execution.result",
        {
            "change_id": proposal.change_id,
            "kind": proposal.kind,
            "accepted": accepted,
            "rationale": proposal.rationale,
            "evidence": proposal.evidence,
            "verdict": verdict,
        },
    )

    return {
        "schema": "geiter/execution-v1",
        "surface": surface_info,
        "strategy": selection,
        "proposal": proposal.to_dict(),
        "applied_path": str(applied_path),
        "run": run["run"],
        "gate": gate,
        "verdict": verdict,
        "learning_id": learning["id"],
        "next_action": (
            "Change accepted; keep iterating on the same surface."
            if accepted
            else "Change reverted; the gate rejected it. Try a different change."
        ),
    }


def request_approval(
    store: GeiterStore,
    surface_id: str,
    heading: str,
    body: str,
    policy: SurfacePolicy,
    kind: str = "append_section",
) -> dict[str, Any]:
    """Record a change that policy requires a human to approve before applying.

    The request is persisted so it survives across processes and can be approved
    or rejected later by something other than the proposing agent. Nothing is
    mutated on the surface here.
    """
    request = store.add(
        "approvals",
        "change.approval_request",
        {
            "surface_id": surface_id,
            "kind": kind,
            "heading": heading.strip(),
            "body": body.strip(),
            "policy": policy.to_dict(),
            "status": APPROVAL_PENDING,
            "requested_at": now_iso(),
        },
    )
    store.event("approvals.requested", {"request_id": request["id"], "surface_id": surface_id})
    return request


def decide_approval(
    store: GeiterStore,
    request_id: str,
    decision: str,
    approver: str,
) -> dict[str, Any]:
    """Approve or reject a pending change request.

    Only a pending request can be decided, and the decision records who made it
    so the audit trail attributes the change to a specific approver. An approved
    request is still subject to the regression gate when it is finally applied.
    """
    if decision not in {APPROVAL_APPROVED, APPROVAL_REJECTED}:
        raise ValueError(f"unsupported approval decision: {decision}")
    if not approver.strip():
        raise ValueError("an approver is required to decide an approval request")
    state = store.read()
    request = next((item for item in state.get("approvals", []) if item["id"] == request_id), None)
    if request is None or request.get("kind") != "change.approval_request":
        raise ValueError(f"unknown approval request: {request_id}")
    if request["data"].get("status") != APPROVAL_PENDING:
        return request
    request["data"]["status"] = decision
    request["data"]["approver"] = approver.strip()
    request["data"]["decided_at"] = now_iso()
    store._write(state)
    store.event("approvals.decided", {"request_id": request_id, "decision": decision, "approver": approver})
    return request


def list_approvals(store: GeiterStore, status: str | None = APPROVAL_PENDING) -> list[dict[str, Any]]:
    requests = [
        item for item in store.read().get("approvals", [])
        if item.get("kind") == "change.approval_request"
    ]
    if status is not None:
        requests = [item for item in requests if item["data"].get("status") == status]
    return requests


def experience(store: GeiterStore) -> dict[str, Any]:
    """Return the durable playbook: historical effectiveness plus the next strategy.

    This is the compounding surface. It answers two questions an agent should
    not have to guess at: which strategies have worked before, and what should
    the next change try.
    """
    report = effectiveness(store)
    selection = recommend_kind(store)
    return {
        "schema": "geiter/experience-v1",
        "generated_at": now_iso(),
        "total_executions": report["total_executions"],
        "strategy_count": report["strategy_count"],
        "minimum_samples_for_preference": report["minimum_samples_for_preference"],
        "strategies": report["strategies"],
        "recommendation": {"kind": selection["kind"], "reason": selection["reason"]},
        "compounding": (
            "each verified change updates the playbook, and the next cycle prefers "
            "a strategy that historically improved the score"
        ),
    }


def execute_governed(
    store: GeiterStore,
    surface_id: str,
    heading: str,
    body: str,
    provider_factory: Any,
    declared_policy: dict[str, Any] | None = None,
    baseline_id: str | None = None,
    target: str | None = None,
    request_id: str | None = None,
) -> dict[str, Any]:
    """Execute a change only if policy allows it, otherwise request approval.

    This is the entry point for any surface that is not the trusted sandbox. It
    resolves the surface policy first:

    - ``deny``: refuse the change outright.
    - ``auto``: run the gated cycle immediately.
    - ``approve``: require an approved request; if none exists, create one and
      return a ``pending_approval`` outcome without touching the surface.

    Even an approved change still has to survive the regression gate, so
    approval only authorizes the *attempt*, never the outcome.
    """
    policy = resolve_policy(surface_id, declared_policy)
    decision = policy.decide("append_section")
    surface = SandboxSurface(store.root) if surface_id == "sandbox" else None

    if decision == POLICY_DENY:
        return {
            "schema": "geiter/execution-v1",
            "status": "denied",
            "surface_id": surface_id,
            "policy": policy.to_dict(),
            "next_action": "Policy denies changes to this surface; no mutation was attempted.",
        }

    if decision == POLICY_APPROVE:
        approved = None
        if request_id:
            request = next(
                (item for item in store.read().get("approvals", []) if item["id"] == request_id),
                None,
            )
            if request is None or request.get("kind") != "change.approval_request":
                raise ValueError(f"unknown approval request: {request_id}")
            if request["data"].get("status") != APPROVAL_APPROVED:
                approved = None
            else:
                approved = request
        if approved is None:
            request = request_approval(store, surface_id, heading, body, policy)
            return {
                "schema": "geiter/execution-v1",
                "status": APPROVAL_PENDING,
                "surface_id": surface_id,
                "policy": policy.to_dict(),
                "approval_request_id": request["id"],
                "next_action": "A human must approve this change before it is applied.",
            }
        heading = approved["data"]["heading"]
        body = approved["data"]["body"]

    # decision == auto, or an approved request: run the gated cycle.
    result = execute_cycle(
        store,
        heading,
        body,
        provider_factory=provider_factory,
        baseline_id=baseline_id,
        target=target,
    )
    result["status"] = "executed"
    result["surface_id"] = surface_id
    result["policy"] = policy.to_dict()
    if request_id and decision == POLICY_APPROVE:
        result["approval_request_id"] = request_id
    return result
