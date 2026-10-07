import json
import tempfile
import unittest
from pathlib import Path

import geiter
from geiter.core import GeiterStore
from geiter.gateway import dispatch
from geiter.providers import (
    JsonlProvider,
    ProviderAnswer,
    load_provider,
    observe_prompts,
    regression_run,
    resume_provider,
    run_provider,
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
        with self.assertRaisesRegex(ValueError, "lease is still active"):
            self.store.reclaim_action(action["id"], at=claimed["data"]["claimed_at"])

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

    def test_prompt_identity_is_deterministic_and_deduplicated(self):
        self.store.init()
        first = self.store.add_prompt("  What is   Geiter? ")
        second = self.store.add_prompt("what is geiter?")
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(len(self.store.read()["prompts"]), 1)

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

    def test_regression_gate_rejects_empty_batches(self):
        self.store.init()
        result = regression_run(self.store, [], load_provider("fixture"))
        self.assertFalse(result["gate"]["ok"])
        checks = {check["name"]: check["ok"] for check in result["gate"]["checks"]}
        self.assertFalse(checks["prompts_present"])
        self.assertEqual(result["gate"]["action"]["type"], "add_prompts")
        self.assertEqual(result["gate"]["action_record"]["data"]["status"], "open")

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

    def test_status_report_and_capabilities_surface_open_actions(self):
        self.store.init()
        action = self.store.propose_action("review", "Review evidence")
        report = self.store.report()
        self.assertEqual(report["open_action_count"], 1)
        self.assertEqual(report["open_actions"][0]["id"], action["id"])
        capabilities = self.store.capabilities()
        self.assertEqual(capabilities["action_queue"]["record_kind"], "agent.action")
        self.assertIn("propose", capabilities["action_queue"]["operations"])
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
            {"geiter://status", "geiter://context", "geiter://report", "geiter://capabilities"},
        )
        read = dispatch(self.store, {
            "jsonrpc": "2.0", "id": 5, "method": "resources/read",
            "params": {"uri": "geiter://status"},
        })
        self.assertEqual(read["result"]["content"][0]["json"]["contents"][0]["uri"], "geiter://status")

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
