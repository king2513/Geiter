import json
import tempfile
import unittest
from pathlib import Path

from geiter.core import GeiterStore
from geiter.gateway import dispatch
from geiter.providers import JsonlProvider, observe_prompts


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

    def test_inspect_ignores_geiter_state(self):
        self.store.init()
        Path(self.tempdir.name, "notes.md").write_text("hello", encoding="utf-8")
        observation = self.store.inspect()
        self.assertEqual(observation["data"]["file_count"], 1)
        self.assertEqual(observation["data"]["extensions"], {".md": 1})

    def test_state_is_valid_json(self):
        self.store.init()
        json.loads(self.store.state_path.read_text(encoding="utf-8"))

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
