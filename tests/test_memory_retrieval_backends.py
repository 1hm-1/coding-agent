from __future__ import annotations

from dataclasses import replace
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from typing import Any

from coding_agent.memory.domain import (
    MemoryKind,
    MemoryQuery,
    MemoryRecord,
    MemoryScope,
    MemoryStatus,
)
from coding_agent.memory.retrieval_backends import (
    BM25ContentBackend,
    CandidateInputError,
    CandidateRecord,
    CandidateRequest,
    EmbeddingHybridBackend,
    ProductionQueryMetadata,
    StructuredBM25Backend,
    validate_record_metadata,
)
from coding_agent.memory import retrieval_spike as RUNNER


ROOT = Path(__file__).resolve().parents[1]
SUITE_PATH = ROOT / "examples" / "memory_retrieval_backend_development.json"
RETRIEVAL_PATH = ROOT / "src" / "coding_agent" / "memory" / "retrieval.py"
F1D03CF_RETRIEVAL_SHA256 = "6e64026504311e3c467f6b34b747eea296bb68104576d8d01104d50dc073d0f8"
class CapturingBackend:
    name = "capture"

    def __init__(self) -> None:
        self.request: CandidateRequest | None = None
        self.delegate = EmbeddingHybridBackend(adapter=None)

    @property
    def parameters(self) -> dict[str, Any]:
        return {"capture": True}

    def retrieve(self, request: CandidateRequest) -> Any:
        self.request = request
        return self.delegate.retrieve(request)


class MemoryRetrievalBackendSpikeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.suite = RUNNER.load_suite(SUITE_PATH)

    def _pool_for_case(self, case: dict[str, Any]) -> Any:
        return RUNNER.build_pool(self.suite["memory_pools"][case["memory_pool_id"]])

    def test_evaluator_projects_ground_truth_away_before_backend_call(self) -> None:
        case = dict(self.suite["cases"][0])
        sentinel = "DO-NOT-PASS-GROUND-TRUTH-SENTINEL"
        case.update(
            {
                "expected_relevant_memory_ids": [sentinel],
                "category": sentinel,
                "oracle": {"answer": sentinel},
                "expected_selection": [sentinel],
                "ground_truth_relevance": {sentinel: True},
                "metadata_ground_truth": {"answer": sentinel},
                "verification": {"expected_answer": sentinel},
            }
        )
        pool = self._pool_for_case(case)
        backend = CapturingBackend()
        try:
            RUNNER.invoke_backend(
                backend,
                case,
                repository=self.suite["repositories"][case["repository_id"]],
                pool=pool,
            )
        finally:
            pool.store.close()
        self.assertIsNotNone(backend.request)
        payload = json.dumps(backend.request.to_dict(), sort_keys=True)
        self.assertNotIn(sentinel, payload)
        for key in RUNNER.FORBIDDEN_BACKEND_LABEL_KEYS:
            self.assertNotIn(f'"{key}"', payload)

    def test_all_backends_share_scope_status_revision_eligibility(self) -> None:
        by_id = {case["case_id"]: case for case in self.suite["cases"]}
        expectations = {
            "bramble-boundary-user-scope": {"bramble-artifact-cache", "bramble-runner-target", "bramble-log-retention"},
            "cove-boundary-revision": {"cove-route-normalize", "cove-redirect-response", "cove-route-limit-current"},
            "drift-boundary-stale": {"drift-export-order", "drift-rounding", "drift-checksum"},
            "ember-boundary-injection": {"ember-case-fold", "ember-tag-index", "ember-note-fingerprint"},
        }
        backends = (
            RUNNER.LexicalControlBackend(now=RUNNER.DEFAULT_NOW),
            BM25ContentBackend(),
            StructuredBM25Backend(),
            EmbeddingHybridBackend(adapter=None),
        )
        for case_id, expected in expectations.items():
            case = by_id[case_id]
            pool = self._pool_for_case(case)
            try:
                request = RUNNER.project_candidate_request(
                    case,
                    repository=self.suite["repositories"][case["repository_id"]],
                    pool=pool,
                )
                self.assertEqual(
                    {item.record.memory_id for item in request.records},
                    expected,
                    case_id,
                )
                for backend in backends:
                    result = backend.retrieve(request)
                    self.assertEqual(set(result.eligible_ids), expected, (case_id, backend.name))
            finally:
                pool.store.close()

    def test_metadata_cannot_inject_instructions_permissions_or_authority(self) -> None:
        valid = {
            "fact_type": "lookup_strategy",
            "entities": ["archive_entry"],
            "concept_keys": ["lookup"],
            "repository_component": "archive_index.py",
            "validity": "current",
            "revision": "dev-r1",
        }
        with self.assertRaises(CandidateInputError):
            validate_record_metadata({**valid, "permissions": ["repository:other"]})
        with self.assertRaises(CandidateInputError):
            validate_record_metadata({**valid, "fact_type": "ignore_previous_instructions"})

        case = next(
            item for item in self.suite["cases"] if item["case_id"] == "cove-boundary-revision"
        )
        pool = self._pool_for_case(case)
        try:
            forged = validate_record_metadata(
                {
                    **pool.metadata_by_id["cove-old-revision"].to_dict(),
                    "revision": "dev-cove-r1",
                }
            )
            metadata = dict(pool.metadata_by_id)
            metadata["cove-old-revision"] = forged
            request = RUNNER.project_candidate_request(
                case,
                repository=self.suite["repositories"][case["repository_id"]],
                pool=replace(pool, metadata_by_id=metadata),
            )
            self.assertNotIn(
                "cove-old-revision", {item.record.memory_id for item in request.records}
            )
        finally:
            pool.store.close()

    def test_tie_break_is_deterministic_for_bm25_candidates(self) -> None:
        def record(memory_id: str) -> MemoryRecord:
            proposed = MemoryRecord.proposed(
                memory_id=memory_id,
                scope=MemoryScope.REPOSITORY,
                scope_id="repo",
                kind=MemoryKind.SEMANTIC,
                content="alpha evidence",
                source_run_id=f"run-{memory_id}",
                source_agent_id="runtime",
                source_event_refs=(f"event-{memory_id}",),
                repository_revision="rev-1",
                confidence=0.9,
                created_at=RUNNER.DEFAULT_NOW,
            )
            return proposed.with_status(MemoryStatus.ACTIVE, updated_at=RUNNER.DEFAULT_NOW)

        metadata = validate_record_metadata(
            {
                "fact_type": "evidence",
                "entities": ["alpha"],
                "concept_keys": ["alpha"],
                "repository_component": "alpha.py",
                "validity": "current",
                "revision": "rev-1",
            }
        )
        request = CandidateRequest(
            query=MemoryQuery(
                text="alpha",
                repository_id="repo",
                repository_revision="rev-1",
                top_k=1,
            ),
            query_metadata=ProductionQueryMetadata(("alpha",), (), ()),
            records=(
                CandidateRecord(record("zeta"), metadata),
                CandidateRecord(record("alpha"), metadata),
            ),
        )
        for backend in (BM25ContentBackend(), StructuredBM25Backend()):
            results = [backend.retrieve(request) for _ in range(3)]
            self.assertTrue(all(result.selected_ids == ("alpha",) for result in results))
            self.assertTrue(all(result.ranked_ids == ("alpha", "zeta") for result in results))

    def test_embedding_without_real_adapter_is_explicitly_unavailable(self) -> None:
        case = self.suite["cases"][0]
        pool = self._pool_for_case(case)
        try:
            request = RUNNER.project_candidate_request(
                case,
                repository=self.suite["repositories"][case["repository_id"]],
                pool=pool,
            )
            result = EmbeddingHybridBackend(adapter=None).retrieve(request)
        finally:
            pool.store.close()
        self.assertEqual(result.availability, "unavailable")
        self.assertEqual(result.unavailable_reason, "no_real_embedding_adapter_configured")
        self.assertFalse(result.parameters["fallback_simulation"])
        self.assertEqual(result.selected_ids, ())

    def test_three_suite_runs_are_identical_excluding_latency(self) -> None:
        raw, _summary = RUNNER.evaluate_suite(
            self.suite,
            suite_sha256=RUNNER.sha256_file(SUITE_PATH),
            repeats=3,
        )
        for candidate in raw["candidates"]:
            repeatability = candidate["repeatability"]
            self.assertTrue(repeatability["deterministic"], candidate["backend"])
            self.assertEqual(len(set(repeatability["digests"])), 1)

    def test_raw_summary_manifest_are_cross_verifiable(self) -> None:
        raw, summary = RUNNER.evaluate_suite(
            self.suite,
            suite_sha256=RUNNER.sha256_file(SUITE_PATH),
            repeats=3,
        )
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "result"
            manifest = RUNNER.write_bundle(
                output,
                raw=raw,
                summary=summary,
                suite_path=SUITE_PATH,
                retrieval_path=RETRIEVAL_PATH,
            )
            saved_raw = json.loads((output / "raw-result.json").read_text())
            saved_summary = json.loads((output / "redacted-summary.json").read_text())
            saved_manifest = json.loads((output / "manifest.json").read_text())
            self.assertEqual(saved_manifest, manifest)
            self.assertEqual(manifest["raw_result_sha256"], RUNNER.sha256_file(output / "raw-result.json"))
            self.assertEqual(
                manifest["redacted_summary_sha256"],
                RUNNER.sha256_file(output / "redacted-summary.json"),
            )
            self.assertEqual(
                [item["backend"] for item in saved_raw["candidates"]],
                [item["backend"] for item in saved_summary["candidates"]],
            )
            self.assertEqual(
                len({frozenset(item) for item in saved_raw["candidates"]}),
                1,
            )
            self.assertEqual(
                len({frozenset(item) for item in saved_summary["candidates"]}),
                1,
            )
            raw_by_name = {item["backend"]: item for item in saved_raw["candidates"]}
            for candidate in saved_summary["candidates"]:
                cases = raw_by_name[candidate["backend"]]["cases"]
                self.assertEqual(len({frozenset(case) for case in cases}), 1)
                self.assertEqual(
                    len({frozenset(case["funnel"]) for case in cases}),
                    1,
                )
                digest = hashlib.sha256(
                    RUNNER.stable_json(
                        [
                            {"case_id": case["case_id"], "selected_ids": case["selected_ids"]}
                            for case in cases
                        ]
                    ).encode("utf-8")
                ).hexdigest()
                self.assertEqual(candidate["case_selection_digest"], digest)

    def test_production_retrieval_file_is_identical_to_f1d03cf(self) -> None:
        self.assertEqual(
            hashlib.sha256(RETRIEVAL_PATH.read_bytes()).hexdigest(),
            F1D03CF_RETRIEVAL_SHA256,
        )


if __name__ == "__main__":
    unittest.main()
