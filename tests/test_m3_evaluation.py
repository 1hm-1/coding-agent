from __future__ import annotations

import tempfile
import unittest
import json
from pathlib import Path

from coding_agent.evaluation import (
    EvalValidationError,
    EvaluationReport,
    EvaluationRunner,
    EvalCase,
    EvalSuite,
    load_eval_suite,
)
from coding_agent.application import AgentApplication
from coding_agent.context import (
    BudgetedContextBuilder,
    ExactTokenCounter,
    ModelCapability,
    ModelCapabilityRegistry,
)
from tests.native_support import require_native_sandbox


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class EvaluationHarnessTest(unittest.TestCase):
    def suite(self) -> EvalSuite:
        return EvalSuite(
            schema_version=1,
            cases=(
                EvalCase.from_dict(
                    {
                        "schema_version": 1,
                        "case_id": "success",
                        "fixture": "examples/fixture",
                        "task": "Fix add and run tests",
                        "backend": {
                            "kind": "scripted",
                            "fixture": "examples/scripted_run.json",
                        },
                        "policy": {"max_steps": 32},
                        "required_facts": ["add must be fixed"],
                        "oracles": [
                            {"kind": "test_profile", "profile": "python_unittest"},
                            {"kind": "changed_paths", "allow": ["calculator.py"]},
                        ],
                    }
                ),
                EvalCase.from_dict(
                    {
                        "schema_version": 1,
                        "case_id": "task-fail",
                        "fixture": "examples/fixture",
                        "task": "Make an unrequested impossible change",
                        "backend": {
                            "kind": "scripted",
                            "responses": [{"final": "I did nothing"}],
                        },
                        "oracles": [
                            {
                                "kind": "file",
                                "path": "calculator.py",
                                "contains": "return a * b",
                            }
                        ],
                    }
                ),
                EvalCase.from_dict(
                    {
                        "schema_version": 1,
                        "case_id": "runtime-fail",
                        "fixture": "examples/fixture",
                        "task": "Read the repository",
                        "backend": {"kind": "scripted", "responses": [{"tool_calls": "bad"}]},
                        "oracles": [
                            {
                                "kind": "file",
                                "path": "calculator.py",
                                "contains": "return a * b",
                            }
                        ],
                    }
                ),
                EvalCase.from_dict(
                    {
                        "schema_version": 1,
                        "case_id": "recovery",
                        "fixture": "examples/fixture",
                        "task": "Read and answer",
                        "backend": {
                            "kind": "scripted",
                            "responses": [
                                {
                                    "tool_calls": [
                                        {
                                            "id": "read-1",
                                            "name": "read_file",
                                            "arguments": {"path": "calculator.py"},
                                        }
                                    ]
                                },
                                {"final": "read"},
                            ],
                            "fault_stage": "after_tool_result",
                            "resume": True,
                        },
                        "oracles": [
                            {"kind": "file", "path": "calculator.py", "exists": True},
                            {"kind": "result_schema", "required": ["state", "session_id"]},
                        ],
                    }
                ),
            ),
        )

    def test_manifest_validation_and_containment(self) -> None:
        with self.assertRaises(EvalValidationError):
            EvalCase.from_dict(
                {
                    "schema_version": 1,
                    "case_id": "bad",
                    "fixture": "../outside",
                    "task": "task",
                    "backend": {"kind": "scripted", "responses": [{"final": "x"}]},
                    "oracles": [{"kind": "result_schema", "required": []}],
                    "unknown": True,
                }
            )

    @require_native_sandbox
    def test_runner_separates_task_success_from_runtime_completion(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            report = EvaluationRunner(
                Path(temporary) / "agent-home",
                suite_root=PROJECT_ROOT,
            ).run(
                self.suite(),
                repetitions=1,
                variant="budgeted",
                output_dir=Path(temporary) / "eval",
            )
            self.assertIsInstance(report, EvaluationReport)
            aggregate = report.report
            self.assertEqual(aggregate["requested_run_count"], 4)
            self.assertEqual(aggregate["valid_run_count"], 4)
            self.assertEqual(aggregate["task_success_rate"], 0.5)
            self.assertEqual(aggregate["runtime_completion_rate"], 0.75)
            self.assertEqual(aggregate["recovery_rate"], 1.0)
            self.assertTrue((report.output_dir / "manifest.snapshot.json").exists())
            self.assertTrue((report.output_dir / "runs.jsonl").exists())
            self.assertTrue((report.output_dir / "report.json").exists())
            self.assertTrue((report.output_dir / "report.md").exists())
            report_json = json.loads(
                (report.output_dir / "report.json").read_text(encoding="utf-8")
            )
            self.assertTrue(all("session_id" not in run for run in report_json["runs"]))
            self.assertTrue(all("trace_path" not in run for run in report_json["runs"]))
            self.assertIn("test_latency_ms", report_json["metrics"])
            by_case = {(run.case_id, run.repetition): run for run in report.runs}
            self.assertTrue(by_case[("success", 1)].task_success)
            self.assertTrue(by_case[("success", 1)].runtime_completed)
            self.assertFalse(by_case[("task-fail", 1)].task_success)
            self.assertTrue(by_case[("task-fail", 1)].runtime_completed)
            self.assertFalse(by_case[("runtime-fail", 1)].runtime_completed)
            self.assertTrue(by_case[("recovery", 1)].metrics["recovery_triggered"])
            self.assertEqual(by_case[("recovery", 1)].metrics["recovery_events"], 1)

    def test_paired_ab_compressed_variant_preserves_case_repetition_keys(self) -> None:
        suite = EvalSuite(
            schema_version=1,
            cases=(
                EvalCase.from_dict(
                    {
                        "schema_version": 1,
                        "case_id": "compressed-pair",
                        "fixture": "examples/fixture",
                        "task": "Read calculator.py and answer",
                        "backend": {
                            "kind": "scripted",
                            "responses": [
                                {
                                    "tool_calls": [
                                        {
                                            "id": "read-1",
                                            "name": "read_file",
                                            "arguments": {"path": "calculator.py"},
                                        }
                                    ]
                                },
                                {"final": "read"},
                            ],
                            "compression_responses": [
                                {
                                    "final": json.dumps(
                                        {
                                            "schema_version": 1,
                                            "goals": ["Read calculator.py and answer"],
                                            "constraints": [],
                                            "decisions": ["Use the read observation"],
                                            "files_read": [],
                                            "edits": [],
                                            "tests": [],
                                            "errors": [],
                                            "unresolved": [],
                                        }
                                    ),
                                    "usage": {"input_tokens": 11, "output_tokens": 7},
                                }
                            ],
                        },
                        "policy": {"max_output_tokens": 0},
                        "oracles": [
                            {"kind": "result_schema", "required": ["state"]}
                        ],
                    }
                ),
            ),
        )

        counter = ExactTokenCounter(lambda _provider, _model, messages: len(messages))
        tiny_registry = ModelCapabilityRegistry(
            [
                ModelCapability(
                    provider="scripted",
                    model="scripted",
                    context_limit=5,
                    protocol_margin_tokens=0,
                    token_counter=counter,
                )
            ]
        )

        def application_factory(agent_home: Path, **kwargs: object) -> AgentApplication:
            builder = kwargs["context_builder"]
            if isinstance(builder, BudgetedContextBuilder):
                builder = BudgetedContextBuilder(
                    capability_registry=tiny_registry,
                    token_counter=counter,
                    system_policy="",
                )
            return AgentApplication(
                agent_home,
                context_builder=builder,  # type: ignore[arg-type]
                compression_engine=kwargs["compression_engine"],  # type: ignore[arg-type]
            )

        with tempfile.TemporaryDirectory() as temporary:
            result = EvaluationRunner(
                Path(temporary) / "agent-home",
                suite_root=PROJECT_ROOT,
                application_factory=application_factory,
            ).run_ab(
                suite,
                variants=("passthrough", "compressed"),
                output_dir=Path(temporary) / "ab",
            )
            self.assertIn("paired_diff", result)
            self.assertEqual(result["paired_diff"]["variable"], "context_policy")
            self.assertEqual(len(result["paired_diff"]["pairs"]), 1)
            compressed = result["compressed"]
            self.assertIsInstance(compressed, EvaluationReport)
            self.assertEqual(
                compressed.report["metrics"]["compression_output_tokens"]["mean"],
                7.0,
            )
            self.assertTrue((Path(temporary) / "ab" / "paired_diff.json").exists())

    def test_oracle_configuration_failure_is_separate_from_agent_failure(self) -> None:
        suite = EvalSuite(
            schema_version=1,
            cases=(
                EvalCase.from_dict(
                    {
                        "schema_version": 1,
                        "case_id": "bad-oracle",
                        "fixture": "examples/fixture",
                        "task": "Return an answer",
                        "backend": {
                            "kind": "scripted",
                            "responses": [{"final": "done"}],
                        },
                        "oracles": [
                            {"kind": "test_profile", "profile": "not-trusted"}
                        ],
                    }
                ),
            ),
        )
        with tempfile.TemporaryDirectory() as temporary:
            report = EvaluationRunner(
                Path(temporary) / "agent-home",
                suite_root=PROJECT_ROOT,
            ).run(suite, output_dir=Path(temporary) / "eval")
            self.assertEqual(report.report["valid_run_count"], 0)
            self.assertEqual(report.report["infrastructure_failure_count"], 1)
            self.assertIsNone(report.report["task_success_rate"])
            self.assertEqual(
                report.report["infrastructure_failure_reasons"],
                {"eval_infrastructure_failure": 1},
            )

    def test_load_suite_uses_manifest_parent_as_default_root(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manifest = root / "suite.json"
            manifest.write_text(
                '{"schema_version": 1, "case_id": "one", "fixture": ".", '
                '"task": "task", "backend": {"kind": "scripted", '
                '"responses": [{"final": "done"}]}, "oracles": '
                '[{"kind": "result_schema", "required": ["state"]}]}',
                encoding="utf-8",
            )
            suite, manifest_root = load_eval_suite(manifest)
            self.assertEqual(manifest_root, root.resolve())
            self.assertEqual(suite.cases[0].case_id, "one")

    def test_checked_in_suite_covers_multiple_repositories(self) -> None:
        suite, manifest_root = load_eval_suite(PROJECT_ROOT / "examples" / "eval_suite.json")

        self.assertEqual(manifest_root, PROJECT_ROOT / "examples")
        self.assertGreaterEqual(len(suite.cases), 13)
        fixtures = {case.fixture for case in suite.cases}
        self.assertEqual(
            fixtures,
            {
                "fixture",
                "todo_cli",
                "mini_repos/checkout_service",
                "mini_repos/order_pipeline",
                "mini_repos/settings_service",
                "mini_repos/long_history",
            },
        )
        case_ids = {case.case_id for case in suite.cases}
        self.assertIn("todo-recovery", case_ids)
        self.assertIn("todo-readonly-inspection", case_ids)
        self.assertIn("checkout-cross-file-location", case_ids)
        self.assertIn("checkout-location-failure", case_ids)
        self.assertIn("pipeline-multi-file-recovery", case_ids)
        self.assertIn("settings-scoped-edit", case_ids)
        self.assertIn("settings-scope-violation", case_ids)
        self.assertIn("long-history-compression", case_ids)

        for fixture in fixtures - {"fixture", "todo_cli"}:
            fixture_path = (manifest_root / fixture).resolve()
            files = [
                path
                for path in fixture_path.rglob("*")
                if path.is_file() and "__pycache__" not in path.parts
            ]
            line_count = sum(
                len(path.read_text(encoding="utf-8").splitlines()) for path in files
            )
            self.assertGreaterEqual(line_count, 20, fixture)
            self.assertLessEqual(line_count, 200, fixture)


if __name__ == "__main__":
    unittest.main()
