from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
import json
import math
import os
from pathlib import Path
import re
import time
from typing import Any
import uuid

from coding_agent.application import AgentApplication
from coding_agent.context import BudgetedContextBuilder
from coding_agent.domain import Event, EventType, RunPolicy, RuntimeState
from coding_agent.evaluation import (
    EVAL_SCHEMA_VERSION,
    EvalCase,
    EvalInfrastructureFailure,
    EvalValidationError,
    EvaluationRunner,
    collect_run_metrics,
    resolve_contained,
)
from coding_agent.memory.domain import MemoryKind, MemoryQuery, MemoryScope
from coding_agent.memory.policy import MemoryWriteContext
from coding_agent.memory.retrieval import LexicalMemoryRetriever
from coding_agent.memory.service import JournalProvenanceValidator, MemoryService
from coding_agent.memory.sqlite import SQLiteMemoryStore
from coding_agent.models.base import ModelBackend
from coding_agent.models.scripted import ScriptedBackend
from coding_agent.workspace import tree_fingerprint


LIVE_MEMORY_SCHEMA_VERSION = 1


@dataclass(frozen=True)
class LiveMemorySeed:
    memory_id: str
    content: str
    scope: MemoryScope
    scope_id: str
    kind: MemoryKind = MemoryKind.SEMANTIC
    confidence: float = 1.0
    repository_revision: str | None = None
    relevant: bool = True


@dataclass(frozen=True)
class LiveMemoryCase:
    evaluation: EvalCase
    memories: tuple[LiveMemorySeed, ...]
    user_id: str | None
    repository_id: str | None
    session_id: str | None
    repository_revision: str | None
    top_k: int
    token_budget: int
    relevant_tools: frozenset[str]


@dataclass(frozen=True)
class LiveMemorySuite:
    schema_version: int
    cases: tuple[LiveMemoryCase, ...]


