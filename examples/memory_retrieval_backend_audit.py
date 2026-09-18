"""Independently replay the committed retrieval-backend spike and audit trade-offs.

This module is deliberately an audit layer rather than another candidate evaluator.
It reconstructs the fixed candidate matrix from the committed parameter report,
checks the committed raw/summary/manifest bundle, and performs Pareto analysis from
the score components already present in the raw result.  Labels are read only after
candidate requests have been executed and are never included in a backend request.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
import argparse
import hashlib
import json
import math
from pathlib import Path
import statistics
from typing import Any

from coding_agent.memory import retrieval_spike as spike
from coding_agent.memory.domain import MemoryScope, stable_json
from coding_agent.memory.retrieval_backends import (
    BM25ContentBackend,
    CandidateBackend,
    CandidateResult,
    EmbeddingHybridBackend,
    LexicalControlBackend,
    StructuredBM25Backend,
)


BASELINE_COMMIT = "cdea7b9"
F1D03CF_RETRIEVAL_SHA256 = "6e64026504311e3c467f6b34b747eea296bb68104576d8d01104d50dc073d0f8"
DEFAULT_SUITE = Path("examples/memory_retrieval_backend_development.json")
DEFAULT_SOURCE_MANIFEST = Path(
    "docs/evidence/s3-7-memory-retrieval-backends-2026-09-18/manifest.json"
)
DEFAULT_SOURCE_RAW = Path(
    "docs/evidence/s3-7-memory-retrieval-backends-2026-09-18/raw-result.json"
)
DEFAULT_SOURCE_SUMMARY = Path(
    "docs/evidence/s3-7-memory-retrieval-backends-2026-09-18/redacted-summary.json"
)
DEFAULT_RETRIEVAL = Path("src/coding_agent/memory/retrieval.py")
DEFAULT_REPEATS = 3
SCOPE_PRIORITY = {
    MemoryScope.SESSION.value: 3,
    MemoryScope.REPOSITORY.value: 2,
    MemoryScope.USER.value: 1,
}


class AuditError(RuntimeError):
    """The fixed candidate evidence cannot be independently reproduced."""


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: str | Path) -> str:
    return sha256_bytes(Path(path).read_bytes())


def _load_json(path: str | Path) -> dict[str, Any]:
    value = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise AuditError(f"{path} must contain a JSON object")
    return value


def _mapping(value: Any, description: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise AuditError(f"{description} must be an object")
    return value


def _float_close(left: Any, right: Any) -> bool:
    return math.isclose(float(left), float(right), rel_tol=1e-12, abs_tol=1e-12)


def _sha256_json(value: Any) -> str:
    return sha256_bytes((stable_json(value) + "\n").encode("utf-8"))


def _percentile(values: Sequence[float], fraction: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, math.ceil(len(ordered) * fraction) - 1))
    return ordered[index]


def _metrics(cases: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    relevant_total = sum(len(case["expected_relevant_memory_ids"]) for case in cases)
    relevant_hits = sum(
        len(set(case["expected_relevant_memory_ids"]) & set(case["selected_ids"]))
        for case in cases
    )
    selected_total = sum(len(case["selected_ids"]) for case in cases)
    irrelevant_total = selected_total - relevant_hits
    latencies = [float(case["funnel"]["latency_ms"]) for case in cases]
    token_total = sum(int(case["funnel"]["selected_token_cost"]) for case in cases)
    zero_cases = [case for case in cases if case["category"] == "zero_overlap_paraphrase"]
    zero_relevant = sum(len(case["expected_relevant_memory_ids"]) for case in zero_cases)
    zero_hits = sum(
        len(set(case["expected_relevant_memory_ids"]) & set(case["selected_ids"]))
        for case in zero_cases
    )
    hard_cases = [case for case in cases if case["category"] == "same_topic_hard_negative"]
    return {
        "case_count": len(cases),
        "relevant_recall": relevant_hits / relevant_total if relevant_total else 1.0,
        "precision": relevant_hits / selected_total if selected_total else 1.0,
        "irrelevant_injection": irrelevant_total / selected_total if selected_total else 0.0,
        "zero_overlap": {
            "case_count": len(zero_cases),
            "relevant_recall": zero_hits / zero_relevant if zero_relevant else 1.0,
        },
        "hard_negative": {
            "case_count": len(hard_cases),
            "cases_with_selection": sum(bool(case["selected_ids"]) for case in hard_cases),
            "selected_count": sum(len(case["selected_ids"]) for case in hard_cases),
        },
        "retrieval_tokens": {
            "total": token_total,
            "mean": token_total / len(cases) if cases else 0.0,
        },
        "latency_ms": {
            "mean": statistics.fmean(latencies) if latencies else 0.0,
            "p50": _percentile(latencies, 0.50),
            "p95": _percentile(latencies, 0.95),
        },
    }


def _candidate_from_parameters(name: str, parameters: Mapping[str, Any]) -> CandidateBackend:
    """Construct exactly the candidate named by the committed parameter report."""

    if name == "lexical-control":
        candidate: CandidateBackend = LexicalControlBackend(now=spike.DEFAULT_NOW)
    elif name == "bm25-content":
        candidate = BM25ContentBackend(
            k1=float(parameters["k1"]),
            b=float(parameters["b"]),
            minimum_score=float(parameters["minimum_score"]),
            relative_floor=float(parameters["relative_score_floor"]),
        )
    elif name == "structured-bm25":
        candidate = StructuredBM25Backend(
            k1=float(parameters["k1"]),
            b=float(parameters["b"]),
            minimum_score=float(parameters["minimum_score"]),
            relative_floor=float(parameters["relative_score_floor"]),
            metadata_weight=float(parameters["metadata_weight"]),
        )
    elif name == "embedding-hybrid":
        candidate = EmbeddingHybridBackend(
            adapter=None,
            k1=float(parameters["k1"]),
            b=float(parameters["b"]),
            minimum_score=float(parameters["minimum_score"]),
            relative_floor=float(parameters["relative_score_floor"]),
            embedding_weight=float(parameters["embedding_weight"]),
        )
    else:
        raise AuditError(f"unknown committed candidate: {name}")
    if dict(candidate.parameters) != dict(parameters):
        raise AuditError(f"candidate parameters changed for {name}")
    return candidate


def _local_result_digest(results: Sequence[CandidateResult]) -> str:
    payload = stable_json([result.deterministic_dict() for result in results])
    return sha256_bytes(payload.encode("utf-8"))


def _selection_digest(cases: Sequence[Mapping[str, Any]]) -> str:
    # Match the committed evaluator's digest protocol exactly: stable JSON with
    # no trailing newline.  The general report hash helper intentionally adds a
    # newline for standalone JSON files, but this field is an embedded digest.
    payload = stable_json(
        [
            {"case_id": case["case_id"], "selected_ids": case["selected_ids"]}
            for case in cases
        ]
    )
    return sha256_bytes(payload.encode("utf-8"))


def _case_observation(case: Mapping[str, Any], result: CandidateResult) -> dict[str, Any]:
    # The result is captured before the evaluator reads expected relevance.
    return {
        "case_id": str(case["case_id"]),
        "category": str(case["category"]),
        "selected_ids": list(result.selected_ids),
        "funnel": result.to_dict(),
    }


def _independent_candidate_run(
    suite: Mapping[str, Any],
    committed: Mapping[str, Any],
    *,
    repeats: int,
) -> dict[str, Any]:
    name = str(committed["backend"])
    parameters = _mapping(committed["parameters"], f"{name} parameters")
    candidate = _candidate_from_parameters(name, parameters)
    pools_raw = _mapping(suite["memory_pools"], "memory_pools")
    repositories = _mapping(suite["repositories"], "repositories")
    pool_states = {
        str(pool_id): spike.build_pool(_mapping(pool, f"pool {pool_id}"))
        for pool_id, pool in pools_raw.items()
    }
    repeated_cases: list[list[dict[str, Any]]] = []
    repeated_results: list[list[CandidateResult]] = []
    cases_raw = suite["cases"]
    if not isinstance(cases_raw, list):
        raise AuditError("suite cases must be an array")
    try:
        for _repeat in range(repeats):
            observations: list[dict[str, Any]] = []
            results: list[CandidateResult] = []
            for raw_case in cases_raw:
                case = _mapping(raw_case, "case")
                pool_id = str(case["memory_pool_id"])
                repository_id = str(case["repository_id"])
                request, result = spike.invoke_backend(
                    candidate,
                    case,
                    repository=_mapping(repositories[repository_id], "repository"),
                    pool=pool_states[pool_id],
                    now=spike.DEFAULT_NOW,
                )
                # This is an audit assertion: no evaluator-only field is sent to the backend.
                request_payload = stable_json(request.to_dict())
                for forbidden in spike.FORBIDDEN_BACKEND_LABEL_KEYS:
                    if f'"{forbidden}"' in request_payload:
                        raise AuditError(f"label leaked into {name} request: {forbidden}")
                results.append(result)
                observations.append(_case_observation(case, result))
            repeated_results.append(results)
            repeated_cases.append(observations)
    finally:
        for pool in pool_states.values():
            pool.store.close()

    committed_cases = committed["cases"]
    if not isinstance(committed_cases, list) or len(committed_cases) != len(repeated_cases[0]):
        raise AuditError(f"committed case count mismatch for {name}")
    committed_by_id = {str(case["case_id"]): case for case in committed_cases}
    first_cases: list[dict[str, Any]] = []
    selection_matches: list[dict[str, Any]] = []
    for case in repeated_cases[0]:
        committed_case = _mapping(committed_by_id[case["case_id"]], "committed case")
        committed_ids = list(committed_case["selected_ids"])
        match = case["selected_ids"] == committed_ids
        selection_matches.append(
            {
                "case_id": case["case_id"],
                "independent_selected_ids": case["selected_ids"],
                "committed_selected_ids": committed_ids,
                "match": match,
            }
        )
        first_cases.append(
            {
                **case,
                "expected_relevant_memory_ids": list(
                    committed_case["expected_relevant_memory_ids"]
                ),
            }
        )
    if not all(item["match"] for item in selection_matches):
        raise AuditError(f"selected IDs changed for {name}")

    independent_digests = [_local_result_digest(results) for results in repeated_results]
    committed_digests = list(_mapping(committed["repeatability"], "repeatability")["digests"])
    committed_runs = int(_mapping(committed["repeatability"], "repeatability")["runs"])
    if committed_runs != repeats:
        raise AuditError(f"committed repeat count mismatch for {name}")
    if independent_digests != committed_digests:
        raise AuditError(f"candidate digest mismatch for {name}")
    independent_selection_digest = _selection_digest(first_cases)
    committed_selection_digest = _selection_digest(committed_cases)
    if independent_selection_digest != committed_selection_digest:
        raise AuditError(f"case selection digest mismatch for {name}")
    return {
        "backend": name,
        "availability": str(committed["availability"]),
        "parameters": dict(parameters),
        "parameters_digest": _sha256_json(parameters),
        "repeatability": {
            "runs": repeats,
            "committed_digests": committed_digests,
            "independent_digests": independent_digests,
            "all_match": True,
        },
        "selection_matches": selection_matches,
        "case_selection_digest": independent_selection_digest,
        "committed_case_selection_digest": committed_selection_digest,
        "metrics": _metrics(first_cases),
        "independent_case_count": len(first_cases),
        "independent_available": first_cases[0]["funnel"]["availability"] == "available"
        if first_cases
        else False,
        "independent_latency_ms": _metrics(first_cases)["latency_ms"],
    }


def _verify_fixed_bundle(
    suite_path: str | Path,
    source_manifest_path: str | Path,
    source_raw_path: str | Path,
    source_summary_path: str | Path,
    retrieval_path: str | Path,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any]]:
    suite = spike.load_suite(suite_path)
    source_manifest = _load_json(source_manifest_path)
    source_raw = _load_json(source_raw_path)
    source_summary = _load_json(source_summary_path)
    suite_sha = sha256_file(suite_path)
    retrieval_sha = sha256_file(retrieval_path)
    if suite_sha != source_manifest["suite_sha256"]:
        raise AuditError("suite SHA does not match committed manifest")
    if suite_sha != source_raw["suite_sha256"] or suite_sha != source_summary["suite_sha256"]:
        raise AuditError("suite SHA is inconsistent across raw/summary")
    if retrieval_sha != source_manifest["production_retrieval_sha256"]:
        raise AuditError("production retrieval SHA does not match committed manifest")
    if retrieval_sha != F1D03CF_RETRIEVAL_SHA256:
        raise AuditError("retrieval.py is not the f1d03cf authority")
    if source_manifest["production_retriever_changed"] or source_raw["production_retriever_changed"]:
        raise AuditError("fixed evidence claims production retriever changed")
    if source_summary["production_retriever_changed"]:
        raise AuditError("fixed summary claims production retriever changed")
    if source_manifest["candidate_names"] != [item["backend"] for item in source_raw["candidates"]]:
        raise AuditError("manifest candidate names do not match raw result")
    if source_manifest["candidate_names"] != [item["backend"] for item in source_summary["candidates"]]:
        raise AuditError("manifest candidate names do not match summary")
    if sha256_file(source_raw_path) != source_manifest["raw_result_sha256"]:
        raise AuditError("raw result hash mismatch")
    if sha256_file(source_summary_path) != source_manifest["redacted_summary_sha256"]:
        raise AuditError("summary hash mismatch")
    raw_by_name = {str(item["backend"]): item for item in source_raw["candidates"]}
    summary_by_name = {str(item["backend"]): item for item in source_summary["candidates"]}
    if set(raw_by_name) != set(summary_by_name):
        raise AuditError("raw and summary candidate sets differ")
    for name, raw_candidate in raw_by_name.items():
        summary_candidate = summary_by_name[name]
        if raw_candidate["parameters"] != summary_candidate["parameters"]:
            raise AuditError(f"parameter mismatch between raw and summary for {name}")
        if raw_candidate["repeatability"] != summary_candidate["repeatability"]:
            raise AuditError(f"repeatability mismatch between raw and summary for {name}")
        if raw_candidate["availability"] != summary_candidate["availability"]:
            raise AuditError(f"availability mismatch between raw and summary for {name}")
        if raw_candidate["availability"] == "available":
            raw_cases = raw_candidate["cases"]
            expected_metrics = _metrics(raw_cases)
            committed_metrics = summary_candidate["metrics"]
            for key in (
                "case_count",
                "relevant_recall",
                "precision",
                "irrelevant_injection",
                "zero_overlap",
                "hard_negative",
                "retrieval_tokens",
            ):
                if key in {"relevant_recall", "precision", "irrelevant_injection"}:
                    if not _float_close(expected_metrics[key], committed_metrics[key]):
                        raise AuditError(f"metric arithmetic mismatch for {name}: {key}")
                elif expected_metrics[key] != committed_metrics[key]:
                    raise AuditError(f"metric arithmetic mismatch for {name}: {key}")
            for key in ("mean", "p50", "p95"):
                if not _float_close(
                    expected_metrics["latency_ms"][key], committed_metrics["latency_ms"][key]
                ):
                    raise AuditError(f"latency arithmetic mismatch for {name}: {key}")
            expected_digest = _selection_digest(raw_cases)
            if expected_digest != summary_candidate["case_selection_digest"]:
                raise AuditError(f"case selection digest mismatch for {name}")
    return suite, source_manifest, source_raw, source_summary, {
        "suite_sha256": suite_sha,
        "retrieval_sha256": retrieval_sha,
        "source_manifest_sha256": sha256_file(source_manifest_path),
    }


def _record_info(suite: Mapping[str, Any]) -> dict[str, dict[str, Mapping[str, Any]]]:
    result: dict[str, dict[str, Mapping[str, Any]]] = {}
    for pool_id, pool_value in _mapping(suite["memory_pools"], "memory pools").items():
        records = _mapping(pool_value, f"pool {pool_id}").get("records")
        if not isinstance(records, list):
            raise AuditError(f"pool {pool_id} records must be an array")
        result[str(pool_id)] = {
            str(record["memory_id"]): record for record in records if isinstance(record, Mapping)
        }
    return result


def _pareto_case_selection(
    case: Mapping[str, Any],
    score_components: Sequence[Mapping[str, Any]],
    records: Mapping[str, Mapping[str, Any]],
    *,
    score_threshold: float,
    top_k: int,
    relative_floor: float,
) -> list[str]:
    candidates = [
        item
        for item in score_components
        if float(item["score"]) >= score_threshold
    ]
    candidates.sort(
        key=lambda item: (
            -float(item["score"]),
            -SCOPE_PRIORITY.get(str(records[str(item["memory_id"])].get("scope")), 0),
            int(item["token_cost"]),
            str(item["memory_id"]),
        )
    )
    dynamic_floor = float(candidates[0]["score"]) * relative_floor if candidates else 0.0
    token_budget = int(case.get("token_budget", 512))
    selected: list[str] = []
    token_total = 0
    for item in candidates:
        if float(item["score"]) < dynamic_floor:
            break
        if len(selected) >= top_k:
            break
        token_cost = int(item["token_cost"])
        if token_total + token_cost > token_budget:
            continue
        selected.append(str(item["memory_id"]))
        token_total += token_cost
    return selected


def _pareto_metrics(
    cases: Sequence[Mapping[str, Any]],
    selections: Mapping[str, Sequence[str]],
) -> dict[str, Any]:
    relevant_total = sum(len(case["expected_relevant_memory_ids"]) for case in cases)
    hit_total = 0
    selected_total = 0
    token_total = 0
    for case in cases:
        selected = list(selections[case["case_id"]])
        expected = set(case["expected_relevant_memory_ids"])
        hit_total += len(expected & set(selected))
        selected_total += len(selected)
        # The score sweep uses raw token_cost values; selection records are populated by caller.
        token_total += sum(
            int(item["token_cost"])
            for item in case["_score_components"]
            if str(item["memory_id"]) in selected
        )
    return {
        "relevant_recall": hit_total / relevant_total if relevant_total else 1.0,
        "precision": hit_total / selected_total if selected_total else 1.0,
        "irrelevant_injection": (
            (selected_total - hit_total) / selected_total if selected_total else 0.0
        ),
        "retrieval_tokens": token_total,
        "selected_count": selected_total,
    }


def _dominates(left: Mapping[str, Any], right: Mapping[str, Any]) -> bool:
    no_worse = (
        float(left["relevant_recall"]) >= float(right["relevant_recall"])
        and float(left["precision"]) >= float(right["precision"])
        and float(left["irrelevant_injection"]) <= float(right["irrelevant_injection"])
        and int(left["retrieval_tokens"]) <= int(right["retrieval_tokens"])
    )
    strictly_better = (
        float(left["relevant_recall"]) > float(right["relevant_recall"])
        or float(left["precision"]) > float(right["precision"])
        or float(left["irrelevant_injection"]) < float(right["irrelevant_injection"])
        or int(left["retrieval_tokens"]) < int(right["retrieval_tokens"])
    )
    return no_worse and strictly_better


def _bounds(points: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    safe = [point for point in points if float(point["irrelevant_injection"]) <= 0.15]
    accurate = [point for point in points if float(point["relevant_recall"]) >= 0.85]
    return {
        "max_recall_when_irrelevant_injection_lte_0.15": max(
            (float(point["relevant_recall"]) for point in safe), default=None
        ),
        "min_irrelevant_injection_when_recall_gte_0.85": min(
            (float(point["irrelevant_injection"]) for point in accurate), default=None
        ),
        "feasible_point_count_injection_lte_0.15": len(safe),
        "feasible_point_count_recall_gte_0.85": len(accurate),
    }


def pareto_from_raw(
    suite: Mapping[str, Any],
    raw: Mapping[str, Any],
) -> dict[str, Any]:
    """Sweep raw score values without invoking or changing a backend."""

    pools = _record_info(suite)
    cases = suite["cases"]
    if not isinstance(cases, list):
        raise AuditError("suite cases must be an array")
    raw_by_name = {str(item["backend"]): item for item in raw["candidates"]}
    candidate_reports: dict[str, Any] = {}
    all_points: list[dict[str, Any]] = []
    for name, candidate in raw_by_name.items():
        if candidate["availability"] != "available":
            candidate_reports[name] = {
                "availability": "unavailable",
                "score_thresholds": [],
                "top_k_values": [],
                "all_operating_point_count": 0,
                "non_dominated_operating_points": [],
                "bounds": _bounds([]),
            }
            continue
        relative_floor = float(candidate["parameters"]["relative_score_floor"])
        case_by_id = {str(case["case_id"]): case for case in candidate["cases"]}
        score_values = {
            float(item["score"])
            for case in candidate["cases"]
            for item in case["funnel"]["score_components"]
        }
        thresholds = sorted(score_values, reverse=True)
        top_k_max = max(int(case.get("top_k", 5)) for case in cases)
        points: list[dict[str, Any]] = []
        for threshold in thresholds:
            selections: dict[str, list[str]] = {}
            prepared_cases: list[dict[str, Any]] = []
            for source_case in cases:
                case_id = str(source_case["case_id"])
                candidate_case = case_by_id[case_id]
                pool_id = str(source_case["memory_pool_id"])
                selections[case_id] = []
                prepared_cases.append(
                    {
                        **source_case,
                        "_score_components": candidate_case["funnel"]["score_components"],
                    }
                )
                # Store the selected IDs for each top-k below; score components remain raw.
                _ = pools[pool_id]
            for top_k in range(1, top_k_max + 1):
                for prepared_case in prepared_cases:
                    case_id = str(prepared_case["case_id"])
                    pool_id = str(prepared_case["memory_pool_id"])
                    selections[case_id] = _pareto_case_selection(
                        prepared_case,
                        prepared_case["_score_components"],
                        pools[pool_id],
                        score_threshold=threshold,
                        top_k=top_k,
                        relative_floor=relative_floor,
                    )
                metrics = _pareto_metrics(prepared_cases, selections)
                point = {
                    "backend": name,
                    "score_threshold": threshold,
                    "top_k": top_k,
                    **metrics,
                    "selection_digest": _sha256_json(
                        [
                            {"case_id": case_id, "selected_ids": selections[case_id]}
                            for case_id in sorted(selections)
                        ]
                    ),
                }
                points.append(point)
                all_points.append(point)
        unique_points: dict[tuple[Any, ...], dict[str, Any]] = {}
        for point in points:
            key = (
                point["relevant_recall"],
                point["precision"],
                point["irrelevant_injection"],
                point["retrieval_tokens"],
            )
            unique_points.setdefault(key, point)
        metric_points = list(unique_points.values())
        non_dominated = [
            point
            for point in metric_points
            if not any(_dominates(other, point) for other in metric_points if other is not point)
        ]
        non_dominated.sort(
            key=lambda point: (
                -float(point["relevant_recall"]),
                float(point["irrelevant_injection"]),
                int(point["retrieval_tokens"]),
                -float(point["score_threshold"]),
                int(point["top_k"]),
            )
        )
        candidate_reports[name] = {
            "availability": "available",
            "score_thresholds": thresholds,
            "top_k_values": list(range(1, top_k_max + 1)),
            "all_operating_point_count": len(points),
            "unique_metric_point_count": len(metric_points),
            "non_dominated_operating_points": non_dominated,
            "bounds": _bounds(points),
        }
    available_points = [point for point in all_points if point["backend"] in candidate_reports and candidate_reports[point["backend"]]["availability"] == "available"]
    global_unique: dict[tuple[Any, ...], dict[str, Any]] = {}
    for point in available_points:
        key = (
            point["backend"],
            point["relevant_recall"],
            point["precision"],
            point["irrelevant_injection"],
            point["retrieval_tokens"],
        )
        global_unique.setdefault(key, point)
    global_points = list(global_unique.values())
    global_non_dominated = [
        point
        for point in global_points
        if not any(_dominates(other, point) for other in global_points if other is not point)
    ]
    global_non_dominated.sort(
        key=lambda point: (
            -float(point["relevant_recall"]),
            float(point["irrelevant_injection"]),
            int(point["retrieval_tokens"]),
            str(point["backend"]),
            -float(point["score_threshold"]),
            int(point["top_k"]),
        )
    )
    return {
        "score_source": "committed raw-result.json score_components only",
        "backend_reinvocation": False,
        "labels_used_only_after_retrieval": True,
        "candidate_reports": candidate_reports,
        "global_bounds": _bounds(global_points),
        "global_non_dominated_operating_points": global_non_dominated,
    }


def audit_fixed_bundle(
    *,
    suite_path: str | Path = DEFAULT_SUITE,
    source_manifest_path: str | Path = DEFAULT_SOURCE_MANIFEST,
    source_raw_path: str | Path = DEFAULT_SOURCE_RAW,
    source_summary_path: str | Path = DEFAULT_SOURCE_SUMMARY,
    retrieval_path: str | Path = DEFAULT_RETRIEVAL,
    baseline_commit: str = BASELINE_COMMIT,
    repeats: int = DEFAULT_REPEATS,
) -> tuple[dict[str, Any], dict[str, Any]]:
    suite, source_manifest, source_raw, source_summary, hashes = _verify_fixed_bundle(
        suite_path,
        source_manifest_path,
        source_raw_path,
        source_summary_path,
        retrieval_path,
    )
    if baseline_commit != BASELINE_COMMIT:
        raise AuditError(f"unexpected audit baseline: {baseline_commit}")
    committed_by_name = {str(item["backend"]): item for item in source_raw["candidates"]}
    candidate_audits = [
        _independent_candidate_run(
            suite,
            committed_by_name[name],
            repeats=repeats,
        )
        for name in source_manifest["candidate_names"]
    ]
    pareto = pareto_from_raw(suite, source_raw)
    raw_report = {
        "schema_version": 1,
        "report_kind": "memory_retrieval_backend_independent_audit_raw",
        "baseline_commit": baseline_commit,
        "suite_id": suite["suite_id"],
        "suite_sha256": hashes["suite_sha256"],
        "source_manifest_sha256": hashes["source_manifest_sha256"],
        "production_retrieval_sha256": hashes["retrieval_sha256"],
        "production_retriever_changed": False,
        "provider_evaluation": False,
        "repeats": repeats,
        "candidate_audits": candidate_audits,
        "pareto": pareto,
        "bundle_consistency": {
            "source_manifest_raw_summary_consistent": True,
            "retrieval_matches_f1d03cf": True,
            "candidate_parameters_reconstructed": True,
            "candidate_digests_match": True,
            "selected_ids_match": True,
            "metric_arithmetic_recomputed": True,
        },
    }
    summary_candidates = []
    for item in candidate_audits:
        summary_candidates.append(
            {
                "backend": item["backend"],
                "availability": item["availability"],
                "parameters_digest": item["parameters_digest"],
                "candidate_digests_match": item["repeatability"]["all_match"],
                "selected_ids_match": all(
                    selection["match"] for selection in item["selection_matches"]
                ),
                "metrics": item["metrics"] if item["availability"] == "available" else None,
                "independent_latency_ms": item["independent_latency_ms"],
                "case_selection_digest": item["case_selection_digest"],
            }
        )
    summary = {
        "schema_version": 1,
        "report_kind": "memory_retrieval_backend_independent_audit_summary",
        "baseline_commit": baseline_commit,
        "suite_id": suite["suite_id"],
        "suite_sha256": hashes["suite_sha256"],
        "production_retrieval_sha256": hashes["retrieval_sha256"],
        "production_retriever_changed": False,
        "provider_evaluation": False,
        "candidates": summary_candidates,
        "pareto": pareto,
        "bundle_consistency": raw_report["bundle_consistency"],
    }
    return raw_report, summary


def write_bundle(
    output: str | Path,
    *,
    raw: Mapping[str, Any],
    summary: Mapping[str, Any],
    source_manifest_path: str | Path,
    source_raw_path: str | Path,
    source_summary_path: str | Path,
) -> dict[str, Any]:
    output_path = Path(output)
    if output_path.exists() and any(output_path.iterdir()):
        raise AuditError("audit output directory must be empty")
    output_path.mkdir(parents=True, exist_ok=True)
    raw_path = output_path / "raw-result.json"
    summary_path = output_path / "redacted-summary.json"
    pareto_path = output_path / "pareto.json"
    raw_path.write_text(json.dumps(raw, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    summary_path.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    pareto_path.write_text(
        json.dumps(summary["pareto"], ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    manifest = {
        "schema_version": 1,
        "report_kind": "memory_retrieval_backend_independent_audit_manifest",
        "baseline_commit": raw["baseline_commit"],
        "suite_sha256": raw["suite_sha256"],
        "source_manifest_sha256": sha256_file(source_manifest_path),
        "source_raw_result_sha256": sha256_file(source_raw_path),
        "source_redacted_summary_sha256": sha256_file(source_summary_path),
        "production_retrieval_sha256": raw["production_retrieval_sha256"],
        "production_retriever_changed": False,
        "provider_evaluation": False,
        "candidate_names": [item["backend"] for item in summary["candidates"]],
        "candidate_digests_match": all(
            bool(item["candidate_digests_match"]) for item in summary["candidates"]
        ),
        "selected_ids_match": all(bool(item["selected_ids_match"]) for item in summary["candidates"]),
        "raw_result_sha256": sha256_file(raw_path),
        "redacted_summary_sha256": sha256_file(summary_path),
        "pareto_sha256": sha256_file(pareto_path),
        "pareto_source": "committed raw score_components; no backend invocation",
        "holdout_v3_created": False,
        "default_application_memory_enabled": False,
    }
    (output_path / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return manifest


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--suite", type=Path, default=DEFAULT_SUITE)
    parser.add_argument("--source-manifest", type=Path, default=DEFAULT_SOURCE_MANIFEST)
    parser.add_argument("--source-raw", type=Path, default=DEFAULT_SOURCE_RAW)
    parser.add_argument("--source-summary", type=Path, default=DEFAULT_SOURCE_SUMMARY)
    parser.add_argument("--retrieval", type=Path, default=DEFAULT_RETRIEVAL)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--baseline-commit", default=BASELINE_COMMIT)
    parser.add_argument("--repeats", type=int, default=DEFAULT_REPEATS)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    raw, summary = audit_fixed_bundle(
        suite_path=args.suite,
        source_manifest_path=args.source_manifest,
        source_raw_path=args.source_raw,
        source_summary_path=args.source_summary,
        retrieval_path=args.retrieval,
        baseline_commit=args.baseline_commit,
        repeats=args.repeats,
    )
    manifest = write_bundle(
        args.output,
        raw=raw,
        summary=summary,
        source_manifest_path=args.source_manifest,
        source_raw_path=args.source_raw,
        source_summary_path=args.source_summary,
    )
    print(json.dumps({"manifest": manifest, "pareto": summary["pareto"]}, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
