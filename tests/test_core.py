import json
import tempfile
import unittest
from pathlib import Path

from geiter.core import GeiterStore
from geiter.gateway import dispatch
from geiter.providers import JsonlProvider, ProviderAnswer, load_provider, observe_prompts, run_provider


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

    def test_gateway_exposes_tools_and_calls_domain(self):
        self.store.init()
        listed = dispatch(self.store, {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        names = {tool["name"] for tool in listed["result"]["content"][0]["json"]["tools"]}
        self.assertIn("geiter_add_prompt", names)
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

    def test_gateway_exposes_resources(self):
        self.store.init()
        listed = dispatch(self.store, {"jsonrpc": "2.0", "id": 4, "method": "resources/list"})
        uris = {item["uri"] for item in listed["result"]["content"][0]["json"]["resources"]}
        self.assertEqual(uris, {"geiter://status", "geiter://report"})
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
