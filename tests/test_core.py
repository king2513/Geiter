import json
import tempfile
import unittest
from pathlib import Path

import geiter
from geiter.core import FileLock, GeiterStore
from geiter.gateway import dispatch
from geiter.providers import (
    FixtureProvider,
    HttpJsonProvider,
    JsonlProvider,
    ProviderAnswer,
    load_provider,
    observe_prompts,
    regression_run,
    resume_provider,
    run_provider,
)
from geiter.execute import (
    SandboxSurface,
    SurfacePolicy,
    decide_approval,
    effectiveness,
    execute_cycle,
    execute_governed,
    experience,
    list_approvals,
    recommend_kind,
    resolve_policy,
    surface_provider_factory,
)


class GeiterStoreTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.store = GeiterStore(self.tempdir.name)

    def tearDown(self):
        self.tempdir.cleanup()

    def test_init_is_idempotent_and_writes_state(self):
        first = self.store.init()
        second = self.store.init()
        self.assertEqual(first["schema"], "geiter/v1")
        self.assertEqual(second["created_at"], first["created_at"])
        self.assertTrue(self.store.state_path.exists())

    def test_state_lock_serializes_concurrent_writes(self):
        """Concurrent threads must not lose an update to the state file."""
        import threading

        self.store.init()
        errors: list[Exception] = []

        def worker(index: int) -> None:
            try:
                for item in range(20):
                    self.store.add("memories", "fact", {"text": f"memory-{index}-{item}"})
            except Exception as exc:  # pragma: no cover - surfaced via assert
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(errors, [])
        self.assertEqual(len(self.store.read()["memories"]), 8 * 20)

    def test_mutating_methods_hold_the_state_lock(self):
        """Every state-mutating method must run while the cross-process lock is held."""
        self.store.init()
        observed: list[int] = []
        original_enter = FileLock.__enter__

        def spy_enter(self_lock: FileLock) -> FileLock:
            observed.append(self_lock._depth)
            return original_enter(self_lock)

        FileLock.__enter__ = spy_enter  # type: ignore[method-assign]
        try:
            self.store.add("memories", "fact", {"text": "a"})
            self.store.add_prompt("What is Geiter?")
            prompt = self.store.read()["prompts"][0]
            self.store.record_observation(prompt["id"], "p", "Geiter", ["https://geiter.dev/x"])
            self.store.iterate("hold the lock")
        finally:
            FileLock.__enter__ = original_enter  # type: ignore[method-assign]
        # Each mutating call entered the lock; nested calls re-enter at greater depth.
        self.assertGreaterEqual(len(observed), 8)
        self.assertTrue(any(depth > 1 for depth in observed))

    def test_state_lock_is_reentrant_for_nested_store_calls(self):
        """Nested mutating calls (iterate -> inspect -> add) must not deadlock."""
        self.store.init()
        result = self.store.iterate("nested reentrancy check")
        self.assertIsNotNone(result["id"])
        # Re-entering an already-held lock is a no-op that must not raise.
        with self.store.lock:
            with self.store.lock:
                self.assertGreater(self.store.lock._depth, 0)

    def test_state_lock_serializes_concurrent_processes(self):
        """Multiple OS processes writing the same workspace must not lose updates."""
        import subprocess
        import sys
        import textwrap

        self.store.init()
        script = textwrap.dedent(
            """
            import sys
            from geiter.core import GeiterStore
            store = GeiterStore(sys.argv[1])
            for index in range(10):
                store.add("memories", "fact", {"text": f"proc-{sys.argv[2]}-{index}"})
            """
        )
        project_root = str(Path(__file__).parent.parent)
        procs = [
            subprocess.Popen(
                [sys.executable, "-c", script, self.tempdir.name, str(worker)],
                cwd=project_root,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            for worker in range(4)
        ]
        for proc in procs:
            _, err = proc.communicate(timeout=60)
            self.assertEqual(proc.returncode, 0, err.decode("utf-8", "replace"))
        # 4 processes x 10 adds each, with no lost updates.
        self.assertEqual(len(self.store.read()["memories"]), 40)

    def test_iteration_leaves_a_complete_trace(self):
        self.store.init()
        self.store.add("goals", "goal", {"text": "Become legible to agents"})
        result = self.store.iterate("Test the smallest observable loop")
        state = self.store.read()
        self.assertEqual(result["hypothesis"]["data"]["text"], "Test the smallest observable loop")
        self.assertEqual(len(state["iterations"]), 1)
        self.assertEqual(len(state["learnings"]), 1)
        self.assertGreaterEqual(len(self.store.events()), 5)

    def test_iteration_consumes_comparison_context(self):
        self.store.init()
        result = self.store.iterate()
        self.assertEqual(result["comparison"]["status"], "no_baseline")
        self.assertIn("Save a baseline", result["learning"]["data"]["next_prompt"])

    def test_iteration_uses_latest_failed_gate_after_gate_action_is_resolved(self):
        self.store.init()
        self.store.add_prompt("What is Geiter?")
        result = regression_run(
            self.store,
            self.store.prompts(),
            load_provider("fixture"),
            baseline_id="baseline_missing",
        )
        self.store.complete_action(
            result["gate"]["action_record"]["id"],
            {"verified": "selected a valid baseline"},
        )

        iteration = self.store.iterate()
        data = iteration["action"]["data"]
        self.assertEqual(iteration["latest_gate"]["run_id"], result["run"]["id"])
        self.assertFalse(iteration["latest_gate"]["ok"])
        self.assertEqual(iteration["hypothesis"]["data"]["text"].split(":")[0],
                         f"Resolve failed regression gate for run {result['run']['id']}")
        self.assertEqual(data["gate_run_id"], result["run"]["id"])
        self.assertFalse(data["gate_ok"])
        self.assertIn("baseline", iteration["learning"]["data"]["next_prompt"])
        self.assertEqual(
            iteration["active_action"]["data"]["status"],
            "in_progress",
        )
        self.assertEqual(
            iteration["active_action"]["data"]["evidence"]["run_id"],
            result["run"]["id"],
        )

    def test_iteration_claims_highest_priority_action_and_leaves_resolution_open(self):
        self.store.init()
        normal = self.store.propose_action("review", "Review evidence")
        critical = self.store.propose_action("repair", "Repair workspace", priority="critical")
        result = self.store.iterate()
        self.assertEqual(result["active_action"]["id"], critical["id"])
        self.assertEqual(result["active_action"]["data"]["status"], "in_progress")
        self.assertEqual(result["hypothesis"]["data"]["text"], "Advance queued action "
                         f"{critical['id']}: Repair workspace")
        self.assertEqual(result["action"]["data"]["active_action_id"], critical["id"])
        self.assertEqual([item["id"] for item in self.store.list_actions()], [normal["id"]])
        resolved = self.store.skip_action(critical["id"], {"reason": "superseded"})
        self.assertEqual(resolved["data"]["status"], "skipped")

    def test_stale_action_recovery_is_detectable_and_idempotent(self):
        self.store.init()
        action = self.store.propose_action("repair", "Repair interrupted work", priority="high")
        claimed = self.store.claim_action(action["id"], "itr_interrupted", lease_seconds=0)
        self.assertEqual(claimed["data"]["status"], "in_progress")
        with self.assertRaisesRegex(ValueError, "already claimed by itr_interrupted"):
            self.store.claim_action(action["id"], "itr_competing")
        self.assertEqual(self.store.list_actions("stale")[0]["id"], action["id"])

        reclaimed = self.store.reclaim_action(
            action["id"],
            {"reason": "agent_interrupted"},
            at="2099-01-01T00:00:00+00:00",
        )
        repeated = self.store.reclaim_action(action["id"], {"reason": "repeat"})
        self.assertEqual(reclaimed["data"]["status"], "open")
        self.assertEqual(reclaimed["data"]["reclaim_count"], 1)
        self.assertEqual(reclaimed["data"]["last_claimed_by"], "itr_interrupted")
        self.assertEqual(repeated["data"]["reclaim_count"], 1)
        self.assertEqual(self.store.stale_actions(), [])

    def test_active_action_lease_rejects_reclaim(self):
        self.store.init()
        action = self.store.propose_action("repair", "Keep active work")
        claimed = self.store.claim_action(action["id"], "itr_active", lease_seconds=3600)
        same_owner = self.store.claim_action(action["id"], "itr_active", lease_seconds=3600)
        self.assertEqual(same_owner["data"]["claimed_by"], "itr_active")
        with self.assertRaisesRegex(ValueError, "already claimed by itr_active"):
            self.store.claim_action(action["id"], "itr_other", lease_seconds=3600)
        with self.assertRaisesRegex(ValueError, "lease is still active"):
            self.store.reclaim_action(action["id"], at=claimed["data"]["claimed_at"])

    def test_iteration_reports_claim_conflict_without_claiming_another_agent_action(self):
        self.store.init()
        action = self.store.propose_action("repair", "Keep another agent's work", priority="critical")
        self.store.claim_action(action["id"], "itr_other", lease_seconds=3600)
        result = self.store.iterate()
        self.assertIsNone(result["active_action"])
        self.assertEqual(result["claim_conflict"]["action_id"], action["id"])
        self.assertEqual(result["action"]["data"]["decision"], "claim_conflict")
        self.assertIn("already claimed", result["claim_conflict"]["reason"])
        stored = self.store.list_actions("in_progress")[0]
        self.assertEqual(stored["data"]["claimed_by"], "itr_other")

    def test_iteration_preserves_failed_gate_action_conflict(self):
        self.store.init()
        self.store.add_prompt("What is Geiter?")
        result = regression_run(
            self.store,
            self.store.prompts(),
            load_provider("fixture"),
            baseline_id="missing-baseline",
        )
        gate_action = result["gate"]["action_record"]
        self.store.claim_action(gate_action["id"], "itr_other", lease_seconds=3600)
        iteration = self.store.iterate()
        self.assertIsNone(iteration["active_action"])
        self.assertEqual(iteration["claim_conflict"]["action_id"], gate_action["id"])
        self.assertEqual(iteration["action"]["data"]["decision"], "claim_conflict")
        self.assertEqual(
            self.store.list_actions("in_progress")[0]["data"]["claimed_by"],
            "itr_other",
        )

    def test_recovery_does_not_touch_completed_or_skipped_actions(self):
        self.store.init()
        completed = self.store.propose_action("repair", "Completed repair")
        self.store.claim_action(completed["id"], "itr_completed", lease_seconds=0)
        self.store.complete_action(completed["id"], {"verified": True})
        skipped = self.store.propose_action("repair", "Skipped repair")
        self.store.claim_action(skipped["id"], "itr_skipped", lease_seconds=0)
        self.store.skip_action(skipped["id"], {"reason": "superseded"})

        self.assertEqual(
            self.store.recover_stale_actions(at="2099-01-01T00:00:00+00:00"),
            [],
        )
        self.assertEqual(self.store.read()["actions"][-2]["data"]["status"], "completed")
        self.assertEqual(self.store.read()["actions"][-1]["data"]["status"], "skipped")

    def test_iteration_recovers_stale_action_before_claiming(self):
        self.store.init()
        action = self.store.propose_action("repair", "Resume interrupted repair", priority="critical")
        self.store.claim_action(action["id"], "itr_interrupted", lease_seconds=0)
        result = self.store.iterate()
        self.assertEqual([item["id"] for item in result["recovered_actions"]], [action["id"]])
        self.assertEqual(result["active_action"]["id"], action["id"])
        self.assertEqual(result["action"]["data"]["recovered_action_ids"], [action["id"]])
        self.assertEqual(result["active_action"]["data"]["status"], "in_progress")

    def test_inspect_ignores_geiter_state(self):
        self.store.init()
        Path(self.tempdir.name, "notes.md").write_text("hello", encoding="utf-8")
        observation = self.store.inspect()
        self.assertEqual(observation["data"]["file_count"], 1)
        self.assertEqual(observation["data"]["extensions"], {".md": 1})

    def test_state_is_valid_json(self):
        self.store.init()
        json.loads(self.store.state_path.read_text(encoding="utf-8"))

    def test_baseline_comparison_and_policy_gate(self):
        self.store.init()
        prompt = self.store.add_prompt("What is Geiter?")
        self.store.record_observation(prompt["id"], "fixture", "Geiter is an agent-native GEO runtime.", ["https://geiter.dev/docs"])
        baseline = self.store.save_baseline("before")
        self.store.record_observation(prompt["id"], "fixture", "Geiter is an agent-native GEO runtime.", ["https://geiter.dev/docs"])
        comparison = self.store.compare(baseline["id"])
        self.assertEqual(comparison["status"], "compared")
        self.assertEqual(comparison["current"]["metrics"]["sample_size"], 1)
        self.assertEqual(comparison["verdict"], "insufficient_data")
        proposal = self.store.propose_experiment("Improve citation rate", "Add an authoritative docs page")
        self.assertEqual(proposal["data"]["status"], "proposed")
        approved = self.store.approve_experiment(proposal["id"])
        self.assertEqual(approved["data"]["status"], "approved")
        result = self.store.record_experiment_result(
            proposal["id"], "supported", "Target citation rate remained stable after the change.", baseline["id"]
        )
        self.assertEqual(result["kind"], "experiment.result")
        completed = next(item for item in self.store.read()["experiments"] if item["id"] == proposal["id"])
        self.assertEqual(completed["data"]["status"], "completed")

    def test_compare_without_baseline_is_explicit(self):
        self.store.init()
        comparison = self.store.compare()
        self.assertEqual(comparison["status"], "no_baseline")

    def test_compare_rejects_unknown_baseline_without_falling_back(self):
        self.store.init()
        prompt = self.store.add_prompt("What is Geiter?")
        self.store.record_observation(
            prompt["id"],
            "fixture",
            "Geiter is an agent-native GEO runtime.",
            ["https://geiter.dev/docs"],
        )
        self.store.save_baseline("known")
        comparison = self.store.compare("baseline_missing")
        self.assertEqual(comparison["status"], "unknown_baseline")
        self.assertEqual(comparison["baseline_id"], "baseline_missing")
        self.assertNotIn("baseline", comparison)

    def test_matrix_groups_provider_and_intent_and_finds_weakest(self):
        self.store.init()
        prompt_a = self.store.add_prompt("What is Geiter?", "discovery")
        prompt_b = self.store.add_prompt("Why use Geiter?", "evaluation")
        self.store.record_observation(prompt_a["id"], "provider-a", "Geiter is useful.", ["https://geiter.dev/docs"])
        self.store.record_observation(prompt_b["id"], "provider-b", "A generic answer.", [])
        matrix = self.store.analyze_matrix()
        self.assertEqual(matrix["cell_count"], 2)
        self.assertEqual(matrix["weakest_cells"][0]["provider"], "provider-b")
        self.assertEqual(matrix["weakest_cells"][0]["intent"], "evaluation")

    def test_read_backfills_collections_for_older_workspaces(self):
        """A workspace written before a collection existed must stay consistent."""
        self.store.init()
        # Simulate a legacy state file that predates the approvals collection.
        state = self.store.read()
        state.pop("approvals", None)
        self.store._write(state)
        reloaded = self.store.read()
        self.assertIn("approvals", reloaded)
        self.assertEqual(reloaded["approvals"], [])
        self.assertTrue(self.store.doctor()["ok"])

    def test_unknown_surface_policy_defaults_to_approve(self):
        policy = resolve_policy("some-real-site")
        self.assertEqual(policy.level, "approve")
        self.assertEqual(policy.decide("append_section"), "approve")
        # The sandbox is the only surface trusted to auto-apply.
        self.assertEqual(resolve_policy("sandbox").level, "auto")
        self.assertEqual(resolve_policy("sandbox").decide("append_section"), "auto")

    def test_policy_levels_decide_changes(self):
        self.assertEqual(
            resolve_policy("x", {"level": "deny"}).decide("append_section"), "deny"
        )
        self.assertEqual(
            resolve_policy("x", {"level": "auto"}).decide("append_section"), "auto"
        )
        # allowed_kinds restricts what an auto surface will touch.
        policy = resolve_policy("x", {"level": "auto", "allowed_kinds": ["rewrite"]})
        self.assertEqual(policy.decide("append_section"), "deny")
        self.assertEqual(policy.decide("rewrite"), "auto")
        with self.assertRaises(ValueError):
            resolve_policy("x", {"level": "nonsense"})

    def test_execute_governed_requires_approval_for_unknown_surface(self):
        store = GeiterStore(self.tempdir.name)
        store.init()
        target = store.read()["identity"]["name"]
        surface = SandboxSurface(self.tempdir.name)
        surface.ensure()
        before = surface.read()
        result = execute_governed(
            store,
            "real-site",
            "Real heading",
            "Real body",
            provider_factory=surface_provider_factory(surface, target),
            target=target,
        )
        self.assertEqual(result["status"], "pending_approval")
        # Nothing may be applied before approval.
        self.assertEqual(surface.read(), before)
        pending = list_approvals(store, "pending_approval")
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["data"]["heading"], "Real heading")

    def test_execute_governed_denies_when_policy_denies(self):
        store = GeiterStore(self.tempdir.name)
        store.init()
        target = store.read()["identity"]["name"]
        surface = SandboxSurface(self.tempdir.name)
        surface.ensure()
        result = execute_governed(
            store,
            "locked-site",
            "H",
            "B",
            provider_factory=surface_provider_factory(surface, target),
            declared_policy={"level": "deny"},
            target=target,
        )
        self.assertEqual(result["status"], "denied")
        self.assertNotIn("verdict", result)

    def test_approved_request_executes_and_rejected_one_does_not(self):
        store = GeiterStore(self.tempdir.name)
        store.init()
        target = store.read()["identity"]["name"]
        store.add_prompt("What is Geiter?")
        store.add_prompt("Why use Geiter?")
        surface = SandboxSurface(self.tempdir.name)
        surface.ensure()
        factory = surface_provider_factory(surface, target)
        # Propose -> pending.
        proposal = execute_governed(
            store, "site", "Geiter: approved", "Geiter is cited here.",
            provider_factory=factory, target=target,
        )
        request_id = proposal["approval_request_id"]
        # A rejected request must not execute.
        execute_governed(
            store, "site", "ignored", "ignored",
            provider_factory=factory, request_id=request_id,
        )
        # Approve and execute using the stored request content.
        decided = decide_approval(store, request_id, "approved", "human:alice")
        self.assertEqual(decided["data"]["status"], "approved")
        self.assertEqual(decided["data"]["approver"], "human:alice")
        result = execute_governed(
            store, "site", "ignored", "ignored",
            provider_factory=factory, request_id=request_id, target=target,
        )
        self.assertEqual(result["status"], "executed")
        self.assertEqual(result["proposal"]["edits"][0]["heading"], "Geiter: approved")

    def test_decide_approval_validates_and_is_idempotent(self):
        store = GeiterStore(self.tempdir.name)
        store.init()
        request = store.add(
            "approvals", "change.approval_request",
            {"status": "pending_approval", "surface_id": "s", "heading": "h", "body": "b"},
        )
        with self.assertRaises(ValueError):
            decide_approval(store, request["id"], "approved", "")
        with self.assertRaises(ValueError):
            decide_approval(store, request["id"], "maybe", "alice")
        with self.assertRaises(ValueError):
            decide_approval(store, "nope", "approved", "alice")
        first = decide_approval(store, request["id"], "approved", "alice")
        second = decide_approval(store, request["id"], "approved", "bob")
        self.assertEqual(first["data"]["approver"], "alice")
        # Idempotent: the first decision stands.
        self.assertEqual(second["data"]["approver"], "alice")

    def test_effectiveness_is_empty_on_cold_start(self):
        store = GeiterStore(self.tempdir.name)
        store.init()
        report = effectiveness(store)
        self.assertEqual(report["schema"], "geiter/experience-v1")
        self.assertEqual(report["total_executions"], 0)
        self.assertEqual(report["strategies"], [])

    def test_recommend_kind_falls_back_before_enough_history(self):
        store = GeiterStore(self.tempdir.name)
        store.init()
        rec = recommend_kind(store)
        self.assertEqual(rec["kind"], "append_section")
        self.assertEqual(rec["reason"], "no_change_kind_has_enough_history_yet")
        # A single attempt is not enough to earn preference.
        store.add("learnings", "execution.result", {
            "change_id": "c1", "kind": "cite_target", "accepted": True,
            "verdict": {"accepted": True, "score_before": 10.0, "score_after": 20.0},
        })
        rec = recommend_kind(store)
        self.assertEqual(rec["kind"], "append_section")

    def test_effectiveness_prefers_the_strategy_that_improved(self):
        store = GeiterStore(self.tempdir.name)
        store.init()

        def seed(kind: str, accepted: bool, before: float, after: float) -> None:
            store.add("learnings", "execution.result", {
                "change_id": f"c_{kind}_{before}_{after}", "kind": kind,
                "accepted": accepted, "evidence": {"weakest_dimension": "target_citation"},
                "verdict": {"accepted": accepted, "score_before": before, "score_after": after},
            })

        # bad strategy: half reverted, and the accepted one actually regressed.
        seed("bad_kind", True, 40.0, 38.0)
        seed("bad_kind", False, 40.0, 40.0)
        # good strategy: consistently improved.
        seed("good_kind", True, 40.0, 55.0)
        seed("good_kind", True, 55.0, 70.0)

        report = effectiveness(store)
        self.assertEqual(report["total_executions"], 4)
        by_kind = {item["kind"]: item for item in report["strategies"]}
        self.assertEqual(by_kind["good_kind"]["accept_rate"], 1.0)
        self.assertEqual(by_kind["good_kind"]["mean_score_delta"], 15.0)
        self.assertEqual(by_kind["good_kind"]["improved_count"], 2)
        self.assertEqual(by_kind["good_kind"]["regressed_count"], 0)
        self.assertEqual(by_kind["bad_kind"]["regressed_count"], 1)
        # It prefers the strategy that actually improved the score.
        self.assertEqual(recommend_kind(store)["kind"], "good_kind")
        # And the strategies are ranked best-first.
        self.assertEqual(report["strategies"][0]["kind"], "good_kind")

    def test_experience_playbook_surface(self):
        store = GeiterStore(self.tempdir.name)
        store.init()
        store.add("learnings", "execution.result", {
            "change_id": "c1", "kind": "cite_target", "accepted": True,
            "verdict": {"accepted": True, "score_before": 10.0, "score_after": 30.0},
        })
        store.add("learnings", "execution.result", {
            "change_id": "c2", "kind": "cite_target", "accepted": True,
            "verdict": {"accepted": True, "score_before": 30.0, "score_after": 50.0},
        })
        report = experience(store)
        self.assertEqual(report["schema"], "geiter/experience-v1")
        self.assertEqual(report["total_executions"], 2)
        self.assertEqual(report["recommendation"]["kind"], "cite_target")
        # The bounded form is exposed through report and context without mutation.
        bounded = store.report()["experience"]
        self.assertEqual(bounded["total_executions"], 2)
        self.assertEqual(bounded["top_strategy"]["kind"], "cite_target")
        self.assertEqual(store.context()["self_assessment"]["experience"]["total_executions"], 2)

    def test_execute_cycle_uses_learned_strategy_by_default(self):
        store = GeiterStore(self.tempdir.name)
        store.init()
        target = store.read()["identity"]["name"]
        store.add_prompt("What is Geiter?")
        store.add_prompt("Why use Geiter?")
        surface = SandboxSurface(self.tempdir.name)
        surface.ensure()
        # Seed a winning strategy so recommendation is eligible.
        for index, (before, after) in enumerate([(10.0, 20.0), (20.0, 30.0)]):
            store.add("learnings", "execution.result", {
                "change_id": f"seed-{index}", "kind": "cite_target", "accepted": True,
                "verdict": {"accepted": True, "score_before": before, "score_after": after},
            })
        result = execute_cycle(
            store,
            "Learned",
            f"{target} is documented and cited.",
            provider_factory=surface_provider_factory(surface, target),
            target=target,
        )
        # It should have chosen the historically better kind, not the default.
        self.assertEqual(result["strategy"]["kind"], "cite_target")
        self.assertEqual(result["proposal"]["kind"], "cite_target")
        # An explicit kind overrides the recommendation.
        explicit = execute_cycle(
            store,
            "Explicit",
            f"{target} is documented and cited.",
            provider_factory=surface_provider_factory(surface, target),
            target=target,
            kind="append_section",
        )
        self.assertEqual(explicit["proposal"]["kind"], "append_section")

    def test_sandbox_surface_apply_and_revert(self):
        surface = SandboxSurface(self.tempdir.name)
        info = surface.ensure()
        self.assertTrue(info["created"])
        before = surface.read()
        snapshot = surface.snapshot("chg_test")
        surface.append_section("Geiter: overview", "Geiter is a runtime.")
        self.assertIn("Geiter: overview", surface.read())
        revert = surface.revert("chg_test")
        self.assertTrue(revert["reverted"])
        self.assertEqual(surface.read(), before)
        self.assertTrue(snapshot.exists())

    def test_execute_cycle_accepts_a_change_the_gate_approves(self):
        store = GeiterStore(self.tempdir.name)
        store.init()
        target = store.read()["identity"]["name"]
        store.add_prompt("What is Geiter?", "discovery")
        store.add_prompt("Why use Geiter?", "evaluation")
        surface = SandboxSurface(self.tempdir.name)
        surface.ensure()
        result = execute_cycle(
            store,
            heading=f"{target}: overview",
            body=f"{target} is an agent-native GEO runtime that is cited.",
            provider_factory=surface_provider_factory(surface, target),
            target=target,
        )
        self.assertEqual(result["schema"], "geiter/execution-v1")
        self.assertTrue(result["verdict"]["accepted"], result["verdict"])
        self.assertTrue(result["gate"]["ok"])
        self.assertIn("Geiter: overview", surface.read())

    def test_execute_cycle_reverts_a_change_the_gate_rejects(self):
        store = GeiterStore(self.tempdir.name)
        store.init()
        target = store.read()["identity"]["name"]
        surface = SandboxSurface(self.tempdir.name)
        surface.ensure()
        before = surface.read()
        result = execute_cycle(
            store,
            heading=f"{target}: rejected",
            body="This change must be reverted because the gate fails with no prompts.",
            provider_factory=surface_provider_factory(surface, target),
            target=target,
        )
        self.assertFalse(result["verdict"]["accepted"])
        self.assertFalse(result["gate"]["ok"])
        self.assertIn("prompts_present", result["verdict"]["gate_failed_checks"])
        self.assertTrue(result["verdict"]["revert"]["reverted"])
        self.assertEqual(surface.read(), before)

    def test_execute_cycle_records_an_execution_learning(self):
        store = GeiterStore(self.tempdir.name)
        store.init()
        target = store.read()["identity"]["name"]
        store.add_prompt("What is Geiter?")
        surface = SandboxSurface(self.tempdir.name)
        surface.ensure()
        result = execute_cycle(
            store,
            heading=f"{target}: note",
            body=f"{target} is referenced and cited for this note.",
            provider_factory=surface_provider_factory(surface, target),
            target=target,
        )
        learnings = [
            item for item in store.read()["learnings"]
            if item["kind"] == "execution.result"
        ]
        self.assertEqual(len(learnings), 1)
        self.assertEqual(learnings[0]["data"]["change_id"], result["verdict"]["change_id"])

    def test_prompt_identity_is_deterministic_and_deduplicated(self):
        self.store.init()
        first = self.store.add_prompt("  What is   Geiter? ")
        second = self.store.add_prompt("what is geiter?")
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(len(self.store.read()["prompts"]), 1)

    def test_score_reports_insufficient_data_on_empty_workspace(self):
        self.store.init()
        score = self.store.score()
        self.assertEqual(score["schema"], "geiter/score-v1")
        self.assertEqual(score["status"], "insufficient_data")
        self.assertIsNone(score["north_star"])
        self.assertIsNone(score["weakest_dimension"])

    def test_score_is_direction_safe_and_finds_weakest_dimension(self):
        self.store.init()
        prompt_a = self.store.add_prompt("What is Geiter?", "discovery")
        prompt_b = self.store.add_prompt("Why use Geiter?", "evaluation")
        # Both mention the target and attribute a citation to it.
        self.store.record_observation(prompt_a["id"], "provider-a", "Geiter is great.", ["https://geiter.dev/docs"])
        self.store.record_observation(prompt_b["id"], "provider-a", "Geiter helps.", ["https://geiter.dev/why"])
        strong = self.store.score()
        self.assertEqual(strong["status"], "scored")
        self.assertGreater(strong["north_star"], 0)
        # A mentioned-but-never-cited observation should drag the score down.
        self.store.record_observation(prompt_b["id"], "provider-b", "Geiter again.", [])
        weak = self.store.score()
        self.assertLess(weak["north_star"], strong["north_star"])
        self.assertEqual(weak["weakest_dimension"], "target_citation")

    def test_introspect_ranks_evidence_backed_opportunities(self):
        self.store.init()
        prompt_a = self.store.add_prompt("What is Geiter?", "discovery")
        self.store.record_observation(prompt_a["id"], "provider-a", "Geiter is great.", ["https://geiter.dev/docs"])
        unobserved = self.store.add_prompt("Never observed prompt?", "discovery")
        result = self.store.introspect()
        self.assertEqual(result["schema"], "geiter/introspect-v1")
        self.assertEqual(result["status"], "actionable")
        opportunities = {item["opportunity"]: item for item in result["opportunities"]}
        self.assertIn("observe_uncovered_prompts", opportunities)
        self.assertIn(
            unobserved["id"],
            opportunities["observe_uncovered_prompts"]["evidence"]["unobserved_prompt_ids"],
        )
        self.assertEqual(result["opportunities"][0]["priority"], "high")

    def test_observation_metrics_and_analysis(self):
        self.store.init()
        prompt = self.store.add_prompt("What is Geiter?", "discovery")
        observation = self.store.record_observation(
            prompt["id"],
            "fixture-provider",
            "Geiter is an agent-native GEO runtime.",
            ["https://example.com/geiter"],
        )
        self.assertEqual(observation["data"]["metrics"]["mention"], 1)
        self.assertEqual(observation["data"]["metrics"]["citation"], 1)
        self.assertEqual(observation["data"]["metrics"]["target_citation"], 0)
        analysis = self.store.analyze()
        self.assertEqual(analysis["metrics"]["sample_size"], 1)
        self.assertEqual(analysis["metrics"]["mention_rate"], 1.0)
        self.assertEqual(analysis["metrics"]["citation_rate"], 1.0)
        self.assertEqual(analysis["metrics"]["target_citation_rate"], 0.0)
        self.assertEqual(analysis["metrics"]["mean_citation_reciprocal_rank"], 0.0)

    def test_health_flags_duplicate_empty_and_invalid_observations(self):
        self.store.init()
        prompt = self.store.add_prompt("What is Geiter?")
        first = self.store.record_observation(prompt["id"], "provider-a", "Geiter", ["https://geiter.dev/docs"])
        duplicate = self.store.record_observation(prompt["id"], "provider-a", "Geiter", ["https://geiter.dev/docs"])
        empty = self.store.record_observation(prompt["id"], "provider-b", "", ["not-a-url"])
        self.assertTrue(first["data"]["quality"]["usable"])
        self.assertFalse(duplicate["data"]["quality"]["usable"])
        self.assertFalse(empty["data"]["quality"]["usable"])
        health = self.store.health()
        self.assertEqual(health["observation_count"], 3)
        self.assertEqual(health["unusable_count"], 2)
        self.assertEqual(self.store.analyze()["metrics"]["sample_size"], 1)

    def test_comparison_requires_paired_sample_and_uses_directional_rank(self):
        self.store.init()
        prompt_a = self.store.add_prompt("What is Geiter?", "discovery")
        prompt_b = self.store.add_prompt("Why use Geiter?", "evaluation")
        self.store.record_observation(prompt_a["id"], "provider-a", "Geiter", ["https://other.test"])
        self.store.record_observation(prompt_b["id"], "provider-a", "Geiter", ["https://other.test"])
        baseline = self.store.save_baseline("before")
        self.store.record_observation(prompt_a["id"], "provider-a", "Geiter", ["https://other.test", "https://geiter.dev/docs"])
        comparison = self.store.compare(baseline["id"])
        self.assertEqual(comparison["pairing"]["pair_count"], 1)
        self.assertEqual(comparison["verdict"], "insufficient_data")
        self.assertEqual(comparison["delta"]["mean_citation_reciprocal_rank"], 0.5)

    def test_doctor_and_report_are_machine_readable(self):
        self.store.init()
        doctor = self.store.doctor()
        report = self.store.report()
        self.assertTrue(doctor["ok"])
        self.assertEqual(report["schema"], "geiter/report-v1")
        self.assertEqual(report["doctor"]["ok"], True)

    def test_context_is_bounded_action_oriented_and_read_only(self):
        self.store.init()
        action = self.store.propose_action("investigate", "Inspect citation coverage", priority="high")
        event_count = len(self.store.events(limit=100000))
        context = self.store.context()
        self.assertEqual(context["schema"], "geiter/context-v1")
        self.assertEqual(context["capabilities"]["version"], geiter.__version__)
        self.assertEqual(context["status"]["open_action_count"], 1)
        self.assertEqual(context["decision"]["open_actions"][0]["id"], action["id"])
        self.assertTrue(context["decision"]["next_action"])
        self.assertEqual(len(self.store.events(limit=100000)), event_count)

    def test_gateway_exposes_tools_and_calls_domain(self):
        self.store.init()
        listed = dispatch(self.store, {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        names = {tool["name"] for tool in listed["result"]["content"][0]["json"]["tools"]}
        self.assertIn("geiter_add_prompt", names)
        self.assertIn("geiter_context", names)
        response = dispatch(
            self.store,
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": "geiter_add_prompt", "arguments": {"text": "What is Geiter?"}},
            },
        )
        self.assertEqual(response["result"]["content"][0]["json"]["kind"], "prompt")

    def test_gateway_advertises_capabilities_and_rejects_invalid_requests(self):
        self.store.init()
        initialized = dispatch(self.store, {
            "jsonrpc": "2.0", "id": 15, "method": "initialize", "params": {},
        })
        capabilities = initialized["result"]["content"][0]["json"]["capabilities"]
        self.assertIn("tools", capabilities)
        self.assertIn("resources", capabilities)
        invalid = dispatch(self.store, {"jsonrpc": "1.0", "id": 16, "method": "tools/list"})
        self.assertEqual(invalid["error"]["code"], -32600)

    def test_capabilities_are_available_as_resource_and_tool(self):
        self.store.init()
        listed = dispatch(self.store, {"jsonrpc": "2.0", "id": 17, "method": "resources/list"})
        uris = {item["uri"] for item in listed["result"]["content"][0]["json"]["resources"]}
        self.assertIn("geiter://capabilities", uris)
        self.assertIn("geiter://context", uris)
        resource = dispatch(self.store, {
            "jsonrpc": "2.0", "id": 18, "method": "resources/read",
            "params": {"uri": "geiter://capabilities"},
        })
        resource_value = json.loads(
            resource["result"]["content"][0]["json"]["contents"][0]["text"]
        )
        tool = dispatch(self.store, {
            "jsonrpc": "2.0", "id": 19, "method": "tools/call",
            "params": {"name": "geiter_capabilities", "arguments": {}},
        })
        self.assertEqual(resource_value, tool["result"]["content"][0]["json"])
        self.assertEqual(resource_value["schema"], "geiter/capabilities-v1")
        context = dispatch(self.store, {
            "jsonrpc": "2.0", "id": 28, "method": "resources/read",
            "params": {"uri": "geiter://context"},
        })
        context_value = json.loads(
            context["result"]["content"][0]["json"]["contents"][0]["text"]
        )
        self.assertEqual(context_value["schema"], "geiter/context-v1")
        context_tool = dispatch(self.store, {
            "jsonrpc": "2.0", "id": 29, "method": "tools/call",
            "params": {"name": "geiter_context", "arguments": {}},
        })
        tool_value = context_tool["result"]["content"][0]["json"]
        self.assertEqual(tool_value["schema"], "geiter/context-v1")
        self.assertEqual(tool_value["status"], context_value["status"])
        self.assertEqual(tool_value["decision"], context_value["decision"])

    def test_jsonl_provider_replays_a_batch(self):
        self.store.init()
        prompt = self.store.add_prompt("What is Geiter?")
        fixture = Path(self.tempdir.name, "answers.jsonl")
        fixture.write_text(
            json.dumps({
                "prompt": "What is Geiter?",
                "provider": "replay",
                "answer": "Geiter is an agent-native GEO runtime.",
                "citations": ["https://geiter.dev/docs"],
            }) + "\n",
            encoding="utf-8",
        )
        observations = observe_prompts(self.store, [prompt], JsonlProvider(fixture))
        self.assertEqual(len(observations), 1)
        self.assertEqual(observations[0]["data"]["provider"], "replay")
        self.assertEqual(load_provider("jsonl", path=fixture).name, "jsonl")

    def test_http_json_provider_posts_prompt_and_parses_answer(self):
        from unittest.mock import MagicMock, patch
        from urllib.request import Request

        response = MagicMock()
        response.__enter__.return_value = response
        response.read.return_value = json.dumps({
            "provider": "remote-fixture",
            "answer": "Geiter is discoverable.",
            "citations": ["https://example.test/geiter"],
        }).encode("utf-8")
        with patch("geiter.providers.urlopen", return_value=response) as request_call:
            provider = HttpJsonProvider(
                "https://provider.test/answer",
                headers={"Authorization": "Bearer test"},
                timeout=4,
            )
            answer = provider.answer("What is Geiter?")

        request = request_call.call_args.args[0]
        self.assertIsInstance(request, Request)
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(json.loads(request.data.decode("utf-8")), {"prompt": "What is Geiter?"})
        self.assertEqual(request.headers["Authorization"], "Bearer test")
        self.assertEqual(request_call.call_args.kwargs["timeout"], 4.0)
        self.assertEqual(answer.provider, "remote-fixture")
        self.assertEqual(answer.citations, ["https://example.test/geiter"])

    def test_http_json_provider_classifies_retryable_and_invalid_responses(self):
        from unittest.mock import MagicMock, patch
        from urllib.error import HTTPError

        with patch(
            "geiter.providers.urlopen",
            side_effect=HTTPError("https://provider.test", 503, "busy", {}, None),
        ):
            with self.assertRaises(ConnectionError):
                HttpJsonProvider("https://provider.test/answer").answer("retry")

        with patch(
            "geiter.providers.urlopen",
            side_effect=HTTPError("https://provider.test", 401, "denied", {}, None),
        ):
            with self.assertRaises(ValueError):
                HttpJsonProvider("https://provider.test/answer").answer("deny")

        with patch("geiter.providers.urlopen") as request_call:
            response = MagicMock()
            response.__enter__.return_value = response
            response.read.return_value = b'{"citations":[]}'
            request_call.return_value = response
            with self.assertRaisesRegex(ValueError, "requires a non-empty answer"):
                HttpJsonProvider("https://provider.test/answer").answer("invalid")

    def test_http_json_provider_requires_safe_endpoint_and_timeout(self):
        with self.assertRaisesRegex(ValueError, "absolute http"):
            HttpJsonProvider("file:///etc/passwd")
        with self.assertRaisesRegex(ValueError, "timeout"):
            HttpJsonProvider("https://provider.test/answer", timeout=0)

    def test_http_json_credentials_never_enter_state_or_events(self):
        from unittest.mock import MagicMock, patch

        self.store.init()
        prompt = self.store.add_prompt("What is Geiter?")
        response = MagicMock()
        response.__enter__.return_value = response
        response.read.return_value = json.dumps({
            "answer": "Geiter is visible.",
            "citations": [],
        }).encode("utf-8")
        provider = HttpJsonProvider(
            "https://provider.test/answer",
            headers={"Authorization": "Bearer secret-token"},
        )
        with patch("geiter.providers.urlopen", return_value=response):
            result = run_provider(self.store, [prompt], provider)
        serialized = json.dumps(self.store.read()) + json.dumps(self.store.events(limit=100000))
        self.assertEqual(result["run"]["data"]["status"], "completed")
        self.assertNotIn("secret-token", serialized)

    def test_run_ledger_records_success_and_failure_without_aborting_batch(self):
        self.store.init()
        prompt_a = self.store.add_prompt("What is Geiter?")
        prompt_b = self.store.add_prompt("Why use Geiter?")

        class FlakyProvider:
            name = "flaky"

            def answer(self, prompt):
                if prompt == "Why use Geiter?":
                    raise TimeoutError("provider timeout")
                return ProviderAnswer(self.name, "Geiter is useful.", ["https://geiter.dev/docs"])

        result = run_provider(self.store, [prompt_a, prompt_b], FlakyProvider())
        self.assertEqual(result["run"]["data"]["status"], "partial")
        self.assertEqual(
            result["run"]["data"]["summary"],
            {"expected": 2, "succeeded": 1, "failed": 1, "pending": 0, "exhausted": 1},
        )
        self.assertEqual(len(result["observations"]), 1)
        self.assertIn("TimeoutError", result["run"]["data"]["attempts"][1]["error"])
        self.assertEqual(result["run"]["data"]["attempts"][1]["error_type"], "TimeoutError")
        self.assertTrue(result["run"]["data"]["attempts"][1]["retryable"])
        self.assertIsInstance(result["run"]["data"]["attempts"][0]["duration_ms"], int)

    def test_run_retries_only_failures_and_never_duplicates_success(self):
        self.store.init()
        prompt_a = self.store.add_prompt("What is Geiter?")
        prompt_b = self.store.add_prompt("Why use Geiter?")
        calls = []

        class RecoveringProvider:
            name = "recovering"

            def answer(self, prompt):
                calls.append(prompt)
                if prompt == "Why use Geiter?" and calls.count(prompt) == 1:
                    raise TimeoutError("temporary")
                return ProviderAnswer(self.name, "Geiter is useful.", ["https://geiter.dev/docs"])

        result = run_provider(self.store, [prompt_a, prompt_b], RecoveringProvider(), max_attempts=2)
        self.assertEqual(calls, ["What is Geiter?", "Why use Geiter?", "Why use Geiter?"])
        self.assertEqual(result["run"]["data"]["status"], "completed")
        self.assertEqual(result["run"]["data"]["summary"]["succeeded"], 2)
        self.assertEqual(len(self.store.read()["observations"]), 2)
        attempts = result["run"]["data"]["attempts"]
        self.assertEqual([item["attempt_number"] for item in attempts], [1, 1, 2])

    def test_non_retryable_errors_stop_retry_schedule(self):
        self.store.init()
        prompt = self.store.add_prompt("What is Geiter?")

        class InvalidProvider:
            name = "invalid"

            def answer(self, _prompt):
                raise ValueError("bad configuration")

        result = run_provider(self.store, [prompt], InvalidProvider(), max_attempts=3)
        self.assertEqual(len(result["run"]["data"]["attempts"]), 1)
        self.assertEqual(result["run"]["data"]["attempts"][0]["retryable"], False)
        self.assertEqual(result["run"]["data"]["summary"]["exhausted"], 0)

    def test_resume_continues_a_partial_run_without_replaying_success(self):
        self.store.init()
        prompt_a = self.store.add_prompt("What is Geiter?")
        prompt_b = self.store.add_prompt("Why use Geiter?")
        calls = []

        class RecoveringProvider:
            name = "recovering"

            def answer(self, prompt):
                calls.append(prompt)
                if prompt == "Why use Geiter?" and calls.count(prompt) == 1:
                    raise TimeoutError("temporary")
                return ProviderAnswer(self.name, "Geiter is useful.", ["https://geiter.dev/docs"])

        run = self.store.start_run("recovering", [prompt_a["id"], prompt_b["id"]], max_attempts=2)
        from geiter.providers import observe_prompts

        observe_prompts(self.store, [prompt_a, prompt_b], RecoveringProvider(), run_id=run["id"])
        first = {"run": self.store.finish_run(run["id"])}
        self.assertEqual(first["run"]["data"]["status"], "running")
        resumed = resume_provider(self.store, first["run"]["id"], RecoveringProvider())
        self.assertEqual(resumed["attempted_prompt_ids"], [prompt_b["id"]])
        self.assertEqual(resumed["run"]["data"]["status"], "completed")
        self.assertEqual(calls, ["What is Geiter?", "Why use Geiter?", "Why use Geiter?"])
        self.assertEqual(len(self.store.read()["observations"]), 2)

    def test_gateway_can_resume_a_run(self):
        self.store.init()
        prompt = self.store.add_prompt("What is Geiter?")
        run = self.store.start_run("fixture", [prompt["id"]], max_attempts=2)
        listed = dispatch(self.store, {"jsonrpc": "2.0", "id": 11, "method": "tools/list"})
        names = {tool["name"] for tool in listed["result"]["content"][0]["json"]["tools"]}
        self.assertIn("geiter_resume", names)
        tools = listed["result"]["content"][0]["json"]["tools"]
        regression_schema = next(tool for tool in tools if tool["name"] == "geiter_regression")["inputSchema"]
        resume_schema = next(tool for tool in tools if tool["name"] == "geiter_resume")["inputSchema"]
        complete_schema = next(tool for tool in tools if tool["name"] == "geiter_complete_action")["inputSchema"]
        skip_schema = next(tool for tool in tools if tool["name"] == "geiter_skip_action")["inputSchema"]
        self.assertIn("baseline_id", regression_schema["properties"])
        self.assertNotIn("baseline_id", resume_schema["properties"])
        self.assertIn("evidence", complete_schema["required"])
        self.assertIn("evidence", skip_schema["required"])
        response = dispatch(self.store, {
            "jsonrpc": "2.0",
            "id": 12,
            "method": "tools/call",
            "params": {
                "name": "geiter_resume",
                "arguments": {"run_id": run["id"], "provider": "fixture"},
            },
        })
        value = response["result"]["content"][0]["json"]
        self.assertEqual(value["resumed_from"], run["id"])
        self.assertEqual(value["run"]["data"]["status"], "completed")

    def test_connection_config_is_portable_and_exposed_to_agents(self):
        self.store.init()
        config = self.store.connection_config()
        self.assertEqual(config["transport"], "stdio")
        self.assertEqual(config["protocol"], "json-rpc")
        self.assertEqual(config["args"][-1], "gateway")
        listed = dispatch(self.store, {"jsonrpc": "2.0", "id": 13, "method": "tools/list"})
        names = {tool["name"] for tool in listed["result"]["content"][0]["json"]["tools"]}
        self.assertIn("geiter_connect", names)
        response = dispatch(self.store, {
            "jsonrpc": "2.0", "id": 14, "method": "tools/call",
            "params": {"name": "geiter_connect", "arguments": {"format": "vscode"}},
        })
        value = response["result"]["content"][0]["json"]
        self.assertEqual(value["servers"]["geiter"]["type"], "stdio")

    def test_regression_gate_is_machine_decidable(self):
        self.store.init()
        self.store.add_prompt("What is Geiter?")
        result = regression_run(self.store, self.store.prompts(), load_provider("fixture"))
        self.assertTrue(result["gate"]["ok"])
        self.assertEqual(result["gate"]["schema"], "geiter/gate-v1")
        self.assertEqual(result["gate"]["checks"][1]["name"], "run_completed")
        self.assertEqual(result["gate"]["action"]["type"], "continue")
        stored_run = self.store.run(result["run"]["id"])
        self.assertEqual(stored_run["data"]["gate"]["run_id"], result["run"]["id"])
        report = self.store.report()
        self.assertEqual(report["latest_gate"]["run_id"], result["run"]["id"])
        context = self.store.context()
        self.assertEqual(context["decision"]["latest_gate"]["ok"], True)

    def test_regression_gate_rejects_empty_batches(self):
        self.store.init()
        result = regression_run(self.store, [], load_provider("fixture"))
        self.assertFalse(result["gate"]["ok"])
        checks = {check["name"]: check["ok"] for check in result["gate"]["checks"]}
        self.assertFalse(checks["prompts_present"])
        self.assertEqual(result["gate"]["action"]["type"], "add_prompts")
        self.assertEqual(result["gate"]["action_record"]["data"]["status"], "open")

    def test_regression_gate_rejects_unknown_baseline_reference(self):
        self.store.init()
        self.store.add_prompt("What is Geiter?")
        result = regression_run(
            self.store,
            self.store.prompts(),
            load_provider("fixture"),
            baseline_id="baseline_missing",
        )
        gate = result["gate"]
        self.assertFalse(gate["ok"])
        self.assertEqual(gate["comparison"]["status"], "unknown_baseline")
        self.assertEqual(gate["action"]["type"], "select_valid_baseline")
        self.assertIn("baseline_comparison", gate["failed_checks"])
        self.assertEqual(
            self.store.report()["latest_gate"]["comparison_status"],
            "unknown_baseline",
        )

    def test_regression_gate_compares_only_the_current_run_and_accepts_flat(self):
        self.store.init()
        prompt_a = self.store.add_prompt("What is Geiter?")
        prompt_b = self.store.add_prompt("Why use Geiter?")
        answer = "Geiter is an agent-native GEO runtime."
        citations = ["https://geiter.dev/docs"]
        self.store.record_observation(prompt_a["id"], "fixture", answer, citations)
        self.store.record_observation(prompt_b["id"], "fixture", answer, citations)
        baseline = self.store.save_baseline("before")

        older_run = self.store.start_run("fixture", [prompt_a["id"], prompt_b["id"]])
        self.store.record_observation(prompt_a["id"], "fixture", answer, citations, run_id=older_run["id"])
        self.store.record_observation(prompt_b["id"], "fixture", answer, citations, run_id=older_run["id"])
        self.store.finish_run(older_run["id"])

        provider = FixtureProvider({
            prompt_a["data"]["text"]: ProviderAnswer("fixture", answer, citations),
            prompt_b["data"]["text"]: ProviderAnswer("fixture", answer, citations),
        })
        result = regression_run(
            self.store,
            self.store.prompts(),
            provider,
            baseline_id=baseline["id"],
        )
        gate = result["gate"]
        self.assertTrue(gate["ok"])
        self.assertEqual(gate["comparison"]["verdict"], "flat")
        self.assertEqual(gate["comparison"]["scope"]["current_run_id"], result["run"]["id"])
        self.assertEqual(gate["comparison"]["scope"]["current_observation_count"], 2)
        self.assertEqual(gate["action"]["type"], "continue")
        self.assertTrue(all(
            item["data"].get("run_id") == result["run"]["id"]
            for item in result["observations"]
        ))

    def test_regression_gate_rejects_a_geo_regression(self):
        self.store.init()
        prompt_a = self.store.add_prompt("What is Geiter?")
        prompt_b = self.store.add_prompt("Why use Geiter?")
        baseline_answer = "Geiter is an agent-native GEO runtime."
        for prompt in (prompt_a, prompt_b):
            self.store.record_observation(
                prompt["id"],
                "fixture",
                baseline_answer,
                ["https://geiter.dev/docs"],
            )
        baseline = self.store.save_baseline("before")
        degraded = "A generic answer without the target entity."
        provider = FixtureProvider({
            prompt_a["data"]["text"]: ProviderAnswer("fixture", degraded, ["https://other.test"]),
            prompt_b["data"]["text"]: ProviderAnswer("fixture", degraded, ["https://other.test"]),
        })
        result = regression_run(
            self.store,
            self.store.prompts(),
            provider,
            baseline_id=baseline["id"],
        )
        gate = result["gate"]
        self.assertFalse(gate["ok"])
        self.assertEqual(gate["comparison"]["verdict"], "regressed")
        self.assertIn("baseline_comparison", gate["failed_checks"])
        self.assertEqual(gate["action"]["type"], "recover_regression")
        self.assertEqual(gate["action"]["priority"], "high")

    def test_regression_gate_requires_enough_pairs_for_a_directional_verdict(self):
        self.store.init()
        prompt_a = self.store.add_prompt("What is Geiter?")
        prompt_b = self.store.add_prompt("Why use Geiter?")
        answer = "Geiter is an agent-native GEO runtime."
        self.store.record_observation(prompt_a["id"], "fixture", answer, ["https://geiter.dev/docs"])
        self.store.record_observation(prompt_b["id"], "fixture", answer, ["https://geiter.dev/docs"])
        baseline = self.store.save_baseline("before")
        provider = FixtureProvider({
            prompt_a["data"]["text"]: ProviderAnswer("fixture", answer, ["https://geiter.dev/docs"]),
            prompt_b["data"]["text"]: ProviderAnswer("fixture", answer, ["https://geiter.dev/docs"]),
        })
        original_prompts = self.store.prompts
        self.store.prompts = lambda: [prompt_a]
        try:
            result = regression_run(
                self.store,
                self.store.prompts(),
                provider,
                baseline_id=baseline["id"],
            )
        finally:
            self.store.prompts = original_prompts
        gate = result["gate"]
        self.assertFalse(gate["ok"])
        self.assertEqual(gate["comparison"]["verdict"], "insufficient_data")
        self.assertEqual(gate["action"]["type"], "collect_comparison_evidence")
        self.assertIn("baseline_comparison", gate["failed_checks"])

    def test_persisted_actions_dedupe_sort_and_complete_idempotently(self):
        self.store.init()
        normal = self.store.propose_action("review", "Review evidence")
        high = self.store.propose_action("repair", "Repair provider", priority="high")
        duplicate = self.store.propose_action("repair", "Repair provider", priority="high")
        self.assertEqual(duplicate["id"], high["id"])
        self.assertEqual([item["id"] for item in self.store.list_actions()], [high["id"], normal["id"]])
        completed = self.store.complete_action(high["id"], {"ticket": "evidence-1"})
        repeated = self.store.complete_action(high["id"])
        self.assertEqual(completed["data"]["status"], "completed")
        self.assertEqual(repeated["data"]["completion_evidence"], {"ticket": "evidence-1"})
        self.assertEqual([item["id"] for item in self.store.list_actions()], [normal["id"]])

    def test_action_proposals_require_meaningful_text(self):
        self.store.init()
        with self.assertRaisesRegex(ValueError, "action_type"):
            self.store.propose_action("", "A useful prompt")
        with self.assertRaisesRegex(ValueError, "prompt"):
            self.store.propose_action("review", " ")

    def test_action_resolution_requires_non_empty_auditable_evidence(self):
        self.store.init()
        action = self.store.propose_action("review", "Review the evidence")
        self.store.claim_action(action["id"], "itr_evidence")
        for evidence in (None, {}, {"text": "  "}, {"items": []}, {"nested": {"verified": True}}):
            with self.subTest(evidence=evidence):
                with self.assertRaisesRegex(ValueError, "non-empty evidence"):
                    self.store.complete_action(action["id"], evidence)
        completed = self.store.complete_action(action["id"], {"verified": True})
        self.assertEqual(completed["data"]["status"], "completed")

    def test_status_report_and_capabilities_surface_open_actions(self):
        self.store.init()
        action = self.store.propose_action("review", "Review evidence")
        report = self.store.report()
        self.assertEqual(report["open_action_count"], 1)
        self.assertEqual(report["open_actions"][0]["id"], action["id"])
        capabilities = self.store.capabilities()
        self.assertEqual(capabilities["action_queue"]["record_kind"], "agent.action")
        self.assertIn("propose", capabilities["action_queue"]["operations"])
        self.assertTrue(capabilities["providers"]["built_in"]["http_json"]["network"])
        self.assertEqual(
            capabilities["regression_gate"]["passing_verdicts"],
            ["improved", "flat"],
        )
        self.assertEqual(
            capabilities["regression_gate"]["persisted_on"],
            "runs[].data.gate",
        )
        self.assertIn("claim_ownership", capabilities["action_queue"])
        self.store.claim_action(action["id"], "itr_test")
        active_report = self.store.report()
        self.assertEqual(active_report["open_action_count"], 0)
        self.assertEqual(active_report["in_progress_action_count"], 1)
        self.assertEqual(active_report["in_progress_actions"][0]["id"], action["id"])
        self.assertEqual(active_report["stale_action_count"], 0)

    def test_gateway_manages_persisted_actions(self):
        self.store.init()
        action = self.store.propose_action("repair", "Fix fixture", priority="high")
        listed = dispatch(self.store, {
            "jsonrpc": "2.0", "id": 20, "method": "tools/call",
            "params": {"name": "geiter_actions", "arguments": {}},
        })
        self.assertEqual(listed["result"]["content"][0]["json"][0]["id"], action["id"])
        completed = dispatch(self.store, {
            "jsonrpc": "2.0", "id": 21, "method": "tools/call",
            "params": {
                "name": "geiter_complete_action",
                "arguments": {"action_id": action["id"], "evidence": {"fixed": True}},
            },
        })
        self.assertEqual(
            completed["result"]["content"][0]["json"]["data"]["status"], "completed"
        )
        listed = dispatch(self.store, {
            "jsonrpc": "2.0", "id": 22, "method": "tools/list",
        })
        names = {tool["name"] for tool in listed["result"]["content"][0]["json"]["tools"]}
        self.assertIn("geiter_capabilities", names)
        self.assertIn("geiter_propose_action", names)
        self.assertIn("geiter_skip_action", names)
        self.assertIn("geiter_reclaim_action", names)

    def test_gateway_proposes_deduplicated_action(self):
        self.store.init()
        request = {
            "jsonrpc": "2.0", "id": 26, "method": "tools/call",
            "params": {
                "name": "geiter_propose_action",
                "arguments": {
                    "type": "investigate",
                    "prompt": "Inspect the weakest citation cell",
                    "priority": "high",
                    "source": "agent:test",
                    "dedupe_key": "coverage:weakest",
                    "evidence": {"origin": "test"},
                },
            },
        }
        first = dispatch(self.store, request)
        second = dispatch(self.store, {**request, "id": 27})
        first_action = first["result"]["content"][0]["json"]
        second_action = second["result"]["content"][0]["json"]
        self.assertEqual(first_action["id"], second_action["id"])
        self.assertEqual(first_action["data"]["priority"], "high")
        self.assertEqual(first_action["data"]["evidence"], {"origin": "test"})
        self.assertEqual(len(self.store.list_actions()), 1)

    def test_gateway_reclaims_stale_action(self):
        self.store.init()
        action = self.store.propose_action("repair", "Recover via gateway")
        self.store.claim_action(action["id"], "itr_gateway", lease_seconds=0)
        stale = dispatch(self.store, {
            "jsonrpc": "2.0", "id": 23, "method": "tools/call",
            "params": {"name": "geiter_actions", "arguments": {"status": "stale"}},
        })
        self.assertEqual(stale["result"]["content"][0]["json"][0]["id"], action["id"])
        all_actions = dispatch(self.store, {
            "jsonrpc": "2.0", "id": 25, "method": "tools/call",
            "params": {"name": "geiter_actions", "arguments": {"status": "all"}},
        })
        self.assertEqual(all_actions["result"]["content"][0]["json"][0]["id"], action["id"])
        reclaimed = dispatch(self.store, {
            "jsonrpc": "2.0", "id": 24, "method": "tools/call",
            "params": {
                "name": "geiter_reclaim_action",
                "arguments": {"action_id": action["id"], "evidence": {"via": "gateway"}},
            },
        })
        self.assertEqual(
            reclaimed["result"]["content"][0]["json"]["data"]["status"], "open"
        )

    def test_gateway_exposes_resources(self):
        self.store.init()
        listed = dispatch(self.store, {"jsonrpc": "2.0", "id": 4, "method": "resources/list"})
        uris = {item["uri"] for item in listed["result"]["content"][0]["json"]["resources"]}
        self.assertEqual(
            uris,
            {
                "geiter://status",
                "geiter://context",
                "geiter://report",
                "geiter://capabilities",
                "geiter://score",
                "geiter://introspect",
                "geiter://experience",
            },
        )
        read = dispatch(self.store, {
            "jsonrpc": "2.0", "id": 5, "method": "resources/read",
            "params": {"uri": "geiter://status"},
        })
        self.assertEqual(read["result"]["content"][0]["json"]["contents"][0]["uri"], "geiter://status")

    def test_gateway_exposes_self_assessment_surfaces(self):
        self.store.init()
        listed = dispatch(self.store, {"jsonrpc": "2.0", "id": 40, "method": "tools/list"})
        names = {tool["name"] for tool in listed["result"]["content"][0]["json"]["tools"]}
        self.assertIn("geiter_score", names)
        self.assertIn("geiter_introspect", names)
        self.assertIn("geiter_execute", names)
        score = dispatch(self.store, {
            "jsonrpc": "2.0", "id": 41, "method": "tools/call",
            "params": {"name": "geiter_score", "arguments": {}},
        })
        self.assertEqual(
            score["result"]["content"][0]["json"]["schema"], "geiter/score-v1"
        )
        introspect = dispatch(self.store, {
            "jsonrpc": "2.0", "id": 42, "method": "tools/call",
            "params": {"name": "geiter_introspect", "arguments": {}},
        })
        self.assertEqual(
            introspect["result"]["content"][0]["json"]["schema"], "geiter/introspect-v1"
        )
        read = dispatch(self.store, {
            "jsonrpc": "2.0", "id": 43, "method": "resources/read",
            "params": {"uri": "geiter://score"},
        })
        self.assertEqual(read["result"]["content"][0]["json"]["contents"][0]["uri"], "geiter://score")

    def test_gateway_execute_runs_a_verified_cycle(self):
        self.store.init()
        self.store.add_prompt("What is Geiter?")
        target = self.store.read()["identity"]["name"]
        response = dispatch(self.store, {
            "jsonrpc": "2.0", "id": 50, "method": "tools/call",
            "params": {
                "name": "geiter_execute",
                "arguments": {
                    "heading": f"{target}: gateway",
                    "body": f"{target} is cited for this gateway exercise.",
                },
            },
        })
        payload = response["result"]["content"][0]["json"]
        self.assertEqual(payload["schema"], "geiter/execution-v1")
        self.assertIn("verdict", payload)
        self.assertIn("change_id", payload["verdict"])

    def test_gateway_govern_and_approvals(self):
        self.store.init()
        listed = dispatch(self.store, {"jsonrpc": "2.0", "id": 60, "method": "tools/list"})
        names = {tool["name"] for tool in listed["result"]["content"][0]["json"]["tools"]}
        self.assertIn("geiter_govern", names)
        self.assertIn("geiter_approvals", names)
        # An undeclared surface must not auto-execute.
        proposed = dispatch(self.store, {
            "jsonrpc": "2.0", "id": 61, "method": "tools/call",
            "params": {
                "name": "geiter_govern",
                "arguments": {
                    "surface_id": "real-site",
                    "heading": "Gateway heading",
                    "body": "Gateway body",
                },
            },
        })
        payload = proposed["result"]["content"][0]["json"]
        self.assertEqual(payload["status"], "pending_approval")
        request_id = payload["approval_request_id"]
        # Approve it through the gateway.
        approved = dispatch(self.store, {
            "jsonrpc": "2.0", "id": 62, "method": "tools/call",
            "params": {
                "name": "geiter_approvals",
                "arguments": {
                    "operation": "approve",
                    "request_id": request_id,
                    "approver": "human:tester",
                },
            },
        })
        self.assertEqual(
            approved["result"]["content"][0]["json"]["data"]["status"], "approved"
        )

    def test_gateway_exposes_experiment_tools(self):
        self.store.init()
        listed = dispatch(self.store, {"jsonrpc": "2.0", "id": 6, "method": "tools/list"})
        names = {tool["name"] for tool in listed["result"]["content"][0]["json"]["tools"]}
        self.assertIn("geiter_save_baseline", names)
        self.assertIn("geiter_compare", names)
        self.assertIn("geiter_propose_experiment", names)
        self.assertIn("geiter_iterate", names)
        self.assertIn("geiter_health", names)

    def test_provider_plugins_are_discovered(self):
        from unittest.mock import patch

        class FakeEntryPoint:
            def load(self):
                return lambda **_kwargs: JsonlProvider.__new__(JsonlProvider)

        class FakeEntryPoints(list):
            def select(self, *, group, name):
                self.assertions = (group, name)
                return self

        entries = FakeEntryPoints([FakeEntryPoint()])
        with patch("geiter.providers.entry_points", return_value=entries):
            provider = load_provider("custom")
        self.assertIsInstance(provider, JsonlProvider)
        self.assertEqual(entries.assertions, ("geiter.providers", "custom"))
