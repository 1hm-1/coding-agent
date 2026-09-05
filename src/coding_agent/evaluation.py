from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
import json
import math
from pathlib import Path
import subprocess
from typing import Any, Callable, Mapping, Sequence
import uuid

from coding_agent.application import AgentApplication
from coding_agent.compression import CompressionEngine
from coding_agent.context import BudgetedContextBuilder, PassthroughContextBuilder
from coding_agent.domain import (
    Event,
    EventType,
    Permission,
    RunPolicy,
    RunResult,
    RuntimeState,
    ToolCall,
    ToolStatus,
)
from coding_agent.models.anthropic import AnthropicBackend
from coding_agent.models.base import ModelBackend
from coding_agent.models.openai_compatible import OpenAICompatibleBackend
from coding_agent.models.scripted import ScriptedBackend
from coding_agent.tools.base import ToolContext
from coding_agent.workspace import file_content_hash, tree_fingerprint


class EvalValidationError(ValueError):
    """An eval manifest is invalid and no run should be counted."""


class EvalInfrastructureFailure(RuntimeError):
    """The eval fixture or oracle is invalid, not an Agent task failure."""


class _InjectedEvalCrash(BaseException):
    pass


EVAL_SCHEMA_VERSION = 1
_CASE_FIELDS = {
    "schema_version",
    "case_id",
    "fixture",
    "task",
    "backend",
    "policy",
    "required_facts",
    "oracles",
}
_BACKEND_FIELDS = {
    "kind",
    "fixture",
    "responses",
    "model",
    "base_url",
    "api_key_env",
    "timeout",
    "compression_fixture",
    "compression_responses",
    "fault_stage",
    "resume",
}
_ORACLE_FIELDS = {
    "test_profile": {"kind", "profile"},
    "file": {"kind", "path", "exists", "sha256", "contains", "not_contains"},
    "changed_paths": {"kind", "allow", "deny"},
    "result_schema": {"kind", "required"},
}


def _strict_fields(raw: Mapping[str, Any], allowed: set[str], description: str) -> None:
    unknown = sorted(set(raw) - allowed)
    if unknown:
        raise EvalValidationError(
            f"{description} contains unknown fields: {', '.join(unknown)}"
        )


@dataclass(frozen=True)
class EvalCase:
    schema_version: int
    case_id: str
    fixture: str
    task: str
    backend: dict[str, Any]
    policy: dict[str, Any]
    required_facts: tuple[str, ...]
    oracles: tuple[dict[str, Any], ...]

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "EvalCase":
        if not isinstance(raw, Mapping):
            raise EvalValidationError("eval case must be an object")
        _strict_fields(raw, _CASE_FIELDS, "eval case")
        version = int(raw.get("schema_version", -1))
        if version != EVAL_SCHEMA_VERSION:
            raise EvalValidationError(f"unsupported eval case schema version: {version}")
        case_id = str(raw.get("case_id", ""))
        fixture = str(raw.get("fixture", ""))
        task = str(raw.get("task", ""))
        if not case_id or not case_id.replace("-", "").replace("_", "").isalnum():
            raise EvalValidationError("eval case_id is invalid")
        if not fixture:
            raise EvalValidationError("eval fixture is required")
        if not task.strip():
            raise EvalValidationError("eval task cannot be empty")
        backend = raw.get("backend")
        policy = raw.get("policy", {})
        required_facts = raw.get("required_facts", [])
        oracles = raw.get("oracles", [])
        if not isinstance(backend, Mapping):
            raise EvalValidationError("eval backend must be an object")
        _strict_fields(backend, _BACKEND_FIELDS, "eval backend")
        backend_dict = dict(backend)
        if not backend_dict.get("kind"):
            raise EvalValidationError("eval backend kind is required")
        if not isinstance(policy, Mapping):
            raise EvalValidationError("eval policy must be an object")
        allowed_policy = set(RunPolicy().to_dict())
        _strict_fields(policy, allowed_policy, "eval policy")
        try:
            normalized_policy = RunPolicy.from_dict(policy).to_dict()
        except (TypeError, ValueError) as exc:
            raise EvalValidationError(f"invalid eval policy: {exc}") from exc
        if not isinstance(required_facts, (list, tuple)) or any(
            not isinstance(fact, str) for fact in required_facts
        ):
            raise EvalValidationError("eval required_facts must be an array of strings")
        if not isinstance(oracles, (list, tuple)) or not oracles:
            raise EvalValidationError("eval case requires at least one oracle")
        normalized_oracles: list[dict[str, Any]] = []
        for oracle in oracles:
            if not isinstance(oracle, Mapping):
                raise EvalValidationError("eval oracle must be an object")
            kind = str(oracle.get("kind", ""))
            if kind not in _ORACLE_FIELDS:
                raise EvalValidationError(f"unknown eval oracle kind: {kind}")
            _strict_fields(oracle, _ORACLE_FIELDS[kind], f"{kind} oracle")
            normalized_oracles.append(dict(oracle))
        return cls(
            schema_version=version,
            case_id=case_id,
            fixture=fixture,
            task=task,
            backend=backend_dict,
            policy=normalized_policy,
            required_facts=tuple(required_facts),
            oracles=tuple(normalized_oracles),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "case_id": self.case_id,
            "fixture": self.fixture,
            "task": self.task,
            "backend": dict(self.backend),
            "policy": dict(self.policy),
            "required_facts": list(self.required_facts),
            "oracles": [dict(oracle) for oracle in self.oracles],
        }


