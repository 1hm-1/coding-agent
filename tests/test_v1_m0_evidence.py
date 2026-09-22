from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
EVIDENCE = ROOT / "docs" / "evidence" / "v1-m0"
COLLECTOR_PATH = ROOT / "examples" / "v1_m0_collect_evidence.py"


def load_collector():
    spec = importlib.util.spec_from_file_location("v1_m0_collect_evidence", COLLECTOR_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class M0EvidenceContractTest(unittest.TestCase):
    def test_metric_dictionary_has_required_classification_and_metrics(self) -> None:
        dictionary = json.loads((EVIDENCE / "metric-dictionary-v1.json").read_text(encoding="utf-8"))
        self.assertEqual(dictionary["schema_version"], 1)
        self.assertEqual(
            set(dictionary["classification_values"]),
            {"measured", "estimated", "unknown", "synthetic"},
        )
        metrics = {item["id"]: item for item in dictionary["metrics"]}
        self.assertTrue(
            {
                "task_oracle_success",
                "runtime_completion",
                "end_to_end_elapsed_ms",
                "legacy_eval_wall_time_ms",
                "model_attempt_latency_ms",
                "tool_attempt_latency_ms",
                "context_compaction_latency_ms",
                "machine_active_ms",
                "permission_user_wait_ms",
                "retry_recovery_overhead_ms",
                "token_usage",
                "derived_cost",
                "permission_outcome_and_safety_violation",
                "local_control_ack_ms",
            }
            <= set(metrics)
        )
        self.assertIn("unknown", metrics["token_usage"]["unknown_handling"])
        self.assertIn("unavailable", metrics["local_control_ack_ms"]["classification"])
        self.assertIn("unknown", metrics["end_to_end_elapsed_ms"]["m0_baseline_mapping"])
        self.assertIn("oracle execution", metrics["legacy_eval_wall_time_ms"]["boundary"])

    def test_baseline_is_scripted_and_representative_manifest_is_pinned(self) -> None:
        baseline = json.loads((EVIDENCE / "baseline-manifest-v1.json").read_text(encoding="utf-8"))
        self.assertEqual(baseline["backend_requirement"], "scripted-only")
        self.assertFalse(baseline["provider_calls_authorized"])
        self.assertEqual(baseline["stability"]["repetitions"], 5)
        self.assertEqual(len(baseline["stability"]["case_ids"]), 5)
        tasks = json.loads((EVIDENCE / "representative-tasks-v1.json").read_text(encoding="utf-8"))
        self.assertEqual(len(tasks["tasks"]), 6)
        self.assertEqual({item["repository"] for item in tasks["tasks"]}, {"coding-agent", "hermes-agent"})
        self.assertGreaterEqual(len({item["type"] for item in tasks["tasks"]}), 3)
        for task in tasks["tasks"]:
            self.assertEqual(len(task["base_revision"]), 40)
            self.assertEqual(len(task["reference_revision"]), 40)
            self.assertEqual(len(task["reference_tree"]), 40)
            self.assertTrue(task["request"] and task["oracle"] and task["environment"])
            self.assertTrue(task["setup_command"] and task["oracle_command"])
            self.assertEqual(task["validation"]["status"], "passed")
            self.assertEqual(task["unavailable_prerequisites"], [])

        projection = json.loads((EVIDENCE / "offline-baseline-v1.json").read_text(encoding="utf-8"))
        self.assertEqual(len(projection["full_suite"]["runs"]), 14)
        self.assertEqual(len(projection["stability"]["runs"]), 25)
        for lane in (projection["full_suite"], projection["stability"]):
            self.assertTrue(all(run["legacy_session_id"] for run in lane["runs"]))
            self.assertTrue(all(lane["recalculation_matches_report_metrics"].values()))
            self.assertTrue(all("legacy_metrics" in run and "oracles" in run for run in lane["runs"]))

    def test_projection_keeps_populations_and_unknown_usage_explicit(self) -> None:
        collector = load_collector()
        report = {
            "schema_version": 1,
            "runs": [
                {
                    "case_id": "normal",
                    "repetition": 1,
                    "session_id": "legacy-a",
                    "case_type": "task",
                    "task_success": True,
                    "runtime_completed": True,
                    "metrics": {"input_tokens": 0, "output_tokens": 3, "permission_violations": 1},
                },
                {
                    "case_id": "negative",
                    "repetition": 1,
                    "session_id": "legacy-b",
                    "case_type": "negative_control",
                    "task_success": False,
                    "runtime_completed": True,
                    "metrics": {},
                },
                {
                    "case_id": "invalid",
                    "repetition": 1,
                    "session_id": "",
                    "case_type": "task",
                    "infrastructure_failure": True,
                    "metrics": {},
                },
            ],
        }
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "report.json"
            path.write_text(json.dumps(report), encoding="utf-8")
            projected = collector.project_report(path)
        self.assertEqual(len(projected["populations"]["normal"]), 1)
        self.assertEqual(len(projected["populations"]["negative_control"]), 1)
        self.assertEqual(len(projected["populations"]["infrastructure_invalid"]), 1)
        first = projected["runs"][0]
        self.assertEqual(first["usage"]["classifications"]["input_tokens"], "unknown")
        self.assertIsNone(first["usage"]["total"])
        self.assertEqual(first["legacy_permission_denials"], 1)
        self.assertIsNone(first["proven_safety_violations"])

    def test_projection_uses_runs_jsonl_for_identity_oracles_and_recalculation(self) -> None:
        collector = load_collector()
        metrics = {
            "tool_attempts": 2,
            "tool_calls": 2,
            "tool_executions": 1,
            "tool_latency_ms": 4.0,
            "model_calls": 3,
            "model_latency_ms": 5.0,
            "latency_ms": 6.0,
            "end_to_end_latency_ms": 7.0,
            "test_latency_ms": 8.0,
        }
        run = {
            "case_id": "case-a",
            "repetition": 1,
            "session_id": "legacy-session-a",
            "case_type": "task",
            "valid": True,
            "task_success": True,
            "oracle_success": True,
            "end_to_end_success": True,
            "runtime_completed": True,
            "source_invariant": True,
            "infrastructure_failure": False,
            "failure_reason": None,
            "state": "completed",
            "oracles": [{"kind": "file", "passed": True}],
            "metrics": metrics,
        }
        report = {
            "schema_version": 1,
            "runs": [{key: value for key, value in run.items() if key != "session_id"}],
            "metrics": {name: collector._distribution([float(value)]) for name, value in metrics.items()},
        }
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "report.json"
            path.write_text(json.dumps(report), encoding="utf-8")
            path.with_name("runs.jsonl").write_text(json.dumps(run) + "\n", encoding="utf-8")
            projected = collector.project_report(path)
        first = projected["runs"][0]
        self.assertEqual(first["legacy_session_id"], "legacy-session-a")
        self.assertEqual(first["legacy_metrics"]["tool_attempts"], 2)
        self.assertTrue(first["oracles"][0]["passed"])
        self.assertEqual(first["legacy_eval_wall_time"]["value_ms"], 7.0)
        self.assertTrue(all(projected["recalculation_matches_report_metrics"].values()))

    def test_migration_inventory_versions_every_serialized_format(self) -> None:
        inventory = json.loads(
            (EVIDENCE / "m1-migration-inventory-v1.json").read_text(encoding="utf-8")
        )
        formats = {item["format_id"]: item for item in inventory["serialized_format_versions"]}
        self.assertEqual(formats["runtime_snapshot"]["current_write_version"], 2)
        self.assertEqual(formats["runtime_snapshot"]["accepted_read_versions"], [1, 2])
        self.assertEqual(formats["runtime_event"]["accepted_read_versions"], [1])
        self.assertEqual(formats["summary_record"]["accepted_read_versions"], [1])
        self.assertEqual(formats["memory_record"]["accepted_read_versions"], [1])
        serialized_columns = {
            f"{table}.{column}"
            for table, columns in inventory["serialized_records"].items()
            for column in columns
        }
        catalogued_columns = {
            column
            for item in formats.values()
            for column in item["columns"]
            if column.endswith("_json")
        }
        self.assertEqual(serialized_columns, catalogued_columns)
        self.assertTrue(all(item["reader"] and item["compatibility_requirement"] for item in formats.values()))


if __name__ == "__main__":
    unittest.main()
