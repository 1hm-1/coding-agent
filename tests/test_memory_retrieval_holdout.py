from __future__ import annotations

import json
import unittest
from collections import Counter
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
EVIDENCE = PROJECT_ROOT / "docs" / "evidence" / "memory-retrieval-holdout-2026-09-16.summary.json"
MANIFEST_SHA256 = "3d4bffb06a19ee219534d4649d93e983e7ecd14cebfd90b84ae64feb1f7e2ed1"


class MemoryRetrievalHoldoutTest(unittest.TestCase):
    def test_frozen_holdout_reports_baseline_evidence_and_gate_status(self) -> None:
        # L3 is immutable historical evidence after S3.5. Re-running its known cases
        # with a candidate retriever would turn a holdout into development feedback.
        report = json.loads(EVIDENCE.read_text(encoding="utf-8"))

        self.assertEqual(report["benchmark"], "l3-independent-memory-retrieval-holdout")
        self.assertEqual(report["source_commit"], "8ebc800")
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

        results = report["first_run"]["results"]
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
        acceptance = report["first_run"]["acceptance"]
        self.assertFalse(acceptance["passed"])
        self.assertFalse(acceptance["relevant_recall_at_least_0_85"])
        self.assertFalse(acceptance["irrelevant_injection_at_most_0_15"])
        self.assertTrue(acceptance["scope_revision_stale_deleted_leakage_zero"])
        self.assertTrue(acceptance["no_relevant_memory_behavior_unchanged"])
        self.assertTrue(acceptance["compatibility_warm_3_of_3"])
        self.assertTrue(acceptance["manifest_tokens_match_renderer"])

        compatibility = report["first_run"]["compatibility_arm"]
        self.assertTrue(compatibility["shared_pool"])
        self.assertEqual(report["compatibility_source_commit"], "5298ba0")
        self.assertEqual(compatibility["warm_success"], {"successful": 3, "total": 3})
        self.assertTrue(compatibility["manifest_tokens_match_renderer"])

        self.assertTrue(report["first_run_frozen"])
        self.assertEqual(report["first_run"]["results"]["relevant_recall"], 2 / 3)
        self.assertEqual(
            report["first_run"]["results"]["irrelevant_injection_rate"],
            1 / 6,
        )


if __name__ == "__main__":
    unittest.main()