@dataclass(frozen=True)
class EvalSuite:
    schema_version: int
    cases: tuple[EvalCase, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "cases": [case.to_dict() for case in self.cases],
        }


def load_eval_suite(path: str | Path) -> tuple[EvalSuite, Path]:
    manifest_path = Path(path).resolve(strict=True)
    if manifest_path.is_dir():
        candidate = manifest_path / "suite.json"
        if not candidate.is_file():
            raise EvalValidationError("eval suite directory must contain suite.json")
        manifest_path = candidate
    if not manifest_path.is_file():
        raise EvalValidationError("eval suite path must be a JSON file")
    try:
        raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise EvalValidationError(f"cannot load eval suite: {exc}") from exc
    if not isinstance(raw, Mapping):
        raise EvalValidationError("eval suite manifest must be an object")
    if isinstance(raw.get("cases"), list):
        _strict_fields(raw, {"schema_version", "cases"}, "eval suite")
        version = int(raw.get("schema_version", -1))
        cases_raw = raw["cases"]
    else:
        version = int(raw.get("schema_version", -1))
        cases_raw = [raw]
    if version != EVAL_SCHEMA_VERSION:
        raise EvalValidationError(f"unsupported eval suite schema version: {version}")
    cases = tuple(EvalCase.from_dict(case) for case in cases_raw)
    if not cases:
        raise EvalValidationError("eval suite must contain at least one case")
    case_ids = [case.case_id for case in cases]
    if len(case_ids) != len(set(case_ids)):
        raise EvalValidationError("eval case ids must be unique")
    return EvalSuite(version, cases), manifest_path.parent.resolve()


def resolve_contained(root: str | Path, relative: str, *, description: str) -> Path:
    base = Path(root).resolve(strict=True)
    candidate = Path(relative)
    if candidate.is_absolute() or any(part == ".." for part in candidate.parts):
        raise EvalInfrastructureFailure(f"{description} escapes the eval suite root")
    resolved = (base / candidate).resolve(strict=False)
    try:
        resolved.relative_to(base)
    except ValueError as exc:
        raise EvalInfrastructureFailure(f"{description} escapes the eval suite root") from exc
    return resolved


@dataclass(frozen=True)
class OracleResult:
    kind: str
    passed: bool
    details: dict[str, Any]
    infrastructure_failure: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "passed": self.passed,
            "details": dict(self.details),
            "infrastructure_failure": self.infrastructure_failure,
        }


def _parse_timestamp(value: str) -> float | None:
    from datetime import datetime

    try:
        return datetime.fromisoformat(value).timestamp()
    except (TypeError, ValueError, OverflowError):
        return None


def _percentile(values: Sequence[float], percentile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, math.ceil(percentile * len(ordered)) - 1))
    return ordered[index]


def _distribution(values: Sequence[float]) -> dict[str, float | int | None]:
    return {
        "count": len(values),
        "mean": (sum(values) / len(values)) if values else None,
        "p50": _percentile(values, 0.50),
        "p95": _percentile(values, 0.95),
    }


