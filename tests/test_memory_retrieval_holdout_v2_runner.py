from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from examples.memory_retrieval_holdout_v2_runner import (
    load_suite,
    run_suite,
    sha256_file,
    write_result_bundle,
)


ROOT = Path(__file__).resolve().parents[1]
DEVELOPMENT_SUITE = ROOT / "examples" / "memory_retrieval_holdout_v2_development.json"


class MemoryRetrievalHoldoutV2RunnerTest(unittest.TestCase):
    def test_runner_uses_shared_pool_and_existing_renderer_on_synthetic_suite(self) -> None:
        suite_sha = sha256_file(DEVELOPMENT_SUITE)
        report = run_suite(
            DEVELOPMENT_SUITE,
            suite_sha256=suite_sha,
            expected_sha256=suite_sha,
            algorithm_commit="synthetic-algorithm",
            runner_commit="synthetic-runner",
            execution_time="2026-09-17T00:00:00+00:00",
        )

        self.assertEqual(report["main_case_count"], 2)
        self.assertEqual(report["valid_case_count"], 2)
        cases = {item["case_id"]: item for item in report["cases"]}
        self.assertEqual(cases["dev-related"]["selected_memory_ids"], ["dev-checksum-memory"])
        self.assertEqual(cases["dev-related"]["scope_revision_stale_deleted_leakage"], 0)
        self.assertEqual(cases["dev-related"]["irrelevant_count"], 0)
        self.assertEqual(cases["dev-related"]["no_memory_behavior_delta"], None)
        self.assertEqual(cases["dev-no-memory"]["selected_memory_ids"], [])
        self.assertEqual(cases["dev-no-memory"]["no_memory_behavior_delta"], 0)
        self.assertEqual(report["metrics"]["manifest_token_mismatch_count"], 0)
        self.assertFalse(report["provider_evaluation"])
        self.assertFalse(report["safety"]["answer_key_used"])

    def test_result_bundle_hashes_raw_and_redacted_outputs(self) -> None:
        suite_sha = sha256_file(DEVELOPMENT_SUITE)
        report = run_suite(
            DEVELOPMENT_SUITE,
            suite_sha256=suite_sha,
            expected_sha256=suite_sha,
            algorithm_commit="synthetic-algorithm",
            runner_commit="synthetic-runner",
            execution_time="2026-09-17T00:00:00+00:00",
        )
        with tempfile.TemporaryDirectory() as temporary:
            hashes = write_result_bundle(report, Path(temporary) / "result")
            raw = Path(temporary) / "result" / "raw-result.json"
            summary = Path(temporary) / "result" / "redacted-summary.json"
            metadata = json.loads(
                (Path(temporary) / "result" / "execution-metadata.json").read_text(
                    encoding="utf-8"
                )
            )
            import hashlib

            self.assertEqual(hashes["raw_result_sha256"], hashlib.sha256(raw.read_bytes()).hexdigest())
            self.assertEqual(
                hashes["redacted_summary_sha256"], hashlib.sha256(summary.read_bytes()).hexdigest()
            )
            self.assertEqual(metadata["raw_result_sha256"], hashes["raw_result_sha256"])
            self.assertEqual(metadata["redacted_summary_sha256"], hashes["redacted_summary_sha256"])

    def test_development_loader_does_not_require_holdout_body(self) -> None:
        suite = load_suite(DEVELOPMENT_SUITE)
        self.assertEqual(suite["_validated_case_count"], 2)
        self.assertEqual(suite["_validated_compatibility_case_count"], 0)


if __name__ == "__main__":
    unittest.main()
