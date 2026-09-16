from __future__ import annotations

from dataclasses import dataclass
from math import ceil
from typing import Sequence

from coding_agent.domain import JsonObject


@dataclass(frozen=True)
class MemoryPairResult:
    """One paired cold/warm result produced by a trusted task oracle."""

    case_id: str
    cold_task_success: bool
    warm_task_success: bool
    relevant_memory_ids: frozenset[str]
    selected_memory_ids: tuple[str, ...]
    retrieval_tokens: int
    retrieval_latency_ms: float
    cold_input_tokens: int = 0
    cold_output_tokens: int = 0
    warm_input_tokens: int = 0
    warm_output_tokens: int = 0
    cold_latency_ms: float = 0.0
    warm_latency_ms: float = 0.0
    memory_context_tokens: int = 0
    cold_answer: str | None = None
    warm_answer: str | None = None
    unrelated_case: bool = False

    def __post_init__(self) -> None:
        if not self.case_id:
            raise ValueError("memory eval case_id is required")
        for name in (
            "retrieval_tokens",
            "cold_input_tokens",
            "cold_output_tokens",
            "warm_input_tokens",
            "warm_output_tokens",
            "memory_context_tokens",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")
        for name in (
            "retrieval_latency_ms",
            "cold_latency_ms",
            "warm_latency_ms",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
                raise ValueError(f"{name} must be a non-negative number")


def _percentile(values: Sequence[float], fraction: float) -> float:
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, ceil(len(ordered) * fraction) - 1))
    return ordered[index]


def _stats(values: Sequence[int | float]) -> JsonObject:
    if not values:
        raise ValueError("memory benchmark stats require at least one value")
    numeric = [float(value) for value in values]
    total = sum(values)
    return {
        "total": total,
        "mean": total / len(values),
        "p50": _percentile(numeric, 0.50),
        "p95": _percentile(numeric, 0.95),
    }


def _model_token_stats(
    results: tuple[MemoryPairResult, ...],
    arm: str,
) -> JsonObject:
    inputs = [int(getattr(result, f"{arm}_input_tokens")) for result in results]
    outputs = [int(getattr(result, f"{arm}_output_tokens")) for result in results]
    totals = [input_tokens + output_tokens for input_tokens, output_tokens in zip(inputs, outputs)]
    return {
        "input": _stats(inputs),
        "output": _stats(outputs),
        "total": _stats(totals),
    }


