from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
import unittest
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
SUITE_PATH = ROOT / "examples" / "memory_retrieval_backend_development.json"
V2_MANIFEST_PATH = ROOT / "docs" / "evidence" / "memory-retrieval-holdout-v2.manifest.json"


REQUIRED_METADATA_FIELDS = {
    "fact_type",
    "entities",
    "concept_keys",
    "repository_component",
    "validity",
    "revision",
}
FORBIDDEN_METADATA_KEYS = {
    "answer",
    "expected_answer",
    "tool",
    "tool_instruction",
    "permission",
    "permissions",
}


class MemoryRetrievalBackendDevelopmentSuiteTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.suite: dict[str, Any] = json.loads(SUITE_PATH.read_text(encoding="utf-8"))
        cls.v2_manifest: dict[str, Any] = json.loads(
            V2_MANIFEST_PATH.read_text(encoding="utf-8")
        )

    def test_suite_is_explicit_development_data_and_not_holdout_v3(self) -> None:
        self.assertEqual(self.suite["data_classification"], "development_data")
        self.assertEqual(
            self.suite["holdout_status"], "development_only_no_holdout_v3"
        )
        self.assertFalse(self.suite["algorithm_implementation_changed"])
        self.assertEqual(len(self.suite["cases"]), 40)

    def test_category_counts_match_requested_development_mix(self) -> None:
        counts = Counter(case["category"] for case in self.suite["cases"])
        self.assertEqual(counts, Counter(self.suite["category_targets"]))

    def test_cases_use_eight_shared_pools_and_eight_domains(self) -> None:
        repositories = self.suite["repositories"]
        pools = self.suite["memory_pools"]
        cases = self.suite["cases"]
        self.assertGreaterEqual(len(repositories), 8)
        self.assertGreaterEqual(len({item["domain"] for item in repositories.values()}), 8)
        pool_case_counts = Counter(case["memory_pool_id"] for case in cases)
        self.assertEqual(set(pool_case_counts), set(pools))
        self.assertTrue(all(count >= 2 for count in pool_case_counts.values()))
        self.assertTrue(all(case["repository_id"] in repositories for case in cases))
        self.assertTrue(all(case["memory_pool_id"] in pools for case in cases))

    def test_each_case_has_relevant_ids_and_structured_metadata(self) -> None:
        repositories = self.suite["repositories"]
        pools = self.suite["memory_pools"]
        case_ids = [case["case_id"] for case in self.suite["cases"]]
        self.assertEqual(len(case_ids), len(set(case_ids)))
        for case in self.suite["cases"]:
            self.assertIsInstance(case["query"], str)
            self.assertTrue(case["query"].strip())
            self.assertIsInstance(case["expected_relevant_memory_ids"], list)
            metadata = case["metadata_ground_truth"]
            self.assertEqual(set(metadata), REQUIRED_METADATA_FIELDS)
            self.assertFalse(FORBIDDEN_METADATA_KEYS & set(metadata))
            self.assertIsInstance(metadata["entities"], list)
            self.assertIsInstance(metadata["concept_keys"], list)
            self.assertIsInstance(metadata["revision"], str)
            repository = repositories[case["repository_id"]]
            self.assertEqual(metadata["revision"], repository["revision"])
            component = metadata["repository_component"]
            self.assertIn(component, {"none", *repository["files"]})
            pool = pools[case["memory_pool_id"]]
            records = {record["memory_id"]: record for record in pool["records"]}
            relevant = set(case["expected_relevant_memory_ids"])
            excluded = set(case["excluded_memory_ids"])
            self.assertTrue(relevant <= set(records))
            self.assertTrue(excluded <= set(records))
            self.assertTrue(relevant.isdisjoint(excluded))

    def test_memory_record_metadata_is_fact_shaped_and_revisioned(self) -> None:
        for pool in self.suite["memory_pools"].values():
            repository = self.suite["repositories"][pool["repository_id"]]
            for record in pool["records"]:
                metadata = record["metadata"]
                self.assertEqual(set(metadata), REQUIRED_METADATA_FIELDS)
                self.assertFalse(FORBIDDEN_METADATA_KEYS & set(metadata))
                self.assertIsInstance(metadata["entities"], list)
                self.assertIsInstance(metadata["concept_keys"], list)
                self.assertIn(metadata["repository_component"], repository["files"])
                if record["scope"] == "repository":
                    self.assertEqual(record["repository_revision"], metadata["revision"])

    def test_verification_points_to_fixture_facts_without_answer_text(self) -> None:
        for case in self.suite["cases"]:
            repository = self.suite["repositories"][case["repository_id"]]
            verification = case["verification"]
            component = verification["component"]
            test_file = verification["test_file"]
            self.assertIn(component, repository["files"])
            self.assertIn(test_file, repository["files"])
            self.assertIn(verification["symbol"], repository["files"][component])
            self.assertNotIn("expected_answer", verification)

    def test_v2_twenty_case_subset_is_retained_by_immutable_reference(self) -> None:
        subset = self.suite["v2_development_subset"]
        manifest_cases = self.v2_manifest["cases"]
        self.assertEqual(subset["case_count"], 20)
        self.assertFalse(subset["body_included"])
        self.assertTrue(subset["reference_only"])
        self.assertEqual(subset["source_suite_sha256"], self.v2_manifest["suite_sha256"])
        refs = subset["case_refs"]
        self.assertEqual(len(refs), 20)
        manifest_by_id = {item["case_id"]: item for item in manifest_cases}
        for reference in refs:
            source = manifest_by_id[reference["case_id"]]
            for field in ("repository_id", "memory_pool_id", "category", "expected_oracle_type"):
                self.assertEqual(reference[field], source[field])
        self.assertTrue(set(item["case_id"] for item in refs).isdisjoint(
            case["case_id"] for case in self.suite["cases"]
        ))


if __name__ == "__main__":
    unittest.main()
