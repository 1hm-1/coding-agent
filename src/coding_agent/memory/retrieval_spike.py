"""Run the S3.7 offline retrieval-backend development comparison.

The evaluator projects each development case into a label-free ``CandidateRequest``
before invoking a backend.  Ground-truth fields are read only after retrieval for
metric calculation and are never present in the backend-visible object.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import argparse
import hashlib
import json
from pathlib import Path
import statistics
from typing import Any

from coding_agent.memory.domain import MemoryKind, MemoryQuery, MemoryScope, stable_json
from coding_agent.memory.policy import MemoryWriteContext
from coding_agent.memory.retrieval_backends import (
    BM25ContentBackend,
    CandidateBackend,
    CandidateRequest,
    CandidateResult,
    EmbeddingHybridBackend,
    LexicalControlBackend,
    StructuredBM25Backend,
    TrustedRecordMetadata,
    derive_query_metadata,
    deterministic_result_digest,
    eligible_candidate_records,
    validate_record_metadata,
)
from coding_agent.memory.service import MemoryService
from coding_agent.memory.sqlite import SQLiteMemoryStore


SCHEMA_VERSION = 1
DEFAULT_NOW = "2026-09-18T00:00:00+00:00"
DEFAULT_REPEATS = 3
FORBIDDEN_BACKEND_LABEL_KEYS = frozenset(
    {
        "relevant_memory_ids",
        "expected_relevant_memory_ids",
        "excluded_memory_ids",
        "category",
        "oracle",
        "expected_selection",
        "ground_truth_relevance",
        "metadata_ground_truth",
        "verification",
    }
)


class SpikeEvaluationError(RuntimeError):
    pass


@dataclass(frozen=True)
class PoolState:
    store: SQLiteMemoryStore
    metadata_by_id: Mapping[str, TrustedRecordMetadata]


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: str | Path) -> str:
    return sha256_bytes(Path(path).read_bytes())


def _mapping(value: Any, description: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise SpikeEvaluationError(f"{description} must be an object")
    return value


def _text(value: Any, description: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise SpikeEvaluationError(f"{description} must be non-empty text")
    return value


def _string_list(value: Any, description: str) -> tuple[str, ...]:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise SpikeEvaluationError(f"{description} must be an array of strings")
    return tuple(value)


def load_suite(path: str | Path) -> dict[str, Any]:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise SpikeEvaluationError("development suite must be an object")
    if raw.get("data_classification") != "development_data":
        raise SpikeEvaluationError("candidate comparison accepts development data only")
    if raw.get("holdout_status") != "development_only_no_holdout_v3":
        raise SpikeEvaluationError("suite must not claim Holdout v3 status")
    if not isinstance(raw.get("cases"), list) or not isinstance(raw.get("memory_pools"), dict):
        raise SpikeEvaluationError("development suite has no cases or memory pools")
    return raw


def _scope_context(scope: MemoryScope, scope_id: str) -> MemoryWriteContext:
    return MemoryWriteContext(
        actor_id="s3-7-spike",
        session_id=scope_id if scope is MemoryScope.SESSION else None,
        repository_id=scope_id if scope is MemoryScope.REPOSITORY else None,
        user_id=scope_id if scope is MemoryScope.USER else None,
        allowed_scopes=frozenset({scope}),
    )


def build_pool(pool_raw: Mapping[str, Any], *, now: str = DEFAULT_NOW) -> PoolState:
    records = pool_raw.get("records")
    if not isinstance(records, list):
        raise SpikeEvaluationError("pool records must be an array")
    store = SQLiteMemoryStore(":memory:", clock=lambda: now)
    service = MemoryService(store, clock=lambda: now, provenance_validator=lambda _record: True)
    metadata_by_id: dict[str, TrustedRecordMetadata] = {}
    for index, value in enumerate(records, start=1):
        raw = _mapping(value, "memory record")
        memory_id = _text(raw.get("memory_id"), "memory_id")
        metadata_by_id[memory_id] = validate_record_metadata(
            _mapping(raw.get("metadata"), f"memory {memory_id} metadata")
        )
        status = _text(raw.get("status", "active"), f"memory {memory_id} status")
        if status == "policy_rejected":
            # It is a policy control, not a record visible to any retriever.
            continue
        scope = MemoryScope(_text(raw.get("scope"), f"memory {memory_id} scope"))
        scope_id = _text(raw.get("scope_id"), f"memory {memory_id} scope_id")
        repository_revision = raw.get("repository_revision")
        if repository_revision is not None:
            repository_revision = _text(
                repository_revision, f"memory {memory_id} repository_revision"
            )
        proposed = service.propose(
            context=_scope_context(scope, scope_id),
            scope=scope,
            kind=MemoryKind(_text(raw.get("kind", "semantic"), f"memory {memory_id} kind")),
            content=_text(raw.get("content"), f"memory {memory_id} content"),
            source_run_id=f"s3-7-seed-{index}-{memory_id}",
            source_agent_id="runtime",
            source_event_refs=(f"s3-7-event-{index}-{memory_id}",),
            confidence=float(raw.get("confidence", 0.9)),
            repository_revision=repository_revision,
            expires_at=(str(raw["expires_at"]) if raw.get("expires_at") is not None else None),
            memory_id=memory_id,
        )
        active = service.activate(
            memory_id,
            context=_scope_context(scope, scope_id),
            expected_version=proposed.version,
        )
        if status == "stale":
            service.mark_stale(
                memory_id,
                context=_scope_context(scope, scope_id),
                expected_version=active.version,
                reason="S3.7 development control",
            )
        elif status == "deleted":
            service.delete(
                memory_id,
                context=_scope_context(scope, scope_id),
                expected_version=active.version,
            )
        elif status != "active":
            raise SpikeEvaluationError(f"unsupported memory status: {status}")
    return PoolState(store=store, metadata_by_id=metadata_by_id)


def _scope_value(scope: Mapping[str, Any], *names: str) -> str | None:
    for name in names:
        value = scope.get(name)
        if value is not None:
            return _text(value, f"query scope {name}")
    return None


def project_candidate_request(
    case: Mapping[str, Any],
    *,
    repository: Mapping[str, Any],
    pool: PoolState,
    now: str = DEFAULT_NOW,
) -> CandidateRequest:
    """Project production inputs before any ground-truth label is read."""

    query_scope = _mapping(case.get("query_scope", {}), "case query_scope")
    query = MemoryQuery(
        text=_text(case.get("query", case.get("task")), "case query"),
        session_id=_scope_value(query_scope, "session_id", "session"),
        repository_id=_scope_value(query_scope, "repository_id", "repository"),
        user_id=_scope_value(query_scope, "user_id", "user"),
        repository_revision=_scope_value(
            query_scope, "repository_revision", "revision"
        ),
        top_k=int(case.get("top_k", 5)),
        token_budget=int(case.get("token_budget", 512)),
    )
    files = _mapping(repository.get("files", {}), "repository files")
    query_metadata = derive_query_metadata(
        query.text,
        repository_components=tuple(str(name) for name in files),
    )
    records = eligible_candidate_records(
        pool.store,
        query,
        pool.metadata_by_id,
        now=now,
    )
    request = CandidateRequest(query=query, query_metadata=query_metadata, records=records)
    serialized = stable_json(request.to_dict())
    if any(f'"{key}"' in serialized for key in FORBIDDEN_BACKEND_LABEL_KEYS):
        raise SpikeEvaluationError("backend request contains an evaluation-only label")
    return request


def invoke_backend(
    backend: CandidateBackend,
    case: Mapping[str, Any],
    *,
    repository: Mapping[str, Any],
    pool: PoolState,
    now: str = DEFAULT_NOW,
) -> tuple[CandidateRequest, CandidateResult]:
    request = project_candidate_request(case, repository=repository, pool=pool, now=now)
    return request, backend.retrieve(request)


def _percentile(values: Sequence[float], fraction: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, math_ceil(len(ordered) * fraction) - 1))
    return ordered[index]


def math_ceil(value: float) -> int:
    # Kept local so report arithmetic has no dependency beyond the standard library.
    integer = int(value)
    return integer if value == integer else integer + 1


def _summarize_cases(cases: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    relevant_total = sum(len(case["expected_relevant_memory_ids"]) for case in cases)
    recalled_total = sum(
        len(set(case["expected_relevant_memory_ids"]) & set(case["selected_ids"]))
        for case in cases
    )
    selected_total = sum(len(case["selected_ids"]) for case in cases)
    relevant_selected = recalled_total
    irrelevant_selected = selected_total - relevant_selected
    latencies = [float(case["funnel"]["latency_ms"]) for case in cases]
    zero_cases = [case for case in cases if case["category"] == "zero_overlap_paraphrase"]
    zero_relevant = sum(len(case["expected_relevant_memory_ids"]) for case in zero_cases)
    zero_recalled = sum(
        len(set(case["expected_relevant_memory_ids"]) & set(case["selected_ids"]))
        for case in zero_cases
    )
    hard_cases = [case for case in cases if case["category"] == "same_topic_hard_negative"]
    return {
        "case_count": len(cases),
        "relevant_recall": recalled_total / relevant_total if relevant_total else 1.0,
        "precision": relevant_selected / selected_total if selected_total else 1.0,
        "irrelevant_injection": irrelevant_selected / selected_total if selected_total else 0.0,
        "zero_overlap": {
            "case_count": len(zero_cases),
            "relevant_recall": zero_recalled / zero_relevant if zero_relevant else 1.0,
        },
        "hard_negative": {
            "case_count": len(hard_cases),
            "cases_with_selection": sum(bool(case["selected_ids"]) for case in hard_cases),
            "selected_count": sum(len(case["selected_ids"]) for case in hard_cases),
        },
        "retrieval_tokens": {
            "total": sum(int(case["funnel"]["selected_token_cost"]) for case in cases),
            "mean": (
                sum(int(case["funnel"]["selected_token_cost"]) for case in cases)
                / len(cases)
                if cases
                else 0.0
            ),
        },
        "latency_ms": {
            "mean": statistics.fmean(latencies) if latencies else 0.0,
            "p50": _percentile(latencies, 0.50),
            "p95": _percentile(latencies, 0.95),
        },
    }


def evaluate_suite(
    suite: Mapping[str, Any],
    *,
    suite_sha256: str,
    repeats: int = DEFAULT_REPEATS,
    now: str = DEFAULT_NOW,
) -> tuple[dict[str, Any], dict[str, Any]]:
    if repeats < 3:
        raise SpikeEvaluationError("S3.7 repeatability requires at least three runs")
    pools_raw = _mapping(suite.get("memory_pools"), "memory pools")
    repositories = _mapping(suite.get("repositories"), "repositories")
    cases_raw = suite.get("cases")
    if not isinstance(cases_raw, list):
        raise SpikeEvaluationError("cases must be an array")
    pools = {
        str(pool_id): build_pool(_mapping(value, f"pool {pool_id}"), now=now)
        for pool_id, value in pools_raw.items()
    }
    backends: tuple[CandidateBackend, ...] = (
        LexicalControlBackend(now=now),
        BM25ContentBackend(),
        StructuredBM25Backend(),
        EmbeddingHybridBackend(adapter=None),
    )
    raw_candidates: list[dict[str, Any]] = []
    summary_candidates: list[dict[str, Any]] = []
    try:
        for backend in backends:
            repeated_results: list[list[CandidateResult]] = []
            first_cases: list[dict[str, Any]] = []
            for repeat in range(repeats):
                run_results: list[CandidateResult] = []
                run_cases: list[dict[str, Any]] = []
                for case_value in cases_raw:
                    case = _mapping(case_value, "case")
                    case_id = _text(case.get("case_id"), "case_id")
                    pool_id = _text(case.get("memory_pool_id"), f"case {case_id} pool")
                    repository_id = _text(
                        case.get("repository_id"), f"case {case_id} repository"
                    )
                    if pool_id not in pools or repository_id not in repositories:
                        raise SpikeEvaluationError(f"case {case_id} references unknown inputs")
                    _request, result = invoke_backend(
                        backend,
                        case,
                        repository=_mapping(repositories[repository_id], "repository"),
                        pool=pools[pool_id],
                        now=now,
                    )
                    # Labels are intentionally read only after backend invocation.
                    expected = _string_list(
                        case.get("expected_relevant_memory_ids", []),
                        f"case {case_id} expected relevance",
                    )
                    run_results.append(result)
                    run_cases.append(
                        {
                            "case_id": case_id,
                            "category": _text(case.get("category"), f"case {case_id} category"),
                            "expected_relevant_memory_ids": list(expected),
                            "selected_ids": list(result.selected_ids),
                            "relevant_hits": sorted(set(expected) & set(result.selected_ids)),
                            "funnel": result.to_dict(),
                        }
                    )
                repeated_results.append(run_results)
                if repeat == 0:
                    first_cases = run_cases
            digests = [deterministic_result_digest(results) for results in repeated_results]
            repeatable = len(set(digests)) == 1
            availability = (
                "available"
                if all(result.availability == "available" for result in repeated_results[0])
                else "unavailable"
            )
            raw_candidate = {
                "backend": backend.name,
                "availability": availability,
                "parameters": dict(backend.parameters),
                "repeatability": {
                    "runs": repeats,
                    "deterministic": repeatable,
                    "digests": digests,
                },
                "cases": first_cases,
            }
            raw_candidates.append(raw_candidate)
            metrics = _summarize_cases(first_cases) if availability == "available" else None
            summary_candidates.append(
                {
                    "backend": backend.name,
                    "availability": availability,
                    "parameters": dict(backend.parameters),
                    "repeatability": raw_candidate["repeatability"],
                    "metrics": metrics,
                    "unavailable_reason": (
                        first_cases[0]["funnel"]["unavailable_reason"]
                        if first_cases and availability == "unavailable"
                        else None
                    ),
                    "case_selection_digest": sha256_bytes(
                        stable_json(
                            [
                                {
                                    "case_id": case["case_id"],
                                    "selected_ids": case["selected_ids"],
                                }
                                for case in first_cases
                            ]
                        ).encode("utf-8")
                    ),
                }
            )
    finally:
        for pool in pools.values():
            pool.store.close()
    raw = {
        "schema_version": SCHEMA_VERSION,
        "report_kind": "memory_retrieval_backend_spike_raw",
        "suite_id": suite.get("suite_id"),
        "suite_sha256": suite_sha256,
        "data_classification": suite.get("data_classification"),
        "production_retriever_changed": False,
        "repeats": repeats,
        "candidates": raw_candidates,
    }
    summary = {
        "schema_version": SCHEMA_VERSION,
        "report_kind": "memory_retrieval_backend_spike_summary",
        "suite_id": suite.get("suite_id"),
        "suite_sha256": suite_sha256,
        "data_classification": suite.get("data_classification"),
        "production_retriever_changed": False,
        "candidates": summary_candidates,
    }
    return raw, summary


def write_bundle(
    output: str | Path,
    *,
    raw: Mapping[str, Any],
    summary: Mapping[str, Any],
    suite_path: str | Path,
    retrieval_path: str | Path,
) -> dict[str, Any]:
    output_path = Path(output)
    if output_path.exists() and any(output_path.iterdir()):
        raise SpikeEvaluationError("output directory must not already contain files")
    output_path.mkdir(parents=True, exist_ok=True)
    raw_path = output_path / "raw-result.json"
    summary_path = output_path / "redacted-summary.json"
    raw_path.write_text(json.dumps(raw, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    summary_path.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "report_kind": "memory_retrieval_backend_spike_manifest",
        "suite_sha256": sha256_file(suite_path),
        "production_retrieval_sha256": sha256_file(retrieval_path),
        "production_retriever_changed": False,
        "raw_result_sha256": sha256_file(raw_path),
        "redacted_summary_sha256": sha256_file(summary_path),
        "candidate_names": [item["backend"] for item in summary["candidates"]],
        "label_projection_forbidden_keys": sorted(FORBIDDEN_BACKEND_LABEL_KEYS),
        "default_application_memory_enabled": False,
        "holdout_v3_created": False,
    }
    (output_path / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return manifest


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--suite", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--retrieval", default="src/coding_agent/memory/retrieval.py")
    parser.add_argument("--repeats", type=int, default=DEFAULT_REPEATS)
    args = parser.parse_args(argv)
    suite = load_suite(args.suite)
    suite_hash = sha256_file(args.suite)
    raw, summary = evaluate_suite(suite, suite_sha256=suite_hash, repeats=args.repeats)
    write_bundle(
        args.output,
        raw=raw,
        summary=summary,
        suite_path=args.suite,
        retrieval_path=args.retrieval,
    )
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