def collect_run_metrics(events: Sequence[Event], *, recovery_triggered: bool = False) -> dict[str, Any]:
    model_calls = sum(event.event_type is EventType.MODEL_CALL_STARTED for event in events)
    tool_calls = sum(
        event.event_type is EventType.TOOL_CALL_PREPARED for event in events
    )
    if not tool_calls:
        tool_calls = sum(event.event_type is EventType.TOOL_CALL_STARTED for event in events)
    input_tokens = 0
    output_tokens = 0
    compression_input_tokens = 0
    compression_output_tokens = 0
    tool_latency_ms = 0.0
    test_latency_ms = 0.0
    model_starts: dict[str, float] = {}
    model_latency_ms: list[float] = []
    compression_started: dict[str, float] = {}
    compression_latency_ms: list[float] = []
    permission_violations = 0
    permission_violations_by_tool: Counter[str] = Counter()
    tool_calls_by_name: Counter[str] = Counter()
    compression_rejections: Counter[str] = Counter()
    failure_reasons: Counter[str] = Counter()
    recovery_events = 0
    for event in events:
        if event.event_type is EventType.MODEL_CALL_STARTED:
            request_id = str(event.payload.get("request_id", ""))
            timestamp = _parse_timestamp(event.timestamp)
            if timestamp is not None:
                model_starts[request_id] = timestamp
        elif event.event_type is EventType.MODEL_CALL_SUCCEEDED:
            usage = event.payload.get("usage")
            if isinstance(usage, Mapping):
                input_tokens += int(usage.get("input_tokens", 0))
                output_tokens += int(usage.get("output_tokens", 0))
            request_id = str(event.payload.get("request_id", ""))
            start = model_starts.get(request_id)
            finish = _parse_timestamp(event.timestamp)
            if start is not None and finish is not None:
                model_latency_ms.append(max(0.0, (finish - start) * 1000))
        elif event.event_type is EventType.MODEL_CALL_FAILED:
            failure_reasons[str(event.payload.get("kind", "model_failure"))] += 1
        elif event.event_type is EventType.TOOL_CALL_FINISHED:
            result = event.payload.get("result")
            if isinstance(result, Mapping):
                tool_name = str(result.get("tool_name", "unknown"))
                tool_calls_by_name[tool_name] += 1
                duration_ms = float(result.get("duration_ms", 0.0))
                tool_latency_ms += duration_ms
                if tool_name == "restricted_test":
                    test_latency_ms += duration_ms
                if str(result.get("status")) == ToolStatus.PERMISSION_DENIED.value:
                    permission_violations += 1
                    permission_violations_by_tool[tool_name] += 1
                if str(result.get("status")) != ToolStatus.SUCCESS.value:
                    error = result.get("error")
                    kind = error.get("kind") if isinstance(error, Mapping) else "tool_failure"
                    failure_reasons[str(kind)] += 1
        elif event.event_type is EventType.COMPRESSION_STARTED:
            key = f"{event.payload.get('source_event_start')}:{event.payload.get('source_event_end')}"
            timestamp = _parse_timestamp(event.timestamp)
            if timestamp is not None:
                compression_started[key] = timestamp
        elif event.event_type is EventType.COMPRESSION_FINISHED:
            usage = event.payload.get("usage")
            if isinstance(usage, Mapping):
                compression_input_tokens += int(usage.get("input_tokens", 0))
                compression_output_tokens += int(usage.get("output_tokens", 0))
            key = f"{event.payload.get('source_event_start')}:{event.payload.get('source_event_end')}"
            start = compression_started.get(key)
            finish = _parse_timestamp(event.timestamp)
            if start is not None and finish is not None:
                compression_latency_ms.append(max(0.0, (finish - start) * 1000))
        elif event.event_type in {
            EventType.TOOL_RESULT_REATTACHED,
            EventType.MODEL_CALL_UNCERTAIN,
            EventType.TOOL_CALL_UNCERTAIN,
            EventType.RESUME_STARTED,
        }:
            recovery_events += 1
        elif event.event_type is EventType.COMPRESSION_REJECTED:
            reason = str(event.payload.get("reason", "rejected"))
            compression_rejections[reason] += 1
            failure_reasons[f"compression:{reason}"] += 1

    run_latency_ms = 0.0
    if events:
        first = _parse_timestamp(events[0].timestamp)
        last = _parse_timestamp(events[-1].timestamp)
        if first is not None and last is not None:
            run_latency_ms = max(0.0, (last - first) * 1000)
    return {
        "model_calls": model_calls,
        "tool_calls": tool_calls,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "compression_input_tokens": compression_input_tokens,
        "compression_output_tokens": compression_output_tokens,
        "latency_ms": run_latency_ms,
        "model_latency_ms": sum(model_latency_ms),
        "tool_latency_ms": tool_latency_ms,
        "test_latency_ms": test_latency_ms,
        "compression_latency_ms": sum(compression_latency_ms),
        "permission_violations": permission_violations,
        "permission_violations_by_tool": dict(sorted(permission_violations_by_tool.items())),
        "tool_calls_by_name": dict(sorted(tool_calls_by_name.items())),
        "compression_rejections": dict(sorted(compression_rejections.items())),
        "recovery_triggered": bool(recovery_triggered or recovery_events),
        "recovery_events": recovery_events,
        "failure_reasons": dict(sorted(failure_reasons.items())),
    }