def summarize_memory_pairs(results: tuple[MemoryPairResult, ...]) -> JsonObject:
    if not results:
        raise ValueError("memory eval requires at least one paired result")
    cold_successes = sum(result.cold_task_success for result in results)
    warm_successes = sum(result.warm_task_success for result in results)
    relevant_total = sum(len(result.relevant_memory_ids) for result in results)
    recalled_total = sum(
        len(result.relevant_memory_ids.intersection(result.selected_memory_ids))
        for result in results
    )
    relevant_selected_total = sum(
        len(result.relevant_memory_ids.intersection(result.selected_memory_ids))
        for result in results
    )
    selected_total = sum(len(result.selected_memory_ids) for result in results)
    irrelevant_total = sum(
        len(set(result.selected_memory_ids) - result.relevant_memory_ids)
        for result in results
    )
    count = len(results)
    cold_rate = cold_successes / count
    warm_rate = warm_successes / count
    unrelated_results = tuple(result for result in results if result.unrelated_case)
    comparable_unrelated = tuple(
        result
        for result in unrelated_results
        if result.cold_answer is not None and result.warm_answer is not None
    )
    unrelated_changes = sum(
        result.cold_answer != result.warm_answer for result in comparable_unrelated
    )
    injected_unrelated = tuple(
        result
        for result in comparable_unrelated
        if result.selected_memory_ids and not result.relevant_memory_ids
    )
    injected_unrelated_changes = sum(
        result.cold_answer != result.warm_answer for result in injected_unrelated
    )
    cold_model_total = sum(
        result.cold_input_tokens + result.cold_output_tokens for result in results
    )
    warm_model_total = sum(
        result.warm_input_tokens + result.warm_output_tokens for result in results
    )
    warm_end_to_end_total = warm_model_total + sum(
        result.retrieval_tokens for result in results
    )
    cold_success_tokens = cold_model_total / cold_successes if cold_successes else None
    warm_success_tokens = (
        warm_model_total / warm_successes if warm_successes else None
    )
    cold_end_to_end_success_tokens = cold_success_tokens
    warm_end_to_end_success_tokens = (
        warm_end_to_end_total / warm_successes if warm_successes else None
    )
    return {
        "schema_version": 1,
        "paired_cases": count,
        "task_success": {
            "cold": cold_rate,
            "warm": warm_rate,
            "delta": warm_rate - cold_rate,
        },
        "relevant_recall": recalled_total / relevant_total if relevant_total else 1.0,
        "precision": (
            relevant_selected_total / selected_total if selected_total else 1.0
        ),
        "irrelevant_injection_rate": (
            irrelevant_total / selected_total if selected_total else 0.0
        ),
        "unrelated_behavior_change_rate": (
            unrelated_changes / len(comparable_unrelated)
            if comparable_unrelated
            else 0.0
        ),
        "unrelated_memory_behavior_change_rate": (
            injected_unrelated_changes / len(injected_unrelated)
            if injected_unrelated
            else 0.0
        ),
        "retrieval_tokens": {
            "total": sum(result.retrieval_tokens for result in results),
            "mean": sum(result.retrieval_tokens for result in results) / count,
        },
        "retrieval_latency_ms": {
            "total": sum(result.retrieval_latency_ms for result in results),
            "mean": sum(result.retrieval_latency_ms for result in results) / count,
        },
        "model_tokens": {
            "cold": _model_token_stats(results, "cold"),
            "warm": _model_token_stats(results, "warm"),
            "delta_total": (
                sum(result.warm_input_tokens + result.warm_output_tokens for result in results)
                - sum(result.cold_input_tokens + result.cold_output_tokens for result in results)
            ),
        },
        "tokens_per_successful_task": {
            "model": {
                "cold": cold_success_tokens,
                "warm": warm_success_tokens,
            },
            "model_plus_retrieval": {
                "cold": cold_end_to_end_success_tokens,
                "warm": warm_end_to_end_success_tokens,
            },
        },
        "memory_context_tokens": {
            "total": sum(result.memory_context_tokens for result in results),
            "mean": sum(result.memory_context_tokens for result in results) / count,
            "max": max(result.memory_context_tokens for result in results),
        },
        "wall_latency_ms": {
            "cold": _stats([result.cold_latency_ms for result in results]),
            "warm": _stats([result.warm_latency_ms for result in results]),
            "warm_minus_cold_mean": (
                sum(result.warm_latency_ms for result in results) / count
                - sum(result.cold_latency_ms for result in results) / count
            ),
        },
        "cases": [
            {
                "case_id": result.case_id,
                "cold_task_success": result.cold_task_success,
                "warm_task_success": result.warm_task_success,
                "relevant_memory_ids": sorted(result.relevant_memory_ids),
                "selected_memory_ids": list(result.selected_memory_ids),
                "retrieval_tokens": result.retrieval_tokens,
                "retrieval_latency_ms": result.retrieval_latency_ms,
                "cold_input_tokens": result.cold_input_tokens,
                "cold_output_tokens": result.cold_output_tokens,
                "warm_input_tokens": result.warm_input_tokens,
                "warm_output_tokens": result.warm_output_tokens,
                "cold_latency_ms": result.cold_latency_ms,
                "warm_latency_ms": result.warm_latency_ms,
                "memory_context_tokens": result.memory_context_tokens,
                "cold_answer": result.cold_answer,
                "warm_answer": result.warm_answer,
                "behavior_changed": (
                    result.cold_answer != result.warm_answer
                    if result.cold_answer is not None and result.warm_answer is not None
                    else None
                ),
                "unrelated_case": result.unrelated_case,
            }
            for result in results
        ],
    }
