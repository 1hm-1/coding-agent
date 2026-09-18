import json
import unittest
from collections import Counter
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
MANIFEST_PATH = ROOT / "docs" / "evidence" / "memory-retrieval-holdout-v2.manifest.json"
REPOSITORY_BODY_PATH = ROOT / "examples" / "memory_retrieval_holdout_v2.json"


class MemoryRetrievalHoldoutV2ManifestContract(unittest.TestCase):
    def load_manifest(self) -> dict[str, Any]:
        return json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))

    def test_body_is_out_of_repo_and_execution_is_recorded(self) -> None:
        manifest = self.load_manifest()
        storage = manifest["body_storage"]
        execution = manifest["execution"]

        self.assertFalse(REPOSITORY_BODY_PATH.exists())
        self.assertEqual(storage["kind"], "user_managed_out_of_repo")
        self.assertIsNone(storage["repository_path"])
        self.assertFalse(storage["relative_label"].startswith("/"))
        self.assertEqual(storage["content_sha256"], manifest["suite_sha256"])
        self.assertEqual(
            manifest["freeze_status"],
            "body_frozen_algorithm_f1d03cf_executed_once_results_external",
        )
        self.assertTrue(execution["executed"])
        self.assertTrue(execution["results_generated"])
        self.assertFalse(execution["first_run_reserved"])
        self.assertEqual(
            execution["first_run_result_sha256"],
            "49e979db9b97f0000cae1f1fdae4d13e1d9d15f67ff584211c522813b2351122",
        )
        self.assertEqual(execution["valid_case_count"], 20)
        self.assertEqual(execution["invalid_case_count"], 0)
        self.assertEqual(execution["acceptance"]["compatibility_arm_warm"], "3/3")

    def test_case_ids_oracles_and_shared_pool_metadata_are_frozen(self) -> None:
        manifest = self.load_manifest()
        main = manifest["main_holdout"]
        cases = manifest["cases"]

        self.assertEqual(main["case_count"], 20)
        self.assertEqual(main["repository_count"], 4)
        self.assertEqual(main["shared_memory_pool_count"], 4)
        self.assertEqual(main["minimum_cases_per_shared_pool"], 5)
        self.assertEqual(len(cases), main["case_count"])
        self.assertEqual(len({case["case_id"] for case in cases}), len(cases))
        self.assertEqual(
            Counter(case["category"] for case in cases),
            Counter(main["category_counts"]),
        )
        self.assertEqual(
            Counter(case["memory_pool_id"] for case in cases),
            Counter(
                {
                    repository["memory_pool_id"]: repository["case_count"]
                    for repository in manifest["repositories"]
                }
            ),
        )
        self.assertTrue(
            all(
                case["expected_oracle_type"]
                in {"test_result", "test_and_file", "file_behavior", "tool_behavior"}
                for case in cases
            )
        )
        self.assertEqual(
            sorted(
                case["case_id"]
                for case in cases
                if case["category"] == "revision_isolation"
                or case["case_id"] in main["revision_isolation_case_ids"]
            ),
            sorted(main["revision_isolation_case_ids"]),
        )

    def test_compatibility_arm_is_separate_and_recorded(self) -> None:
        manifest = self.load_manifest()
        arm = manifest["compatibility_arm"]

        self.assertEqual(arm["source_commit"], "5298ba0")
        self.assertEqual(arm["count"], 3)
        self.assertFalse(arm["counted_in_main_holdout"])
        self.assertEqual(arm["execution_status"], "recorded_with_first_run")
        self.assertEqual(arm["warm_success_count"], 3)
        self.assertEqual(arm["warm_success_total"], 3)
        self.assertTrue(arm["manifest_tokens_match_renderer"])


if __name__ == "__main__":
    unittest.main()
