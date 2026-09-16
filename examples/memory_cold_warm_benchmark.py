"""Run the deterministic P2-M2 Memory cold/warm benchmark.

The benchmark deliberately wires Memory into a custom ``BudgetedContextBuilder``.  The
default ``AgentApplication`` and headless composition remain memory-disabled.
"""

from __future__ import annotations

from dataclasses import dataclass
from math import ceil
import json
from pathlib import Path
import tempfile
import time

from coding_agent.application import AgentApplication
from coding_agent.domain import ModelRequest, ModelResponse, RunPolicy, Usage
from coding_agent.evaluation import collect_run_metrics
from coding_agent.memory.domain import MemoryKind, MemoryQuery, MemoryScope, MemoryRecord
from coding_agent.memory.evaluation import MemoryPairResult, summarize_memory_pairs
from coding_agent.memory.policy import MemoryWriteContext
from coding_agent.memory.retrieval import LexicalMemoryRetriever
from coding_agent.memory.service import MemoryService
from coding_agent.memory.sqlite import SQLiteMemoryStore
from coding_agent.context import BudgetedContextBuilder


BENCHMARK_USER = "memory-benchmark-user"
BENCHMARK_NOW = "2026-09-16T00:00:00+00:00"


@dataclass(frozen=True)
class BenchmarkCase:
    case_id: str
    task: str
    expected_fact: str
    answer: str
    distractor: str


CASES = (
    BenchmarkCase(
        case_id="deployment-color",
        task="What deploy color is required for this user?",
        expected_fact="The user's deploy color is BLUE.",
        answer="BLUE",
        distractor="The user's deploy region is EAST.",
    ),
    BenchmarkCase(
        case_id="invoice-rounding",
        task="How should invoice totals be rounded?",
        expected_fact="Invoice totals must be rounded to two decimal places.",
        answer="two decimal places",
        distractor="Invoice labels use title case.",
    ),
    BenchmarkCase(
        case_id="parser-blank-input",
        task="What should the parser do for blank input?",
        expected_fact="The parser ignores blank input and returns no item.",
        answer="ignore it",
        distractor="Parser errors use a stable code.",
    ),
)


class MemoryAwareBackend:
    """A deterministic task oracle backend; usage is a synthetic token estimate."""

    name = "scripted"

    def __init__(self, case: BenchmarkCase):
        self.case = case

    def complete(self, request: ModelRequest) -> ModelResponse:
        context = "\n".join(message.content for message in request.messages)
        answer = self.case.answer if self.case.expected_fact in context else "UNKNOWN"
        input_tokens = sum(
            max(1, ceil(len(message.content.encode("utf-8")) / 4)) + 4
            for message in request.messages
        )
        return ModelResponse(
            text=answer,
            usage=Usage(input_tokens=input_tokens, output_tokens=2),
        )


def _seed_memory(
    store: SQLiteMemoryStore,
) -> dict[str, MemoryRecord]:
    valid_provenance: set[tuple[str, tuple[str, ...]]] = set()
    service = MemoryService(
        store,
        clock=lambda: BENCHMARK_NOW,
        provenance_validator=lambda record: (
            record.source_run_id,
            record.source_event_refs,
        )
        in valid_provenance,
    )
    context = MemoryWriteContext(
        actor_id="benchmark",
        user_id=BENCHMARK_USER,
        allowed_scopes=frozenset({MemoryScope.USER}),
    )
    records: dict[str, MemoryRecord] = {}
    for case in CASES:
        run_id = f"seed-{case.case_id}"
        event_refs = (f"seed-event-{case.case_id}",)
        valid_provenance.add((run_id, event_refs))
        for role, content in (("relevant", case.expected_fact), ("distractor", case.distractor)):
            memory_id = f"memory-{case.case_id}-{role}"
            proposed = service.propose(
                context=context,
                scope=MemoryScope.USER,
                kind=MemoryKind.SEMANTIC,
                content=content,
                source_run_id=run_id,
                source_agent_id="runtime",
                source_event_refs=event_refs,
                confidence=0.9,
                memory_id=memory_id,
            )
            records[memory_id] = service.activate(proposed.memory_id, context=context)
    return records


