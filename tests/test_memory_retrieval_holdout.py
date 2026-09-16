from __future__ import annotations

import json
import os
import subprocess
import sys
import unittest
from collections import Counter
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
HOLDOUT_SCRIPT = PROJECT_ROOT / "examples" / "memory_retrieval_holdout.py"
EVIDENCE = PROJECT_ROOT / "docs" / "evidence" / "memory-retrieval-holdout-2026-09-16.summary.json"
MANIFEST_SHA256 = "3d4bffb06a19ee219534d4649d93e983e7ecd14cebfd90b84ae64feb1f7e2ed1"


class MemoryRetrievalHoldoutTest(unittest.TestCase):
    def test_frozen_holdout_reports_baseline_evidence_and_gate_status(self) -> None:
        environment = os.environ.copy()
        environment["PYTHONPATH"] = str(PROJECT_ROOT / "src")
        completed = subprocess.run(
            [sys.executable, str(HOLDOUT_SCRIPT)],
            capture_output=True,
            text=True,
            env=environment,
            check=False,
        )
        self.assertEqual(completed.returncode, 1)
        report = json.loads(completed.stdout)

        self.assertEqual(report["benchmark"], "l3-independent-memory-retrieval-holdout")
        self.assertEqual(report["baseline_commit"], "8ebc800")
        self.assertEqual(report["case_manifest_sha256"], MANIFEST_SHA256)
        self.assertEqual(report["frozen_case_count"], 18)
        self.assertEqual(
            Counter(report["case_categories"]),
            Counter(
                {
                    "semantic_relevant": 6,
                    "hard_negative": 4,
                    "shared_pool_competition": 2,
                    "scope_isolation": 2,
                    "repository_revision": 1,
                    "stale_deleted": 1,
                    "no_relevant_memory": 1,
                    "prompt_injection_negative": 1,
                }
            ),
        )
        self.assertEqual(
            report["shared_pool_topology"]["cases_per_pool"],
            {"silver-grove": 6, "quiet-cairn": 7, "blue-fern": 5},
        )

        results = report["results"]
        self.assertEqual(results["paired_cases"], 18)
        self.assertEqual(results["relevant_memory_records"], 15)
        self.assertEqual(results["task_success"], {
            "cold": 1 / 6,
            "cold_successful": 3,
            "warm": 13 / 18,
            "warm_successful": 13,
        })
        self.assertEqual(results["relevant_recall"], 2 / 3)
        self.assertEqual(results["precision"], 5 / 6)
        self.assertEqual(results["irrelevant_injection_rate"], 1 / 6)
        self.assertEqual(results["unrelated_memory_behavior_change_rate"], 0.0)
        self.assertEqual(results["retrieval_tokens"], {"total": 274, "mean": 274 / 18})
        self.assertEqual(
            results["memory_context_tokens"],
            {"total": 381, "mean": 381 / 18, "max": 53},
        )
        self.assertEqual(results["model_tokens"], {"cold_total": 4274, "warm_total": 4655})
        self.assertEqual(
            results["leakage_selected_counts"],
            {"scope": 0, "revision": 0, "stale_deleted": 0},
        )
        self.assertTrue(results["no_relevant_memory_behavior_unchanged"])
        self.assertTrue(results["manifest_tokens_match_renderer"])
        self.assertGreater(results["wall_latency_ms"]["cold"]["total"], 0.0)
        self.assertGreater(results["wall_latency_ms"]["warm"]["total"], 0.0)

        acceptance = report["acceptance"]
        self.assertFalse(acceptance["passed"])
        self.assertFalse(acceptance["relevant_recall_at_least_0_85"])
        self.assertFalse(acceptance["irrelevant_injection_at_most_0_15"])
        self.assertTrue(acceptance["scope_revision_stale_deleted_leakage_zero"])
        self.assertTrue(acceptance["no_relevant_memory_behavior_unchanged"])
        self.assertTrue(acceptance["compatibility_warm_3_of_3"])
        self.assertTrue(acceptance["manifest_tokens_match_renderer"])

        compatibility = report["compatibility_arm"]
        self.assertTrue(compatibility["shared_pool"])
        self.assertEqual(compatibility["source_commit"], "5298ba0")
        self.assertEqual(compatibility["warm_success"], {"successful": 3, "total": 3})
        self.assertTrue(compatibility["manifest_tokens_match_renderer"])

        restricted_paths = (
            "src/coding_agent/memory/retrieval.py",
            "src/coding_agent/context.py",
            "src/coding_agent/memory/evaluation.py",
        )
        restricted_diff = subprocess.run(
            ["git", "diff", "--name-only", "8ebc800", "--", *restricted_paths],
            capture_output=True,
            text=True,
            cwd=PROJECT_ROOT,
            check=True,
        )
        self.assertEqual(restricted_diff.stdout, "")

        evidence = json.loads(EVIDENCE.read_text(encoding="utf-8"))
        self.assertTrue(evidence["first_run_frozen"])
        self.assertEqual(evidence["case_manifest_sha256"], MANIFEST_SHA256)
        self.assertEqual(evidence["first_run"]["results"]["relevant_recall"], 2 / 3)
        self.assertEqual(
            evidence["first_run"]["results"]["irrelevant_injection_rate"],
            1 / 6,
        )

        holdout_cases = report["case_plan"]
        self.assertTrue(
            all(
                forbidden not in json.dumps(case, ensure_ascii=False).casefold()
                for case in holdout_cases
                for forbidden in ("invoice", "deploy color", "alias")
            )
        )


if __name__ == "__main__":
    unittest.main()
