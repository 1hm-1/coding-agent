#!/usr/bin/env python3
"""Create a read-only M0 projection from legacy offline eval reports.

The source reports are never rewritten. This projection makes the legacy
measurement limitations explicit and records hashes for reproduction.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path
from typing import Any, Mapping, Sequence


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(65536), b""):
            digest.update(block)
    return digest.hexdigest()


def _load_object(path: Path) -> dict[str, Any]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return raw


def _load_runs(path: Path) -> list[dict[str, Any]]:
    runs: list[dict[str, Any]] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        raw = json.loads(line)
        if not isinstance(raw, dict):
            raise ValueError(f"{path}:{line_number} must contain a JSON object")
        runs.append(raw)
    return runs


def _percentile(values: Sequence[float], percentile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, math.ceil(percentile * len(ordered)) - 1))
    return ordered[index]


def _distribution(values: Sequence[float]) -> dict[str, float | int | None]:
    return {
        "count": len(values),
        "total": sum(values),
        "mean": sum(values) / len(values) if values else None,
        "p50": _percentile(values, 0.50),
        "p95": _percentile(values, 0.95),
    }


def _usage_projection(run: Mapping[str, Any]) -> dict[str, Any]:
    metrics = run.get("metrics", {})
    if not isinstance(metrics, Mapping):
        metrics = {}
    fields = (
        "input_tokens",
        "output_tokens",
        "compression_input_tokens",
        "compression_output_tokens",
    )
    values = {name: metrics.get(name) for name in fields}
    # Scripted values are fixture data. A legacy zero has no presence/provenance
    # bit, so M0 cannot reinterpret it as a measured provider zero.
    classifications = {
        name: ("unknown" if value in (None, 0) else "synthetic")
        for name, value in values.items()
    }
    complete = all(value not in (None, 0) for value in values.values())
    return {
        "values": values,
        "classifications": classifications,
        "complete_total": complete,
        "total": sum(int(value) for value in values.values()) if complete else None,
    }


def project_report(path: Path, runs_path: Path | None = None) -> dict[str, Any]:
    report = _load_object(path)
    if runs_path is None:
        candidate = path.with_name("runs.jsonl")
        runs_path = candidate if candidate.is_file() else None
    raw_runs = _load_runs(runs_path) if runs_path is not None else report.get("runs", [])
    if not isinstance(raw_runs, list):
        raise ValueError(f"{path} report.runs must be an array")
    populations = {
        "normal": [],
        "negative_control": [],
        "infrastructure_invalid": [],
    }
    projected_runs: list[dict[str, Any]] = []
    for raw in raw_runs:
        if not isinstance(raw, Mapping):
            raise ValueError(f"{path} contains a non-object run")
        case_type = str(raw.get("case_type", "task"))
        infrastructure = bool(raw.get("infrastructure_failure", False))
        population = (
            "infrastructure_invalid"
            if infrastructure
            else "negative_control"
            if case_type == "negative_control"
            else "normal"
        )
        session_id = raw.get("session_id")
        identity = {
            "case_id": str(raw.get("case_id", "")),
            "repetition": int(raw.get("repetition", 0)),
            "legacy_session_id": str(session_id) if session_id else None,
        }
        populations[population].append(identity)
        metrics = raw.get("metrics", {})
        if not isinstance(metrics, Mapping):
            metrics = {}
        projected_runs.append(
            {
                **identity,
                "population": population,
                "case_type": case_type,
                "state": str(raw.get("state", "")),
                "valid": bool(raw.get("valid", not infrastructure)),
                "infrastructure_failure": infrastructure,
                "failure_reason": raw.get("failure_reason"),
                "runtime_completed": bool(raw.get("runtime_completed", False)),
                "task_success": bool(raw.get("task_success", False)),
                "oracle_success": bool(raw.get("oracle_success", False)),
                "end_to_end_success": bool(raw.get("end_to_end_success", False)),
                "source_invariant": raw.get("source_invariant"),
                "oracles": raw.get("oracles", []),
                "legacy_metrics": dict(metrics),
                "usage": _usage_projection(raw),
                "legacy_eval_wall_time": {
                    "value_ms": metrics.get("end_to_end_latency_ms"),
                    "classification": "measured",
                    "boundary": "EvaluationRunner._run_case entry through fixture resolution/fingerprint, application/workspace setup, Runtime execution, source check, and oracle execution",
                    "not_equivalent_to": "end_to_end_elapsed_ms durable acceptance to terminal outcome",
                },
                "model_latency": {
                    "value_ms": metrics.get("model_latency_ms"),
                    "classification": "measured_success_only",
                    "limitation": "failed, cancelled, and uncertain model attempts lack complete latency coverage",
                },
                "legacy_permission_denials": int(metrics.get("permission_violations", 0)),
                "proven_safety_violations": None,
            }
        )
    aggregate_names = (
        "requested_run_count",
        "valid_run_count",
        "capability_run_count",
        "infrastructure_failure_count",
        "task_success_rate",
        "runtime_completion_rate",
        "oracle_success_rate",
        "end_to_end_success_rate",
        "source_invariant_rate",
        "source_invariant_unknown_count",
        "recovery_rate",
        "negative_control_runs",
        "failure_reasons",
        "infrastructure_failure_reasons",
    )
    valid_runs = [run for run in raw_runs if bool(run.get("valid", False))]
    metric_names = (
        "tool_attempts",
        "tool_calls",
        "tool_executions",
        "tool_latency_ms",
        "model_calls",
        "model_latency_ms",
        "latency_ms",
        "end_to_end_latency_ms",
        "test_latency_ms",
    )
    recomputed = {
        name: _distribution(
            [
                float(run.get("metrics", {}).get(name, 0.0))
                for run in valid_runs
                if isinstance(run.get("metrics"), Mapping)
            ]
        )
        for name in metric_names
    }
    reported_metrics = report.get("metrics", {})
    metric_match = {
        name: recomputed[name] == reported_metrics.get(name)
        for name in metric_names
    }
    return {
        "source": str(path),
        "source_sha256": sha256_file(path),
        "runs_source": str(runs_path) if runs_path is not None else "embedded report.runs",
        "runs_source_sha256": sha256_file(runs_path) if runs_path is not None else None,
        "source_schema": report.get("schema_version"),
        "backend_classification": "synthetic scripted fixture",
        "reported_aggregates": {name: report.get(name) for name in aggregate_names},
        "recomputed_valid_run_distributions": recomputed,
        "recalculation_matches_report_metrics": metric_match,
        "populations": populations,
        "runs": projected_runs,
        "limitations": [
            "model latency is collected from MODEL_CALL_SUCCEEDED pairs only",
            "missing usage may be encoded as legacy zero and is therefore unknown",
            "legacy permission_violations counts denials, not unauthorized effects",
            "machine-active, typed permission wait, other user wait, and host downtime are not fully measured",
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--full-report", type=Path, required=True)
    parser.add_argument("--stability-report", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = {
        "schema_version": 1,
        "measurement_contract": "metric-dictionary-v1.json",
        "full_suite": project_report(args.full_report),
        "stability": project_report(args.stability_report),
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
