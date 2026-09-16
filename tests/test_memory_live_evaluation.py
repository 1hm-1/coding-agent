from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from coding_agent.memory.live_evaluation import LiveMemoryABRunner, load_live_memory_suite
from coding_agent.models.scripted import ScriptedBackend


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class LiveMemoryEvaluationTest(unittest.TestCase):
    def _manifest(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "cases": [
                {
                    "schema_version": 1,
                    "case_id": "calculator-memory-pair",
                    "fixture": "examples/fixture",
                    "task": "Fix calculator add and preserve the established operator convention.",
                    "policy": {"max_steps": 12, "max_model_calls": 3, "max_tool_calls": 2},
                    "required_facts": ["the prior operator convention is useful context"],
                    "oracles": [
                        {
                            "kind": "file",
                            "path": "calculator.py",
                            "contains": "return left + right",
                        },
                        {"kind": "changed_paths", "allow": ["calculator.py"]},
                    ],
                    "query": {"user_id": "live-user", "top_k": 3, "token_budget": 128},
                    "memories": [
                        {
                            "memory_id": "memory-relevant",
                            "content": "Fix calculator add and preserve the established operator convention using plus",
                            "scope": "user",
                            "scope_id": "live-user",
                            "relevant": True,
                        },
                        {
                            "memory_id": "memory-wrong-user",
                            "content": "Fix calculator add and preserve the established operator convention using multiplication",
                            "scope": "user",
                            "scope_id": "other-user",
                            "relevant": False,
                        },
                    ],
                    "relevant_tools": ["edit_file"],
                }
            ],
        }

    @staticmethod
    def _backend() -> ScriptedBackend:
        return ScriptedBackend(
            [
                {
                    "tool_calls": [
                        {
                            "id": "edit-calculator",
                            "name": "edit_file",
                            "arguments": {
                                "path": "calculator.py",
                                "old_text": "return left - right",
                                "new_text": "return left + right",
                            },
                        }
                    ],
                    "usage": {"input_tokens": 40, "output_tokens": 10},
                },
                {
                    "final": "Updated calculator.py.",
                    "usage": {"input_tokens": 55, "output_tokens": 5},
                },
            ]
        )

    def test_paired_harness_alternates_order_and_reports_audited_memory(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manifest = root / "suite.json"
            manifest.write_text(json.dumps(self._manifest()), encoding="utf-8")
            suite, _ = load_live_memory_suite(manifest)
            runner = LiveMemoryABRunner(
                root / "agent-home",
                suite_root=PROJECT_ROOT,
                provider="scripted-test-double",
                model="fixed-script-v1",
                backend_factory=self._backend,
                allow_non_live_backend=True,
            )
            report = runner.run(suite, repetitions=2, output_dir=root / "report")

            self.assertEqual(report["pair_count"], 2)
            self.assertEqual(
                [item["order"] for item in report["pair_orders"]],
                [["off", "on"], ["on", "off"]],
            )
            self.assertEqual(report["arms"]["off"]["end_to_end_success"], 2)
            self.assertEqual(report["arms"]["on"]["end_to_end_success"], 2)
            self.assertEqual(report["arms"]["on"]["scope_leakage"], 0)
            self.assertEqual(report["arms"]["on"]["permission_violations"], 0)
            self.assertEqual(report["arms"]["on"]["source_invariant_failures"], 0)
            self.assertGreater(report["arms"]["on"]["retrieval_tokens"]["total"], 0)
            self.assertEqual(
                report["arms"]["on"]["tool_calls_before_first_relevant_action"]["total"],
                0,
            )

            warm_runs = [item for item in report["runs"] if item["arm"] == "on"]
            self.assertEqual(len(warm_runs), 2)
            for run in warm_runs:
                memory = run["memory"]
                self.assertIsNotNone(memory)
                self.assertEqual(memory["included_memory_ids"], ["memory-relevant"])
                self.assertTrue(memory["retrieval_id"].startswith("retrieval-"))
                self.assertGreater(memory["actual_context_token_cost"], 0)
                record = memory["records"][0]
                self.assertEqual(record["memory_id"], "memory-relevant")
                self.assertIn("schema_version", record)
                self.assertIn("record_version", record)
                self.assertIn("score", record)
                self.assertIn("provenance", record)
                self.assertIn("actual_context_token_cost", record)
                self.assertTrue(run["provenance"][0]["verified_runtime_result"])

            encoded = json.dumps(report)
            self.assertNotIn(str(root.resolve()), encoded)
            self.assertTrue((root / "report" / "report.json").is_file())

    def test_live_mode_rejects_a_non_provider_backend_label(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaisesRegex(ValueError, "real provider"):
                LiveMemoryABRunner(
                    temporary,
                    suite_root=PROJECT_ROOT,
                    provider="scripted",
                    model="test",
                    backend_factory=self._backend,
                )


if __name__ == "__main__":
    unittest.main()