def _run_case(
    *,
    root: Path,
    source: Path,
    case: BenchmarkCase,
    context_builder: BudgetedContextBuilder,
) -> tuple[object, dict[str, object], float]:
    application = AgentApplication(
        root,
        context_builder=context_builder,
    )
    started = time.perf_counter()
    try:
        result = application.run_task(
            source=source,
            task=case.task,
            backend=MemoryAwareBackend(case),
            policy=RunPolicy(
                max_steps=8,
                max_model_calls=1,
                max_tool_calls=0,
                max_output_tokens=32,
            ),
            session_id=f"{case.case_id}-{root.name}",
            manage_signals=False,
        )
        journal = application.journal
        if journal is None:
            raise RuntimeError("benchmark application did not create a SQLite journal")
        metrics = collect_run_metrics(journal.list_events(result.session_id))
        elapsed_ms = (time.perf_counter() - started) * 1000.0
        return result, metrics, elapsed_ms
    finally:
        application.close()


def run_benchmark() -> dict[str, object]:
    """Run all fixed pairs and return a sanitized, JSON-serializable report."""

    with tempfile.TemporaryDirectory(prefix="coding-agent-memory-benchmark-") as temporary:
        root = Path(temporary)
        source = root / "source"
        source.mkdir()
        (source / "README.md").write_text("Deterministic Memory benchmark fixture.\n", encoding="utf-8")
        store = SQLiteMemoryStore(root / "memory.db", clock=lambda: BENCHMARK_NOW)
        try:
            _seed_memory(store)
            pairs: list[MemoryPairResult] = []
            for case in CASES:
                cold_result, cold_metrics, cold_latency = _run_case(
                    root=root / f"cold-{case.case_id}",
                    source=source,
                    case=case,
                    context_builder=BudgetedContextBuilder(),
                )
                retrieval_id = f"retrieval-{case.case_id}"
                retriever = LexicalMemoryRetriever(
                    store,
                    clock=lambda: BENCHMARK_NOW,
                    id_factory=lambda retrieval_id=retrieval_id: retrieval_id,
                )
                warm_builder = BudgetedContextBuilder(
                    memory_retriever=retriever,
                    memory_query_factory=lambda request, case=case: MemoryQuery(
                        text=request.task,
                        user_id=BENCHMARK_USER,
                        top_k=2,
                        token_budget=256,
                    ),
                )
                warm_result, warm_metrics, warm_latency = _run_case(
                    root=root / f"warm-{case.case_id}",
                    source=source,
                    case=case,
                    context_builder=warm_builder,
                )
                retrieval = store.get_retrieval(retrieval_id)
                if retrieval is None:
                    raise RuntimeError(f"missing retrieval audit for {case.case_id}")
                selected = tuple(
                    str(record["memory_id"]) for record in retrieval["selected"]
                )
                pairs.append(
                    MemoryPairResult(
                        case_id=case.case_id,
                        cold_task_success=cold_result.final_answer == case.answer,
                        warm_task_success=warm_result.final_answer == case.answer,
                        relevant_memory_ids=frozenset(
                            {f"memory-{case.case_id}-relevant"}
                        ),
                        selected_memory_ids=selected,
                        retrieval_tokens=int(retrieval["token_cost"]),
                        retrieval_latency_ms=float(retrieval["duration_ms"]),
                        cold_input_tokens=int(cold_metrics["input_tokens"]),
                        cold_output_tokens=int(cold_metrics["output_tokens"]),
                        warm_input_tokens=int(warm_metrics["input_tokens"]),
                        warm_output_tokens=int(warm_metrics["output_tokens"]),
                        cold_latency_ms=cold_latency,
                        warm_latency_ms=warm_latency,
                    )
                )
            summary = summarize_memory_pairs(tuple(pairs))
            return {
                "benchmark": "p2-m2-memory-cold-warm",
                "schema_version": 1,
                "implementation": {
                    "memory": "explicit_python_composition",
                    "default_application_enabled": False,
                    "headless_enabled": False,
                },
                "case_ids": [case.case_id for case in CASES],
                "summary": summary,
                "notes": [
                    "Cold uses the default AgentApplication context builder; warm explicitly injects Memory.",
                    "The backend is a deterministic trusted task oracle, not a Provider quality evaluation.",
                    "Model usage is a synthetic scripted estimate; retrieval cost is reported separately.",
                    "Irrelevant injection means selected memory ids outside the case relevant set.",
                ],
            }
        finally:
            store.close()


def main() -> int:
    print(json.dumps(run_benchmark(), ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