def _mapping(value: object, description: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise EvalValidationError(f"{description} must be an object")
    return value


def load_live_memory_suite(path: str | Path) -> tuple[LiveMemorySuite, Path]:
    manifest = Path(path).resolve(strict=True)
    raw = json.loads(manifest.read_text(encoding="utf-8"))
    root = _mapping(raw, "live memory suite")
    if set(root) != {"schema_version", "cases"}:
        raise EvalValidationError("live memory suite requires only schema_version and cases")
    if int(root["schema_version"]) != LIVE_MEMORY_SCHEMA_VERSION:
        raise EvalValidationError("unsupported live memory suite schema version")
    raw_cases = root["cases"]
    if not isinstance(raw_cases, list) or not raw_cases:
        raise EvalValidationError("live memory suite requires at least one case")
    cases = tuple(_parse_case(item) for item in raw_cases)
    identifiers = [case.evaluation.case_id for case in cases]
    if len(identifiers) != len(set(identifiers)):
        raise EvalValidationError("live memory case ids must be unique")
    return LiveMemorySuite(LIVE_MEMORY_SCHEMA_VERSION, cases), manifest.parent


def _parse_case(value: object) -> LiveMemoryCase:
    raw = _mapping(value, "live memory case")
    allowed = {
        "schema_version", "case_id", "fixture", "task", "policy", "required_facts",
        "oracles", "case_type", "memories", "query", "relevant_tools",
    }
    unknown = sorted(set(raw) - allowed)
    if unknown:
        raise EvalValidationError(f"live memory case contains unknown fields: {', '.join(unknown)}")
    evaluation_raw = {
        key: raw[key]
        for key in (
            "schema_version", "case_id", "fixture", "task", "policy",
            "required_facts", "oracles", "case_type",
        )
        if key in raw
    }
    evaluation_raw.setdefault("schema_version", EVAL_SCHEMA_VERSION)
    # The paired runner supplies the only backend. This placeholder exists solely
    # to reuse the versioned oracle/policy validation contract.
    evaluation_raw["backend"] = {"kind": "scripted", "responses": [{"final": "unused"}]}
    evaluation = EvalCase.from_dict(evaluation_raw)
    raw_memories = raw.get("memories")
    if not isinstance(raw_memories, list) or not raw_memories:
        raise EvalValidationError("live memory case requires at least one memory")
    memories = tuple(_parse_memory(item) for item in raw_memories)
    memory_ids = [seed.memory_id for seed in memories]
    if len(memory_ids) != len(set(memory_ids)):
        raise EvalValidationError("memory ids must be unique within a case")
    query = _mapping(raw.get("query", {}), "memory query")
    allowed_query = {
        "user_id", "repository_id", "session_id", "repository_revision", "top_k", "token_budget"
    }
    if set(query) - allowed_query:
        raise EvalValidationError("memory query contains unknown fields")
    top_k = int(query.get("top_k", 5))
    token_budget = int(query.get("token_budget", 512))
    # Let MemoryQuery perform the canonical range checks too.
    MemoryQuery(text=evaluation.task, top_k=top_k, token_budget=token_budget)
    raw_tools = raw.get("relevant_tools", [])
    if not isinstance(raw_tools, list) or any(not isinstance(item, str) or not item for item in raw_tools):
        raise EvalValidationError("relevant_tools must be an array of non-empty strings")
    return LiveMemoryCase(
        evaluation=evaluation,
        memories=memories,
        user_id=_optional_text(query.get("user_id")),
        repository_id=_optional_text(query.get("repository_id")),
        session_id=_optional_text(query.get("session_id")),
        repository_revision=_optional_text(query.get("repository_revision")),
        top_k=top_k,
        token_budget=token_budget,
        relevant_tools=frozenset(raw_tools),
    )


def _optional_text(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        raise EvalValidationError("scope identifiers cannot be empty")
    return text


def _parse_memory(value: object) -> LiveMemorySeed:
    raw = _mapping(value, "memory seed")
    allowed = {
        "memory_id", "content", "scope", "scope_id", "kind", "confidence",
        "repository_revision", "relevant",
    }
    if set(raw) - allowed:
        raise EvalValidationError("memory seed contains unknown fields")
    memory_id = str(raw.get("memory_id", "")).strip()
    content = str(raw.get("content", "")).strip()
    scope_id = str(raw.get("scope_id", "")).strip()
    if not memory_id or not content or not scope_id:
        raise EvalValidationError("memory_id, content and scope_id are required")
    confidence = float(raw.get("confidence", 1.0))
    if not 0.0 <= confidence <= 1.0:
        raise EvalValidationError("memory confidence must be between zero and one")
    try:
        scope = MemoryScope(str(raw.get("scope", "user")))
        kind = MemoryKind(str(raw.get("kind", "semantic")))
    except ValueError as exc:
        raise EvalValidationError(str(exc)) from exc
    revision = _optional_text(raw.get("repository_revision"))
    if scope is MemoryScope.REPOSITORY and revision is None:
        raise EvalValidationError("repository memory requires repository_revision")
    return LiveMemorySeed(
        memory_id=memory_id,
        content=content,
        scope=scope,
        scope_id=scope_id,
        kind=kind,
        confidence=confidence,
        repository_revision=revision,
        relevant=bool(raw.get("relevant", True)),
    )


def _percentile(values: Sequence[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, max(0, math.ceil(len(ordered) * fraction) - 1))]


def _distribution(values: Sequence[float]) -> dict[str, float | int | None]:
    return {
        "count": len(values),
        "total": sum(values),
        "mean": sum(values) / len(values) if values else None,
        "p50": _percentile(values, 0.50),
        "p95": _percentile(values, 0.95),
    }


def _timestamp(event: Event) -> float | None:
    from datetime import datetime

    try:
        return datetime.fromisoformat(event.timestamp).timestamp()
    except (TypeError, ValueError, OverflowError):
        return None


def _first_relevant_action(events: Sequence[Event], tools: frozenset[str]) -> dict[str, Any]:
    if not tools:
        return {"configured": False, "found": None, "elapsed_ms": None, "tool_attempts_before": None}
    start = _timestamp(events[0]) if events else None
    attempts = 0
    for event in events:
        if event.event_type is EventType.TOOL_CALL_PREPARED:
            raw_call = event.payload.get("call")
            payload_name = (
                str(raw_call.get("name", ""))
                if isinstance(raw_call, Mapping)
                else str(event.payload.get("tool_name", event.payload.get("name", "")))
            )
            if payload_name in tools:
                finish = _timestamp(event)
                return {
                    "configured": True,
                    "found": payload_name,
                    "elapsed_ms": max(0.0, (finish - start) * 1000) if start is not None and finish is not None else None,
                    "tool_attempts_before": attempts,
                }
            attempts += 1
    return {"configured": True, "found": None, "elapsed_ms": None, "tool_attempts_before": attempts}


def _source_unchanged(events: Sequence[Event]) -> bool | None:
    for event in reversed(events):
        if event.event_type is EventType.RUN_FINISHED:
            value = event.payload.get("source_unchanged")
            return value if isinstance(value, bool) else None
    return None


class LiveMemoryABRunner:
    """Paired provider evaluation where validated Memory injection is the sole arm variable."""

    def __init__(
        self,
        agent_home: str | Path,
        *,
        suite_root: str | Path,
        provider: str,
        model: str,
        backend_factory: Callable[[], ModelBackend],
        allow_non_live_backend: bool = False,
    ):
        if provider not in {"openai-compatible", "anthropic"} and not allow_non_live_backend:
            raise ValueError("live Memory A/B requires a real provider adapter")
        if not provider.strip() or not model.strip():
            raise ValueError("provider and model are required")
        self.agent_home = Path(agent_home).resolve()
        self.suite_root = Path(suite_root).resolve(strict=True)
        self.provider = provider
        self.model = model
        self.backend_factory = backend_factory
        self.oracle_runner = EvaluationRunner(self.agent_home, suite_root=self.suite_root)

    def run(
        self,
        suite: LiveMemorySuite,
        *,
        repetitions: int = 1,
        output_dir: str | Path | None = None,
    ) -> dict[str, Any]:
        if repetitions <= 0:
            raise ValueError("repetitions must be positive")
        output = Path(output_dir).resolve() if output_dir is not None else self.agent_home / "memory-live-evals" / str(uuid.uuid4())
        observations: list[dict[str, Any]] = []
        pair_orders: list[dict[str, Any]] = []
        for case_index, case in enumerate(suite.cases):
            for repetition in range(1, repetitions + 1):
                order = ("off", "on") if (case_index + repetition) % 2 else ("on", "off")
                pair_orders.append({"case_id": case.evaluation.case_id, "repetition": repetition, "order": list(order)})
                observations.extend(self._run_pair(case, repetition, order, output))
        report = self._report(suite, repetitions, observations, pair_orders)
        self._assert_safe_report(report, output)
        output.mkdir(parents=True, exist_ok=True)
        (output / "report.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        return report

    def _run_pair(
        self,
        case: LiveMemoryCase,
        repetition: int,
        order: Sequence[str],
        output: Path,
    ) -> list[dict[str, Any]]:
        fixture = resolve_contained(self.suite_root, case.evaluation.fixture, description="fixture")
        fingerprint = tree_fingerprint(fixture)
        pair_root = output / "runs" / case.evaluation.case_id / str(repetition)
        provenance_app = AgentApplication(pair_root / "provenance")
        store = SQLiteMemoryStore(pair_root / "memory.db")
        try:
            service = MemoryService(
                store,
                provenance_validator=JournalProvenanceValidator(provenance_app.journal),
            )
            provenance = [self._seed_memory(provenance_app, service, fixture, case, seed) for seed in case.memories]
            observations = []
            for arm in order:
                observations.append(
                    self._run_arm(
                        case,
                        repetition,
                        arm,
                        fixture,
                        fingerprint,
                        pair_root / arm,
                        store,
                        provenance,
                    )
                )
            return observations
        finally:
            store.close()
            provenance_app.close()

    def _seed_memory(
        self,
        application: AgentApplication,
        service: MemoryService,
        fixture: Path,
        case: LiveMemoryCase,
        seed: LiveMemorySeed,
    ) -> dict[str, Any]:
        source_session = f"memory-source-{case.evaluation.case_id}-{seed.memory_id}-{uuid.uuid4().hex}"
        result = application.run_task(
            source=fixture,
            task="Record the validated outcome of this completed Runtime task.",
            backend=ScriptedBackend([{"final": seed.content}]),
            policy=RunPolicy(max_steps=4, max_model_calls=1, max_tool_calls=0),
            session_id=source_session,
            manage_signals=False,
        )
        if result.state is not RuntimeState.COMPLETED or result.final_answer != seed.content:
            raise EvalInfrastructureFailure("Memory seed is not identical to its prior Runtime result")
        events = tuple(application.journal.list_events(result.session_id))
        finished = next((event for event in reversed(events) if event.event_type is EventType.RUN_FINISHED), None)
        if finished is None:
            raise EvalInfrastructureFailure("Memory seed Runtime has no committed completion event")
        context = self._write_context(case, seed)
        proposed = service.propose(
            context=context,
            scope=seed.scope,
            kind=seed.kind,
            content=result.final_answer,
            source_run_id=result.session_id,
            source_agent_id="runtime",
            source_event_refs=(finished.event_id,),
            confidence=seed.confidence,
            repository_revision=seed.repository_revision,
            memory_id=seed.memory_id,
        )
        active = service.activate(proposed.memory_id, context=context)
        return {
            "memory_id": active.memory_id,
            "source_run_id": result.session_id,
            "source_event_refs": [finished.event_id],
            "record_version": active.version,
            "schema_version": active.schema_version,
            "verified_runtime_result": True,
        }

    @staticmethod
    def _write_context(case: LiveMemoryCase, seed: LiveMemorySeed) -> MemoryWriteContext:
        return MemoryWriteContext(
            actor_id="live-memory-evaluation",
            session_id=seed.scope_id if seed.scope is MemoryScope.SESSION else case.session_id,
            repository_id=seed.scope_id if seed.scope is MemoryScope.REPOSITORY else case.repository_id,
            user_id=seed.scope_id if seed.scope is MemoryScope.USER else case.user_id,
            allowed_scopes=frozenset({seed.scope}),
        )

    def _run_arm(
        self,
        case: LiveMemoryCase,
        repetition: int,
        arm: str,
        fixture: Path,
        source_fingerprint: str,
        run_home: Path,
        store: SQLiteMemoryStore,
        provenance: Sequence[dict[str, Any]],
    ) -> dict[str, Any]:
        retrieval_prefix = f"retrieval-{case.evaluation.case_id}-{repetition}"
        if arm == "on":
            retriever = LexicalMemoryRetriever(
                store,
                id_factory=lambda: f"{retrieval_prefix}-{uuid.uuid4().hex}",
            )
            builder = BudgetedContextBuilder(
                memory_retriever=retriever,
                memory_query_factory=lambda request: MemoryQuery(
                    text=request.task,
                    user_id=case.user_id,
                    repository_id=case.repository_id,
                    session_id=case.session_id,
                    repository_revision=case.repository_revision,
                    top_k=case.top_k,
                    token_budget=case.token_budget,
                ),
            )
        else:
            builder = BudgetedContextBuilder()
        application = AgentApplication(run_home, context_builder=builder)
        wall_started = time.perf_counter()
        try:
            result = application.run_task(
                source=fixture,
                task=case.evaluation.task,
                backend=self.backend_factory(),
                policy=RunPolicy.from_dict(case.evaluation.policy),
                manage_signals=False,
            )
            events = tuple(application.journal.list_events(result.session_id))
            metrics = collect_run_metrics(events)
            metrics["end_to_end_latency_ms"] = max(0.0, (time.perf_counter() - wall_started) * 1000)
            metrics["first_relevant_action"] = _first_relevant_action(events, case.relevant_tools)
            oracles = tuple(
                self.oracle_runner._run_oracle(
                    application, result, oracle, case.evaluation.case_id, self.suite_root
                )
                for oracle in case.evaluation.oracles
            )
            if any(oracle.infrastructure_failure for oracle in oracles):
                raise EvalInfrastructureFailure("trusted oracle infrastructure failure")
            oracle_success = all(oracle.passed for oracle in oracles)
            memory_manifest = self._memory_manifest(events)
            scope_leakage = self._scope_leakage(case, memory_manifest)
            records = (memory_manifest or {}).get("records", [])
            selected_memory_ids = [
                str(record["memory_id"])
                for record in records
                if isinstance(record, Mapping) and "memory_id" in record
            ] if isinstance(records, list) else []
            relevant_memory_ids = [seed.memory_id for seed in case.memories if seed.relevant]
            return {
                "case_id": case.evaluation.case_id,
                "repetition": repetition,
                "arm": arm,
                "oracle_success": oracle_success,
                "runtime_completion": result.state is RuntimeState.COMPLETED,
                "end_to_end_success": oracle_success and result.state is RuntimeState.COMPLETED,
                "source_invariant": _source_unchanged(events) is True and tree_fingerprint(fixture) == source_fingerprint,
                "source_revision": source_fingerprint,
                "permission_violation": int(metrics["permission_violations"]),
                "scope_leakage": scope_leakage,
                "retrieval_quality": {
                    "relevant_memory_ids": relevant_memory_ids,
                    "selected_memory_ids": selected_memory_ids,
                },
                "metrics": metrics,
                "memory": memory_manifest,
                "provenance": list(provenance) if arm == "on" else [],
                "oracles": [{"kind": oracle.kind, "passed": oracle.passed} for oracle in oracles],
            }
        finally:
            application.close()

    @staticmethod
    def _memory_manifest(events: Sequence[Event]) -> dict[str, Any] | None:
        manifests = [event.payload.get("memory") for event in events if event.event_type is EventType.CONTEXT_BUILT]
        selected = [item for item in manifests if isinstance(item, Mapping)]
        if not selected:
            return None
        # The final audited build is authoritative. Preview builds never emit an audit id.
        return dict(selected[-1])

    @staticmethod
    def _scope_leakage(case: LiveMemoryCase, manifest: Mapping[str, Any] | None) -> int:
        if manifest is None:
            return 0
        records = manifest.get("records", [])
        leaks = 0
        for record in records if isinstance(records, list) else []:
            if not isinstance(record, Mapping):
                leaks += 1
                continue
            seed = next((item for item in case.memories if item.memory_id == record.get("memory_id")), None)
            if seed is None:
                leaks += 1
            elif seed.scope is MemoryScope.USER and seed.scope_id != case.user_id:
                leaks += 1
            elif seed.scope is MemoryScope.SESSION and seed.scope_id != case.session_id:
                leaks += 1
            elif seed.scope is MemoryScope.REPOSITORY and (
                seed.scope_id != case.repository_id or seed.repository_revision != case.repository_revision
            ):
                leaks += 1
        return leaks

    def _report(
        self,
        suite: LiveMemorySuite,
        repetitions: int,
        observations: Sequence[dict[str, Any]],
        pair_orders: Sequence[dict[str, Any]],
    ) -> dict[str, Any]:
        arms = {name: [item for item in observations if item["arm"] == name] for name in ("off", "on")}
        aggregates = {name: self._aggregate(values) for name, values in arms.items()}
        pairs = []
        keyed = {(item["case_id"], item["repetition"], item["arm"]): item for item in observations}
        for case in suite.cases:
            for repetition in range(1, repetitions + 1):
                off = keyed[(case.evaluation.case_id, repetition, "off")]
                on = keyed[(case.evaluation.case_id, repetition, "on")]
                pairs.append({
                    "case_id": case.evaluation.case_id,
                    "repetition": repetition,
                    "off_success": off["end_to_end_success"],
                    "on_success": on["end_to_end_success"],
                    "total_token_delta": on["metrics"]["total_tokens"] - off["metrics"]["total_tokens"],
                    "retrieval_tokens": (on["memory"] or {}).get("retrieval_token_cost", 0),
                })
        return {
            "benchmark": "paired-live-memory-provider-ab",
            "schema_version": 1,
            "provider": self.provider,
            "model": self.model,
            "pair_count": len(pairs),
            "only_arm_variable": "validated_memory_context",
            "controlled_variables": [
                "provider",
                "model",
                "task",
                "RunPolicy budget",
                "source revision",
                "trusted oracle",
            ],
            "pair_orders": list(pair_orders),
            "arms": aggregates,
            "pairs": pairs,
            "runs": list(observations),
            "safety": {
                "provider_reasoning_persisted": False,
                "secret_values_persisted": False,
                "absolute_paths_persisted": False,
                "memory_default_runtime_path_changed": False,
            },
        }

    @staticmethod
    def _aggregate(values: Sequence[dict[str, Any]]) -> dict[str, Any]:
        successes = sum(bool(item["end_to_end_success"]) for item in values)
        metric_names = (
            "input_tokens", "output_tokens", "total_tokens", "end_to_end_latency_ms",
            "tool_attempts", "tool_executions", "repeated_failure_batches", "invalid_tool_calls",
        )
        metrics = {
            name: _distribution([float(item["metrics"][name]) for item in values])
            for name in metric_names
        }
        retrieval = [float((item["memory"] or {}).get("retrieval_token_cost", 0)) for item in values]
        context_tokens = [
            float((item["memory"] or {}).get("actual_context_token_cost", 0))
            for item in values
        ]
        relevant_elapsed = [
            float(item["metrics"]["first_relevant_action"]["elapsed_ms"])
            for item in values
            if item["metrics"]["first_relevant_action"]["elapsed_ms"] is not None
        ]
        relevant_attempts = [
            float(item["metrics"]["first_relevant_action"]["tool_attempts_before"])
            for item in values
            if item["metrics"]["first_relevant_action"]["tool_attempts_before"] is not None
        ]
        total_tokens = sum(int(item["metrics"]["total_tokens"]) for item in values)
        retrieval_total = sum(retrieval)
        relevant_total = sum(
            len(item["retrieval_quality"]["relevant_memory_ids"])
            for item in values
        )
        recalled_total = sum(
            len(
                set(item["retrieval_quality"]["relevant_memory_ids"])
                & set(item["retrieval_quality"]["selected_memory_ids"])
            )
            for item in values
        )
        selected_total = sum(
            len(item["retrieval_quality"]["selected_memory_ids"])
            for item in values
        )
        return {
            "run_count": len(values),
            "oracle_success": sum(bool(item["oracle_success"]) for item in values),
            "runtime_completion": sum(bool(item["runtime_completion"]) for item in values),
            "end_to_end_success": successes,
            "end_to_end_success_rate": successes / len(values) if values else None,
            "tokens_per_successful_task": {
                "model": total_tokens / successes if successes else None,
                "model_plus_retrieval": (
                    (total_tokens + retrieval_total) / successes if successes else None
                ),
            },
            "metrics": metrics,
            "retrieval_tokens": _distribution(retrieval),
            "memory_context_tokens": _distribution(context_tokens),
            "retrieval_relevant_recall": (
                recalled_total / relevant_total if relevant_total else 1.0
            ),
            "irrelevant_injection_rate": (
                (selected_total - recalled_total) / selected_total
                if selected_total
                else 0.0
            ),
            "latency_ms": metrics["end_to_end_latency_ms"],
            "time_before_first_relevant_action_ms": _distribution(relevant_elapsed),
            "tool_calls_before_first_relevant_action": _distribution(relevant_attempts),
            "source_invariant_failures": sum(not bool(item["source_invariant"]) for item in values),
            "permission_violations": sum(int(item["permission_violation"]) for item in values),
            "scope_leakage": sum(int(item["scope_leakage"]) for item in values),
        }

    @staticmethod
    def _assert_safe_report(report: Mapping[str, Any], output: Path) -> None:
        encoded = json.dumps(report, ensure_ascii=False, sort_keys=True)
        absolute_candidates = {str(output), str(output.parent)}
        if any(candidate and candidate in encoded for candidate in absolute_candidates):
            raise EvalInfrastructureFailure("absolute path would enter live Memory report")
        if re.search(r"(?<![A-Za-z0-9_.-])(?:/[A-Za-z0-9_.-]+){2,}(?:/|\b)", encoded):
            raise EvalInfrastructureFailure("host absolute path would enter live Memory report")
        if re.search(r"(?i)\b[A-Z]:\\(?:[^\\\s]+\\)+[^\\\s]+", encoded):
            raise EvalInfrastructureFailure("host absolute path would enter live Memory report")
        for name, value in os.environ.items():
            if ("KEY" in name or "TOKEN" in name or "SECRET" in name) and len(value) >= 8 and value in encoded:
                raise EvalInfrastructureFailure("secret value would enter live Memory report")