@dataclass(frozen=True)
class EvalRun:
    case_id: str
    repetition: int
    session_id: str
    state: str
    task_success: bool
    runtime_completed: bool
    source_invariant: bool | None
    valid: bool
    infrastructure_failure: bool
    failure_reason: str | None
    trace_path: str | None
    metrics: dict[str, Any]
    oracles: tuple[OracleResult, ...] = ()

    def to_dict(self, *, include_trace: bool = True) -> dict[str, Any]:
        result = {
            "case_id": self.case_id,
            "repetition": self.repetition,
            "session_id": self.session_id,
            "state": self.state,
            "task_success": self.task_success,
            "runtime_completed": self.runtime_completed,
            "source_invariant": self.source_invariant,
            "valid": self.valid,
            "infrastructure_failure": self.infrastructure_failure,
            "failure_reason": self.failure_reason,
            "metrics": dict(self.metrics),
            "oracles": [oracle.to_dict() for oracle in self.oracles],
        }
        if include_trace:
            result["trace_path"] = self.trace_path
        return result


@dataclass(frozen=True)
class EvaluationReport:
    output_dir: Path
    report: dict[str, Any]
    runs: tuple[EvalRun, ...]

    def to_dict(self) -> dict[str, Any]:
        return dict(self.report)


def _comparison_run_dict(run: EvalRun) -> dict[str, Any]:
    """Return a stable run projection for report comparison.

    Session IDs and filesystem trace paths are intentionally retained only in
    ``runs.jsonl``. They are execution identifiers, not evaluation outcomes,
    and would make the machine-readable comparison report vary for identical
    case/repetition inputs.
    """

    result = run.to_dict(include_trace=False)
    result.pop("session_id", None)
    return result


