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
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .core import GeiterStore


SANDBOX_DIRNAME = "sandbox"
CHANGELOG_FILENAME = "knowledge.md"


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
) -> dict[str, Any]:
    """Run one autonomous proposal -> apply -> verify -> accept/revert cycle.

    ``provider_factory`` is a callable that, given the store, returns a provider
    bound to the *current* surface content. This is what makes the loop honest:
    after the change is applied, the provider re-reads the mutated surface, so
    the gate judges the change's real effect rather than a canned response.
    """
    surface = SandboxSurface(store.root)
    surface_info = surface.ensure()
    before_score = store.score()

    proposal = propose_change(store, surface, heading, body)
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
            "accepted": accepted,
            "rationale": proposal.rationale,
            "evidence": proposal.evidence,
            "verdict": verdict,
        },
    )

    return {
        "schema": "geiter/execution-v1",
        "surface": surface_info,
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
