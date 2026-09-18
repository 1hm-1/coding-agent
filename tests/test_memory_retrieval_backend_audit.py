from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from examples import memory_retrieval_backend_audit as AUDIT


ROOT = Path(__file__).resolve().parents[1]


class MemoryRetrievalBackendAuditTest(unittest.TestCase):
    def test_fixed_matrix_is_independently_reproduced(self) -> None:
        raw, summary = AUDIT.audit_fixed_bundle(repeats=3)

        self.assertEqual(raw["baseline_commit"], "cdea7b9")
        self.assertTrue(raw["bundle_consistency"]["retrieval_matches_f1d03cf"])
        self.assertTrue(raw["bundle_consistency"]["metric_arithmetic_recomputed"])
        self.assertEqual(
            [item["backend"] for item in raw["candidate_audits"]],
            ["lexical-control", "bm25-content", "structured-bm25", "embedding-hybrid"],
        )
        for candidate in raw["candidate_audits"]:
            self.assertTrue(candidate["repeatability"]["all_match"], candidate["backend"])
            self.assertTrue(
                all(item["match"] for item in candidate["selection_matches"]),
                candidate["backend"],
            )
            self.assertEqual(
                candidate["case_selection_digest"],
                candidate["committed_case_selection_digest"],
            )
        self.assertEqual(
            raw["pareto"]["score_source"],
            "committed raw-result.json score_components only",
        )
        self.assertFalse(raw["pareto"]["backend_reinvocation"])
        self.assertTrue(summary["bundle_consistency"]["selected_ids_match"])

        with tempfile.TemporaryDirectory() as temporary:
            manifest = AUDIT.write_bundle(
                Path(temporary) / "audit",
                raw=raw,
                summary=summary,
                source_manifest_path=AUDIT.DEFAULT_SOURCE_MANIFEST,
                source_raw_path=AUDIT.DEFAULT_SOURCE_RAW,
                source_summary_path=AUDIT.DEFAULT_SOURCE_SUMMARY,
            )
            output = Path(temporary) / "audit"
            self.assertEqual(
                manifest["raw_result_sha256"], AUDIT.sha256_file(output / "raw-result.json")
            )
            self.assertEqual(
                manifest["redacted_summary_sha256"],
                AUDIT.sha256_file(output / "redacted-summary.json"),
            )
            self.assertEqual(manifest["pareto_sha256"], AUDIT.sha256_file(output / "pareto.json"))

    def test_pareto_sweep_has_fixed_threshold_and_top_k_axes(self) -> None:
        suite = AUDIT.spike.load_suite(AUDIT.DEFAULT_SUITE)
        raw = AUDIT._load_json(AUDIT.DEFAULT_SOURCE_RAW)
        pareto = AUDIT.pareto_from_raw(suite, raw)

        self.assertTrue(pareto["labels_used_only_after_retrieval"])
        for name in ("lexical-control", "bm25-content", "structured-bm25"):
            report = pareto["candidate_reports"][name]
            self.assertGreaterEqual(len(report["score_thresholds"]), 1)
            self.assertEqual(report["top_k_values"], [1, 2, 3, 4, 5])
            self.assertGreater(report["all_operating_point_count"], 0)
            self.assertIsInstance(report["non_dominated_operating_points"], list)
        self.assertEqual(
            pareto["candidate_reports"]["embedding-hybrid"]["availability"],
            "unavailable",
        )
        self.assertIn("max_recall_when_irrelevant_injection_lte_0.15", pareto["global_bounds"])
        self.assertIn("min_irrelevant_injection_when_recall_gte_0.85", pareto["global_bounds"])

    def test_production_retrieval_authority_hash_is_fixed(self) -> None:
        self.assertEqual(
            AUDIT.sha256_file(ROOT / "src/coding_agent/memory/retrieval.py"),
            AUDIT.F1D03CF_RETRIEVAL_SHA256,
        )


if __name__ == "__main__":
    unittest.main()