class EvaluationRunner:
    """Run versioned cases in fresh workspaces and produce deterministic metrics."""

    def __init__(
        self,
        agent_home: str | Path,
        *,
        suite_root: str | Path | None = None,
        application_factory: Callable[..., AgentApplication] | None = None,
    ):
        self.agent_home = Path(agent_home).resolve()
        self.suite_root = Path(suite_root).resolve() if suite_root is not None else None
        self.application_factory = application_factory or AgentApplication

    def run(
        self,
        suite: EvalSuite,
        *,
        repetitions: int = 1,
        variant: str = "budgeted",
        output_dir: str | Path | None = None,
        evaluation_id: str | None = None,
    ) -> EvaluationReport:
        if repetitions <= 0:
            raise ValueError("repetitions must be positive")
        if variant not in {"passthrough", "budgeted", "compressed"}:
            raise ValueError("variant must be passthrough, budgeted, or compressed")
        root = (Path(output_dir).resolve() if output_dir is not None else self.agent_home / "evals" / (evaluation_id or str(uuid.uuid4())))
        root.mkdir(parents=True, exist_ok=True)
        suite_root = self.suite_root or Path.cwd().resolve()
        runs: list[EvalRun] = []
        for case in suite.cases:
            for repetition in range(1, repetitions + 1):
                runs.append(
                    self._run_case(
                        case,
                        repetition=repetition,
                        variant=variant,
                        suite_root=suite_root,
                        root=root,
                    )
                )
        self._write_outputs(suite, variant, root, runs)
        return EvaluationReport(root, self._aggregate(suite, variant, runs), tuple(runs))

    def run_ab(
        self,
        suite: EvalSuite,
        *,
        repetitions: int = 1,
        variants: Sequence[str] = ("passthrough", "budgeted"),
        output_dir: str | Path | None = None,
    ) -> dict[str, EvaluationReport | dict[str, Any]]:
        chosen = tuple(variants)
        if len(chosen) != 2 or len(set(chosen)) != 2:
            raise ValueError("A/B requires two distinct variants")
        if any(variant not in {"passthrough", "budgeted", "compressed"} for variant in chosen):
            raise ValueError("unknown A/B context variant")
        base = Path(output_dir).resolve() if output_dir is not None else self.agent_home / "evals" / str(uuid.uuid4())
        reports: dict[str, EvaluationReport | dict[str, Any]] = {}
        for variant in chosen:
            reports[variant] = self.run(
                suite,
                repetitions=repetitions,
                variant=variant,
                output_dir=base / variant,
            )
        first = reports[chosen[0]]
        second = reports[chosen[1]]
        assert isinstance(first, EvaluationReport)
        assert isinstance(second, EvaluationReport)
        difference = paired_diff(first.runs, second.runs)
        reports["paired_diff"] = difference
        base.mkdir(parents=True, exist_ok=True)
        (base / "paired_diff.json").write_text(
            json.dumps(difference, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
            encoding="utf-8",
        )
        return reports

    def _run_case(
        self,
        case: EvalCase,
        *,
        repetition: int,
        variant: str,
        suite_root: Path,
        root: Path,
    ) -> EvalRun:
        fixture = resolve_contained(suite_root, case.fixture, description="fixture")
        if not fixture.is_dir():
            return self._infrastructure_run(case, repetition, "fixture_missing")
        # A repeated invocation of the same report directory must still get a
        # fresh SQLite authority and workspace. The nonce is deliberately
        # internal; comparison reports omit it and all public pairing keys stay
        # case_id/repetition.
        run_home = root / "agent-home" / case.case_id / str(repetition) / uuid.uuid4().hex
        try:
            source_fingerprint = tree_fingerprint(fixture)
        except OSError as exc:
            return self._infrastructure_run(
                case,
                repetition,
                f"fixture_fingerprint_failed: {exc}",
            )
        context_builder = (
            PassthroughContextBuilder()
            if variant == "passthrough"
            else BudgetedContextBuilder()
        )
        try:
            compression_engine = self._compression_engine(case, suite_root, variant)
        except EvalInfrastructureFailure as exc:
            return self._infrastructure_run(case, repetition, str(exc))
        application = self.application_factory(
            run_home,
            context_builder=context_builder,
            compression_engine=compression_engine,
        )
        backend_spec = case.backend
        try:
            backend = self._backend_from_spec(backend_spec, suite_root)
            fault_stage = backend_spec.get("fault_stage")
            # fault_stage is intentionally not a general shell/command field;
            # it names a deterministic Runtime injection point for recovery evals.
            fault_state = {"raised": False}

            def fault_injector(stage: str) -> None:
                if stage == fault_stage and not fault_state["raised"]:
                    fault_state["raised"] = True
                    raise _InjectedEvalCrash(stage)

            try:
                result = application.run_task(
                    source=fixture,
                    task=case.task,
                    backend=backend,
                    policy=RunPolicy.from_dict(case.policy),
                    fault_injector=fault_injector if fault_stage else None,
                )
            except _InjectedEvalCrash:
                if not backend_spec.get("resume", False):
                    result = self._snapshot_result(application, case.case_id)
                else:
                    session_id = str(application.list_sessions()[-1]["id"])
                    resumed_backend = self._backend_from_spec(backend_spec, suite_root)
                    result = application.resume_session(session_id, backend=resumed_backend)
            events = application.journal.list_events(result.session_id)
            source_invariant = self._source_invariant(events)
            if source_invariant is None:
                source_invariant = tree_fingerprint(fixture) == source_fingerprint
            oracle_results = tuple(
                self._run_oracle(application, result, oracle, case.case_id, suite_root)
                for oracle in case.oracles
            )
            infra_oracles = [oracle for oracle in oracle_results if oracle.infrastructure_failure]
            if infra_oracles:
                return EvalRun(
                    case_id=case.case_id,
                    repetition=repetition,
                    session_id=result.session_id,
                    state=result.state.value,
                    task_success=False,
                    runtime_completed=result.state is RuntimeState.COMPLETED,
                    source_invariant=source_invariant,
                    valid=False,
                    infrastructure_failure=True,
                    failure_reason="eval_infrastructure_failure",
                    trace_path=str(result.trace_path),
                    metrics=collect_run_metrics(events, recovery_triggered=bool(fault_stage)),
                    oracles=oracle_results,
                )
            task_success = all(oracle.passed for oracle in oracle_results)
            failure_reason = self._failure_reason(result, task_success)
            return EvalRun(
                case_id=case.case_id,
                repetition=repetition,
                session_id=result.session_id,
                state=result.state.value,
                task_success=task_success,
                runtime_completed=result.state is RuntimeState.COMPLETED,
                source_invariant=source_invariant,
                valid=True,
                infrastructure_failure=False,
                failure_reason=failure_reason,
                trace_path=str(result.trace_path),
                metrics=collect_run_metrics(events, recovery_triggered=bool(fault_stage)),
                oracles=oracle_results,
            )
        except EvalInfrastructureFailure as exc:
            return self._infrastructure_run(case, repetition, str(exc))
        except (OSError, ValueError, KeyError, TypeError) as exc:
            return self._infrastructure_run(case, repetition, f"{type(exc).__name__}: {exc}")

    def _compression_engine(
        self,
        case: EvalCase,
        suite_root: Path,
        variant: str,
    ) -> CompressionEngine | None:
        if variant != "compressed":
            return None
        fixture = case.backend.get("compression_fixture")
        responses = case.backend.get("compression_responses")
        if fixture is None and responses is None:
            return None
        if fixture is not None:
            path = resolve_contained(suite_root, str(fixture), description="compression fixture")
            summarizer: ModelBackend = ScriptedBackend.from_file(path)
        else:
            if not isinstance(responses, list):
                raise EvalInfrastructureFailure("compression_responses must be an array")
            summarizer = ScriptedBackend(responses)
        return CompressionEngine(summarizer)

    @staticmethod
    def _backend_from_spec(spec: Mapping[str, Any], suite_root: Path) -> ModelBackend:
        kind = str(spec.get("kind", ""))
        if kind == "scripted":
            if isinstance(spec.get("responses"), list):
                return ScriptedBackend(spec["responses"])
            fixture = spec.get("fixture")
            if fixture is None:
                raise EvalInfrastructureFailure("scripted backend requires fixture or responses")
            return ScriptedBackend.from_file(
                resolve_contained(suite_root, str(fixture), description="backend fixture")
            )
        model = str(spec.get("model", ""))
        if not model:
            raise EvalInfrastructureFailure("provider backend requires model")
        timeout = float(spec.get("timeout", 60.0))
        if kind == "openai-compatible":
            return OpenAICompatibleBackend(
                model=model,
                base_url=str(spec.get("base_url", "https://api.openai.com/v1")),
                api_key_env=str(spec.get("api_key_env", "OPENAI_API_KEY")),
                timeout=timeout,
            )
        if kind == "anthropic":
            return AnthropicBackend(
                model=model,
                base_url=str(spec.get("base_url", "https://api.anthropic.com/v1")),
                api_key_env=str(spec.get("api_key_env", "ANTHROPIC_API_KEY")),
                timeout=timeout,
            )
        raise EvalInfrastructureFailure(f"unknown eval backend kind: {kind}")

    def _run_oracle(
        self,
        application: AgentApplication,
        result: RunResult,
        oracle: Mapping[str, Any],
        case_id: str,
        suite_root: Path,
    ) -> OracleResult:
        kind = str(oracle["kind"])
        try:
            if kind == "test_profile":
                profile = str(oracle.get("profile", ""))
                if application.test_profiles.get(profile) is None:
                    raise EvalInfrastructureFailure(f"unknown trusted test profile: {profile}")
                if result.workspace_path is None:
                    raise EvalInfrastructureFailure("test oracle has no workspace")
                outcome = application.harness.execute(
                    ToolCall(
                        id=f"eval-oracle-{case_id}-{profile}",
                        name="restricted_test",
                        arguments={"profile": profile},
                    ),
                    ToolContext(
                        workspace=application.workspace_manager.get(result.session_id),
                        allowed_permissions=frozenset({Permission.EXECUTE_TEST}),
                    ),
                )
                passed = outcome.status is ToolStatus.SUCCESS and bool(
                    outcome.data.get("passed") is True
                )
                return OracleResult(kind, passed, {"status": outcome.status.value, "data": outcome.data})
            if kind == "file":
                if result.workspace_path is None:
                    raise EvalInfrastructureFailure("file oracle has no workspace")
                path_text = str(oracle.get("path", ""))
                if not path_text:
                    raise EvalInfrastructureFailure("file oracle path is required")
                path = application.workspace_manager.get(result.session_id).resolve(
                    path_text,
                    must_exist=False,
                )
                exists = path.is_file()
                expected_exists = bool(oracle.get("exists", True))
                passed = exists is expected_exists
                details: dict[str, Any] = {"path": path_text, "exists": exists}
                if exists:
                    content = path.read_text(encoding="utf-8")
                    if oracle.get("sha256") is not None:
                        actual_hash = file_content_hash(path)
                        details["sha256"] = actual_hash
                        passed = passed and actual_hash == str(oracle["sha256"])
                    if oracle.get("contains") is not None:
                        passed = passed and str(oracle["contains"]) in content
                    if oracle.get("not_contains") is not None:
                        passed = passed and str(oracle["not_contains"]) not in content
                return OracleResult(kind, passed, details)
            if kind == "changed_paths":
                if result.workspace_path is None:
                    raise EvalInfrastructureFailure("changed-path oracle has no workspace")
                changed = _changed_paths(Path(result.workspace_path))
                allow = {str(path) for path in oracle.get("allow", [])}
                deny = {str(path) for path in oracle.get("deny", [])}
                passed = (not allow or changed <= allow) and not (changed & deny)
                return OracleResult(
                    kind,
                    passed,
                    {"changed_paths": sorted(changed), "allow": sorted(allow), "deny": sorted(deny)},
                )
            if kind == "result_schema":
                required = oracle.get("required", [])
                if not isinstance(required, list) or any(not isinstance(item, str) for item in required):
                    raise EvalInfrastructureFailure("result_schema required must be an array")
                result_dict = {
                    "session_id": result.session_id,
                    "state": result.state.value,
                    "final_answer": result.final_answer,
                    "failure": result.failure,
                    "workspace_path": result.workspace_path,
                }
                missing = [key for key in required if key not in result_dict]
                return OracleResult(kind, not missing, {"missing": missing})
            raise EvalInfrastructureFailure(f"unsupported eval oracle: {kind}")
        except EvalInfrastructureFailure as exc:
            return OracleResult(kind, False, {"error": str(exc)}, infrastructure_failure=True)
        except (OSError, ValueError) as exc:
            return OracleResult(kind, False, {"error": str(exc)}, infrastructure_failure=True)

    @staticmethod
    def _failure_reason(result: RunResult, task_success: bool) -> str | None:
        if result.failure and result.failure.get("kind") is not None:
            return str(result.failure["kind"])
        if not task_success:
            return "oracle_failure"
        return None

    @staticmethod
    def _source_invariant(events: Sequence[Event]) -> bool | None:
        for event in reversed(events):
            if event.event_type is EventType.RUN_FINISHED:
                value = event.payload.get("source_unchanged")
                return value if isinstance(value, bool) else None
        return None

    @staticmethod
    def _snapshot_result(application: AgentApplication, case_id: str) -> RunResult:
        sessions = application.list_sessions()
        if not sessions:
            raise EvalInfrastructureFailure(f"no persisted session for {case_id}")
        session_id = str(sessions[-1]["id"])
        snapshot = application.journal.load_snapshot(session_id)
        return RunResult(
            session_id=session_id,
            state=snapshot.state,
            final_answer=snapshot.final_answer,
            failure=snapshot.failure,
            workspace_path=snapshot.workspace_path,
            trace_path=str(application.journal.trace_path(session_id)),
            step_count=snapshot.step_count,
            model_calls=snapshot.model_calls,
            tool_calls=snapshot.tool_calls,
        )

    @staticmethod
    def _infrastructure_run(case: EvalCase, repetition: int, reason: str) -> EvalRun:
        return EvalRun(
            case_id=case.case_id,
            repetition=repetition,
            session_id="",
            state="infrastructure_failure",
            task_success=False,
            runtime_completed=False,
            source_invariant=None,
            valid=False,
            infrastructure_failure=True,
            failure_reason=reason,
            trace_path=None,
            metrics=collect_run_metrics([]),
        )

    @staticmethod
    def _aggregate(
        suite: EvalSuite,
        variant: str,
        runs: Sequence[EvalRun],
    ) -> dict[str, Any]:
        valid = [run for run in runs if run.valid and not run.infrastructure_failure]
        success_count = sum(run.task_success for run in valid)
        completed_count = sum(run.runtime_completed for run in valid)
        recovery_runs = [run for run in valid if run.metrics.get("recovery_triggered")]
        recovery_success = sum(run.task_success for run in recovery_runs)
        failure_reasons: Counter[str] = Counter(
            run.failure_reason for run in valid if run.failure_reason is not None
        )
        infrastructure_failure_reasons: Counter[str] = Counter(
            run.failure_reason or "unknown"
            for run in runs
            if run.infrastructure_failure
        )
        for run in valid:
            for reason, count in run.metrics.get("failure_reasons", {}).items():
                if reason != run.failure_reason:
                    failure_reasons[reason] += int(count)

        def values(name: str) -> list[float]:
            return [float(run.metrics.get(name, 0.0)) for run in valid]

        metrics = {
            "tool_calls": _distribution(values("tool_calls")),
            "model_calls": _distribution(values("model_calls")),
            "input_tokens": _distribution(values("input_tokens")),
            "output_tokens": _distribution(values("output_tokens")),
            "compression_input_tokens": _distribution(values("compression_input_tokens")),
            "compression_output_tokens": _distribution(values("compression_output_tokens")),
            "latency_ms": _distribution(values("latency_ms")),
            "model_latency_ms": _distribution(values("model_latency_ms")),
            "tool_latency_ms": _distribution(values("tool_latency_ms")),
            "test_latency_ms": _distribution(values("test_latency_ms")),
            "compression_latency_ms": _distribution(values("compression_latency_ms")),
        }
        return {
            "schema_version": 1,
            "suite_schema_version": suite.schema_version,
            "variant": variant,
            "case_count": len(suite.cases),
            "requested_run_count": len(runs),
            "valid_run_count": len(valid),
            "infrastructure_failure_count": len(runs) - len(valid),
            "task_success_rate": (success_count / len(valid)) if valid else None,
            "runtime_completion_rate": (completed_count / len(valid)) if valid else None,
            "source_invariant_rate": (
                sum(run.source_invariant is True for run in valid) / len(valid)
                if valid
                else None
            ),
            "source_invariant_unknown_count": sum(
                run.source_invariant is None for run in valid
            ),
            "recovery_rate": (
                recovery_success / len(recovery_runs) if recovery_runs else None
            ),
            "permission_violations": {
                "total": sum(int(run.metrics.get("permission_violations", 0)) for run in valid),
                "by_tool": dict(
                    sorted(
                        (
                            tool,
                            sum(
                                int(
                                    run.metrics.get(
                                        "permission_violations_by_tool", {}
                                    ).get(tool, 0)
                                )
                                for run in valid
                            ),
                        )
                        for tool in {
                            tool
                            for run in valid
                            for tool in run.metrics.get(
                                "permission_violations_by_tool", {}
                            )
                        }
                    )
                ),
                "by_run": _distribution(
                    [float(run.metrics.get("permission_violations", 0)) for run in valid]
                ),
            },
            "failure_reasons": dict(sorted(failure_reasons.items())),
            "infrastructure_failure_reasons": dict(
                sorted(infrastructure_failure_reasons.items())
            ),
            "metrics": metrics,
            "runs": [
                _comparison_run_dict(run)
                for run in sorted(valid + [run for run in runs if run not in valid], key=lambda item: (item.case_id, item.repetition))
            ],
        }

    @staticmethod
    def _write_outputs(
        suite: EvalSuite,
        variant: str,
        root: Path,
        runs: Sequence[EvalRun],
    ) -> None:
        (root / "manifest.snapshot.json").write_text(
            json.dumps(suite.to_dict(), ensure_ascii=False, sort_keys=True, indent=2) + "\n",
            encoding="utf-8",
        )
        with (root / "runs.jsonl").open("w", encoding="utf-8") as handle:
            for run in runs:
                handle.write(json.dumps(run.to_dict(), ensure_ascii=False, sort_keys=True) + "\n")
        report = EvaluationRunner._aggregate(suite, variant, runs)
        (root / "report.json").write_text(
            json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
            encoding="utf-8",
        )
        (root / "report.md").write_text(_report_markdown(report), encoding="utf-8")


def _changed_paths(root: Path) -> set[str]:
    completed = subprocess.run(
        ["git", "diff", "--name-only", "--no-renames"],
        cwd=root,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    if completed.returncode != 0:
        raise EvalInfrastructureFailure("git diff oracle failed")
    return {line.strip() for line in completed.stdout.splitlines() if line.strip()}


def paired_diff(first: Sequence[EvalRun], second: Sequence[EvalRun]) -> dict[str, Any]:
    left = {(run.case_id, run.repetition): run for run in first}
    right = {(run.case_id, run.repetition): run for run in second}
    if set(left) != set(right):
        raise EvalValidationError("A/B runs do not have paired case/repetition keys")
    pairs = []
    for key in sorted(left):
        a = left[key]
        b = right[key]
        pairs.append(
            {
                "case_id": key[0],
                "repetition": key[1],
                "task_success_delta": int(b.task_success) - int(a.task_success),
                "runtime_completion_delta": int(b.runtime_completed) - int(a.runtime_completed),
                "tool_calls_delta": b.metrics.get("tool_calls", 0) - a.metrics.get("tool_calls", 0),
                "model_calls_delta": b.metrics.get("model_calls", 0) - a.metrics.get("model_calls", 0),
                "input_tokens_delta": b.metrics.get("input_tokens", 0) - a.metrics.get("input_tokens", 0),
                "output_tokens_delta": b.metrics.get("output_tokens", 0) - a.metrics.get("output_tokens", 0),
                "compression_input_tokens_delta": (
                    b.metrics.get("compression_input_tokens", 0)
                    - a.metrics.get("compression_input_tokens", 0)
                ),
                "compression_output_tokens_delta": (
                    b.metrics.get("compression_output_tokens", 0)
                    - a.metrics.get("compression_output_tokens", 0)
                ),
                "latency_ms_delta": b.metrics.get("latency_ms", 0) - a.metrics.get("latency_ms", 0),
            }
        )
    return {
        "variable": "context_policy",
        "baseline": "first_argument",
        "candidate": "second_argument",
        "pairs": pairs,
    }


def _report_markdown(report: Mapping[str, Any]) -> str:
    lines = [
        "# Evaluation report",
        "",
        f"- Variant: `{report['variant']}`",
        f"- Valid runs: {report['valid_run_count']} / {report['requested_run_count']}",
        f"- Task success rate: {report['task_success_rate']}",
        f"- Runtime completion rate: {report['runtime_completion_rate']}",
        f"- Source invariant rate: {report['source_invariant_rate']}",
        f"- Recovery rate: {report['recovery_rate']}",
        "",
        "## Runs",
        "",
        "| Case | Repetition | Runtime | Task | Failure | Trace reference |",
        "| --- | ---: | --- | --- | --- | --- |",
    ]
    for run in report["runs"]:
        trace = f"runs/{run['case_id']}/{run['repetition']}"
        lines.append(
            f"| {run['case_id']} | {run['repetition']} | {run['state']} | "
            f"{run['task_success']} | {run['failure_reason'] or ''} | {trace} |"
        )
    lines.extend(("", "Infrastructure failures are excluded from valid-run denominators."))
    return "\n".join(lines) + "\n"
