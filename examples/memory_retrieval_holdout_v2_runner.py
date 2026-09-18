"""Run the sealed Memory retrieval Holdout v2.

The runner deliberately accepts the v2 JSON shape rather than teaching the generic
evaluation suite about Holdout-specific fields.  It uses the repository's existing
retriever and context renderer, keeps one SQLite pool shared by all cases that name
it, and records fixture-oracle observations without embedding an answer key.

The default executor is observation-only.  It measures retrieval and rendering and
evaluates immutable file/test/tool safety oracles, but it does not pretend that a
Provider or an Agent changed a fixture.  A real Provider evaluation therefore
cannot be inferred from this report.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from typing import Any

from coding_agent.context import BudgetedContextBuilder
from coding_agent.domain import ContextBuildInput, Message, RepositorySnapshot, RunPolicy, RuntimeState
from coding_agent.memory.domain import MemoryKind, MemoryQuery, MemoryScope
from coding_agent.memory.policy import MemoryPolicyError, MemoryWriteContext
from coding_agent.memory.retrieval import LexicalMemoryRetriever
from coding_agent.memory.service import MemoryService
from coding_agent.memory.sqlite import SQLiteMemoryStore


RUNNER_SCHEMA_VERSION = 1
DEFAULT_NOW = "2026-09-17T00:00:00+00:00"
EXPECTED_HOLDOUT_SHA256 = (
    "d1d9c45d9d06aea211780fa1d6b3d9baf4154891f1a3344ce1ae1cb658907d6f"
)
ALLOWED_CHECKS = frozenset(
    {
        "file_contains",
        "file_not_contains",
        "file_absent",
        "file_exists",
        "command_result",
        "tool_observation",
        "behavior_delta",
    }
)


class HoldoutRunnerError(RuntimeError):
    """The suite, fixture, oracle, or result bundle is not runnable."""


class OracleInfrastructureError(HoldoutRunnerError):
    """An oracle cannot be evaluated safely by this runner."""


@dataclass(frozen=True)
class CaseSpec:
    raw: Mapping[str, Any]
    case_id: str
    repository_id: str
    memory_pool_id: str
    category: str
    task: str
    query_scope: Mapping[str, Any]
    expected_relevant: frozenset[str]
    excluded: frozenset[str]
    oracle: Mapping[str, Any]
    compatibility: bool = False


@dataclass(frozen=True)
class SeedOutcome:
    memory_id: str
    final_status: str
    seeded: bool


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: str | Path) -> str:
    return sha256_bytes(Path(path).read_bytes())


def load_manifest(path: str | Path) -> dict[str, Any]:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise HoldoutRunnerError("manifest must be a JSON object")
    return raw


def _as_mapping(value: Any, description: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise HoldoutRunnerError(f"{description} must be an object")
    return value


def _as_string(value: Any, description: str, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str) or (not allow_empty and not value.strip()):
        raise HoldoutRunnerError(f"{description} must be a non-empty string")
    return value


def _string_set(value: Any, description: str) -> frozenset[str]:
    if value is None:
        return frozenset()
    if not isinstance(value, (list, tuple, set, frozenset)):
        raise HoldoutRunnerError(f"{description} must be an array of strings")
    if any(not isinstance(item, str) or not item for item in value):
        raise HoldoutRunnerError(f"{description} must be an array of strings")
    return frozenset(value)


def _query_scope(raw: Mapping[str, Any]) -> Mapping[str, Any]:
    candidate = raw.get("query_scope", raw.get("query", {}))
    if isinstance(candidate, str):
        return {}
    if candidate is None:
        return {}
    return _as_mapping(candidate, "case query_scope")


def _oracle(raw: Mapping[str, Any]) -> Mapping[str, Any]:
    value = raw.get("oracle")
    if value is None:
        raise HoldoutRunnerError(f"case {raw.get('case_id', '<unknown>')} has no oracle")
    return _as_mapping(value, "case oracle")


def _case_from_raw(raw: Mapping[str, Any], *, compatibility: bool = False) -> CaseSpec:
    case_id = _as_string(raw.get("case_id"), "case case_id")
    repository_id = _as_string(
        raw.get("repository_id", "holdout-compatibility"), "case repository_id"
    )
    pool_id = _as_string(
        raw.get("memory_pool_id", raw.get("pool_id", "")), "case memory_pool_id"
    )
    task = _as_string(raw.get("task"), f"case {case_id} task")
    query_scope = _query_scope(raw)
    oracle = _oracle(raw)
    return CaseSpec(
        raw=raw,
        case_id=case_id,
        repository_id=repository_id,
        memory_pool_id=pool_id,
        category=str(raw.get("category", "")),
        task=task,
        query_scope=query_scope,
        expected_relevant=_string_set(
            raw.get("expected_relevant_memory_ids", raw.get("relevant_memory_ids", [])),
            f"case {case_id} expected_relevant_memory_ids",
        ),
        excluded=_string_set(
            raw.get("excluded_memory_ids", raw.get("forbidden_memory_ids", [])),
            f"case {case_id} excluded_memory_ids",
        ),
        oracle=oracle,
        compatibility=compatibility,
    )


def _case_list(value: Any, description: str, *, compatibility: bool = False) -> list[CaseSpec]:
    if not isinstance(value, list):
        raise HoldoutRunnerError(f"{description} must be an array")
    return [_case_from_raw(_as_mapping(item, "case"), compatibility=compatibility) for item in value]


def _compatibility_cases(raw: Mapping[str, Any]) -> tuple[list[CaseSpec], dict[str, Any]]:
    arm_value = raw.get("compatibility_arm", {})
    if arm_value is None:
        return [], {}
    arm = _as_mapping(arm_value, "compatibility_arm")
    cases_raw = arm.get("cases", [])
    if not isinstance(cases_raw, list):
        raise HoldoutRunnerError("compatibility_arm.cases must be an array")
    pool_id = str(arm.get("memory_pool_id", arm.get("pool_id", "compatibility-5298ba0")))
    repository_id = str(arm.get("repository_id", "compatibility-5298ba0"))
    normalized: list[dict[str, Any]] = []
    for item in cases_raw:
        case = dict(_as_mapping(item, "compatibility case"))
        case.setdefault("memory_pool_id", pool_id)
        case.setdefault("repository_id", repository_id)
        normalized.append(case)
    cases = _case_list(normalized, "compatibility_arm.cases", compatibility=True)
    pool_raw = arm.get("pool")
    if pool_raw is None:
        pool_raw = arm.get("memory_pool")
    pool = dict(_as_mapping(pool_raw, "compatibility memory pool")) if pool_raw is not None else {}
    if "records" not in pool and isinstance(arm.get("records"), list):
        pool["records"] = arm["records"]
    pool.setdefault("records", [])
    return cases, {pool_id: pool}


def _run_legacy_compatibility(workspace: Path) -> dict[str, Any]:
    """Run only the frozen 5298ba0 compatibility control.

    The v2 body intentionally stores metadata for this arm rather than copying the
    historical task text.  The source module is already frozen in the repository and
    is excluded from the independent-main aggregates.
    """

    from examples.memory_retrieval_holdout import (
        COMPATIBILITY_CASES,
        _compatibility_summary,
        _run_pairs,
    )

    source = workspace / "legacy-source"
    source.mkdir(parents=True, exist_ok=True)
    (source / "README.md").write_text(
        "5298ba0 compatibility fixture.\n",
        encoding="utf-8",
    )
    pairs, _outcomes = _run_pairs(
        root=workspace / "legacy-runs",
        source=source,
        cases=COMPATIBILITY_CASES,
        arm_name="compatibility-5298ba0",
    )
    summary = _compatibility_summary(pairs)
    warm_success = summary.get("warm_success", {})
    if not isinstance(warm_success, Mapping):
        raise HoldoutRunnerError("legacy compatibility summary has no warm success count")
    return {
        "source_commit": "5298ba0",
        "executor_kind": "legacy_scripted_compatibility",
        "provider_evaluation": False,
        "case_count": len(pairs),
        "valid_case_count": len(pairs),
        "invalid_case_count": 0,
        "warm_success_count": int(warm_success.get("successful", 0)),
        "warm_success_total": int(warm_success.get("total", 0)),
        "manifest_tokens_match_renderer": bool(summary.get("manifest_tokens_match_renderer")),
        "cases": summary.get("cases", []),
    }


def load_suite(path: str | Path) -> dict[str, Any]:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise HoldoutRunnerError("suite must be a JSON object")
    if int(raw.get("schema_version", -1)) != 1:
        raise HoldoutRunnerError("unsupported Holdout v2 schema version")
    repositories = _as_mapping(raw.get("repositories"), "repositories")
    pools = _as_mapping(raw.get("memory_pools"), "memory_pools")
    cases = _case_list(raw.get("cases"), "cases")
    if not cases:
        raise HoldoutRunnerError("suite must contain at least one main case")
    case_ids = [case.case_id for case in cases]
    if len(set(case_ids)) != len(case_ids):
        raise HoldoutRunnerError("case ids must be unique")
    for case in cases:
        if case.repository_id not in repositories:
            raise HoldoutRunnerError(f"case {case.case_id} names an unknown repository")
        if case.memory_pool_id not in pools:
            raise HoldoutRunnerError(f"case {case.case_id} names an unknown memory pool")
    for pool_id, pool_value in pools.items():
        pool = _as_mapping(pool_value, f"memory pool {pool_id}")
        records = pool.get("records", [])
        if not isinstance(records, list):
            raise HoldoutRunnerError(f"memory pool {pool_id}.records must be an array")
    compat_cases, compat_pools = _compatibility_cases(raw)
    compat_ids = [case.case_id for case in compat_cases]
    if len(set(compat_ids)) != len(compat_ids):
        raise HoldoutRunnerError("compatibility case ids must be unique")
    if set(case_ids) & set(compat_ids):
        raise HoldoutRunnerError("main and compatibility case ids must be disjoint")
    raw["_validated_case_count"] = len(cases)
    raw["_validated_compatibility_case_count"] = len(compat_cases)
    raw["_compatibility_pool_ids"] = sorted(compat_pools)
    return raw


def _repository_files(raw: Mapping[str, Any]) -> Mapping[str, Any]:
    files = raw.get("files", raw)
    return _as_mapping(files, "repository files")


def _safe_relative_path(value: Any, description: str) -> Path:
    text = _as_string(value, description)
    path = Path(text)
    if path.is_absolute() or ".." in path.parts:
        raise HoldoutRunnerError(f"{description} must be workspace-relative")
    return path


def _materialize_repository(repository: Mapping[str, Any], destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    for name, content in _repository_files(repository).items():
        relative = _safe_relative_path(name, "repository file path")
        if not isinstance(content, str):
            raise HoldoutRunnerError(f"repository file {name} must contain text")
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")


def _tree_digest(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*")):
        if not path.is_file() or "__pycache__" in path.parts or path.suffix == ".pyc":
            continue
        relative = path.relative_to(root).as_posix()
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def _scope_context(scope: MemoryScope, scope_id: str) -> MemoryWriteContext:
    return MemoryWriteContext(
        actor_id="holdout-v2-runner",
        session_id=scope_id if scope is MemoryScope.SESSION else None,
        repository_id=scope_id if scope is MemoryScope.REPOSITORY else None,
        user_id=scope_id if scope is MemoryScope.USER else None,
        allowed_scopes=frozenset({scope}),
    )


def _record_status(record: Mapping[str, Any]) -> str:
    value = record.get("final_status", record.get("status", "active"))
    return str(value)


def _seed_pool(store: SQLiteMemoryStore, pool: Mapping[str, Any], now: str) -> dict[str, SeedOutcome]:
    records = pool.get("records", [])
    if not isinstance(records, list):
        raise HoldoutRunnerError("memory pool records must be an array")
    service = MemoryService(store, clock=lambda: now, provenance_validator=lambda _record: True)
    outcomes: dict[str, SeedOutcome] = {}
    for index, raw_value in enumerate(records, start=1):
        raw = _as_mapping(raw_value, "memory record")
        memory_id = _as_string(raw.get("memory_id"), "memory_id")
        if memory_id in outcomes:
            raise HoldoutRunnerError(f"duplicate memory id in pool: {memory_id}")
        scope = MemoryScope(_as_string(raw.get("scope"), f"memory {memory_id} scope"))
        scope_id = _as_string(raw.get("scope_id"), f"memory {memory_id} scope_id")
        status = _record_status(raw)
        content = raw.get("content", "")
        if not isinstance(content, str):
            raise HoldoutRunnerError(f"memory {memory_id} content must be text")
        repository_revision = raw.get("repository_revision")
        if repository_revision is not None:
            repository_revision = _as_string(
                repository_revision, f"memory {memory_id} repository_revision"
            )
        if scope is MemoryScope.REPOSITORY and repository_revision is None:
            repository_revision = str(pool.get("repository_revision", "holdout-revision"))
        kind = MemoryKind(str(raw.get("kind", MemoryKind.SEMANTIC.value)))
        context = _scope_context(scope, scope_id)
        source_run_id = f"holdout-v2-seed-{index}-{memory_id}"
        source_event_refs = (f"holdout-v2-event-{index}-{memory_id}",)
        confidence = float(raw.get("confidence", 0.9))
        expires_at = raw.get("expires_at")
        if expires_at is not None:
            expires_at = _as_string(expires_at, f"memory {memory_id} expires_at")
        try:
            proposed = service.propose(
                context=context,
                scope=scope,
                kind=kind,
                content=content,
                source_run_id=source_run_id,
                source_agent_id="runtime",
                source_event_refs=source_event_refs,
                confidence=confidence,
                repository_revision=repository_revision,
                expires_at=expires_at,
                memory_id=memory_id,
            )
        except MemoryPolicyError:
            if status not in {"policy_rejected", "rejected"}:
                raise
            outcomes[memory_id] = SeedOutcome(memory_id, status, seeded=False)
            continue
        if status == "policy_rejected":
            raise HoldoutRunnerError(f"policy negative was accepted: {memory_id}")
        if status == "rejected":
            service.reject(memory_id, context=context, expected_version=proposed.version)
            outcomes[memory_id] = SeedOutcome(memory_id, status, seeded=True)
            continue
        active = service.activate(memory_id, context=context, expected_version=proposed.version)
        if status == "stale":
            service.mark_stale(
                memory_id,
                context=context,
                expected_version=active.version,
                reason="frozen Holdout stale control",
            )
        elif status == "deleted":
            service.delete(memory_id, context=context, expected_version=active.version)
        elif status != "active":
            raise HoldoutRunnerError(f"unknown final memory status: {status}")
        outcomes[memory_id] = SeedOutcome(memory_id, status, seeded=True)
    return outcomes


def _scope_value(scope: Mapping[str, Any], *names: str) -> str | None:
    for name in names:
        value = scope.get(name)
        if value is not None:
            return str(value)
    return None


def _query_for(case: CaseSpec) -> MemoryQuery:
    scope = case.query_scope
    query_value = case.raw.get("query")
    query_text = query_value if isinstance(query_value, str) else case.task
    top_k = int(case.raw.get("top_k", case.raw.get("retrieval_top_k", 5)))
    token_budget = int(case.raw.get("token_budget", case.raw.get("retrieval_token_budget", 512)))
    return MemoryQuery(
        text=query_text,
        session_id=_scope_value(scope, "session_id", "session"),
        repository_id=_scope_value(scope, "repository_id", "repository"),
        user_id=_scope_value(scope, "user_id", "user"),
        repository_revision=_scope_value(scope, "repository_revision", "revision"),
        top_k=top_k,
        token_budget=token_budget,
    )


def _request_for(case: CaseSpec, repository: Mapping[str, Any]) -> ContextBuildInput:
    scope = case.query_scope
    revision = _scope_value(scope, "repository_revision", "revision") or str(
        repository.get("revision", "holdout-fixture-revision")
    )
    files = tuple(sorted(str(path) for path in _repository_files(repository)))
    return ContextBuildInput(
        session_id=_scope_value(scope, "session_id", "session") or f"session-{case.case_id}",
        task=case.task,
        messages=(Message(role="user", content=case.task),),
        runtime_state=RuntimeState.BUILDING_CONTEXT,
        policy=RunPolicy(max_steps=4, max_model_calls=1, max_tool_calls=0, max_output_tokens=0),
        repository_snapshot=RepositorySnapshot(workspace_revision=revision, file_paths=files),
        latest_summary=None,
        provider="scripted",
        model="scripted",
    )


def _memory_query_factory(case: CaseSpec):
    query = _query_for(case)

    def factory(_request: ContextBuildInput) -> MemoryQuery:
        return query

    return factory


def _safe_file(root: Path, value: Any) -> Path:
    relative = _safe_relative_path(value, "oracle file path")
    target = (root / relative).resolve()
    if root.resolve() not in target.parents and target != root.resolve():
        raise OracleInfrastructureError("oracle path escaped fixture root")
    return target


def _need_text(check: Mapping[str, Any]) -> list[str]:
    value = check.get("text", check.get("contains", check.get("value")))
    if isinstance(value, str):
        return [value]
    if isinstance(value, list) and all(isinstance(item, str) for item in value):
        return list(value)
    raise OracleInfrastructureError("file oracle requires text/contains/value")


def _run_command_check(check: Mapping[str, Any], root: Path) -> dict[str, Any]:
    profile = str(check.get("profile", check.get("command", "")))
    if profile not in {"python_unittest", "unittest"}:
        raise OracleInfrastructureError(f"unsupported trusted command profile: {profile}")
    timeout = float(check.get("timeout_seconds", 30.0))
    if timeout <= 0 or timeout > 120:
        raise OracleInfrastructureError("command timeout is outside the allowed range")
    started = time.perf_counter()
    completed = subprocess.run(
        [sys.executable, "-m", "unittest", "discover", "-s", "tests"],
        cwd=root,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    expected_exit = int(check.get("expected_exit_code", check.get("exit_code", 0)))
    stdout = completed.stdout
    stderr = completed.stderr
    contains_stdout = check.get("stdout_contains", check.get("stdout", []))
    contains_stderr = check.get("stderr_contains", check.get("stderr", []))
    stdout_needles = [contains_stdout] if isinstance(contains_stdout, str) else list(contains_stdout or [])
    stderr_needles = [contains_stderr] if isinstance(contains_stderr, str) else list(contains_stderr or [])
    if any(not isinstance(item, str) for item in (*stdout_needles, *stderr_needles)):
        raise OracleInfrastructureError("command output expectations must be strings")
    passed = (
        completed.returncode == expected_exit
        and all(needle in stdout for needle in stdout_needles)
        and all(needle in stderr for needle in stderr_needles)
    )
    return {
        "kind": "command_result",
        "profile": profile,
        "passed": passed,
        "exit_code": completed.returncode,
        "expected_exit_code": expected_exit,
        "latency_ms": max(0.0, (time.perf_counter() - started) * 1000),
    }


def _run_check(check: Mapping[str, Any], root: Path) -> dict[str, Any]:
    kind = str(check.get("kind", check.get("type", "")))
    if kind not in ALLOWED_CHECKS:
        raise OracleInfrastructureError(f"unsupported oracle check: {kind}")
    if kind == "command_result":
        return _run_command_check(check, root)
    if kind == "tool_observation":
        expected = check.get("expected_tool_calls", check.get("expected", []))
        if expected in (None, [], ()):  # Observation-only runner made no tool calls.
            return {"kind": kind, "passed": True, "observed_tool_calls": 0}
        return {
            "kind": kind,
            "passed": False,
            "observed_tool_calls": 0,
            "reason": "observation_only_executor_did_not_call_tools",
        }
    if kind == "behavior_delta":
        expected = str(check.get("expected", check.get("expected_behavior_delta", "none"))).lower()
        passed = expected in {"none", "zero", "unchanged", "0"}
        return {"kind": kind, "passed": passed, "observed_delta": 0}
    target = _safe_file(root, check.get("path"))
    exists = target.is_file()
    if kind == "file_absent":
        return {"kind": kind, "passed": not target.exists(), "path_exists": target.exists()}
    if kind == "file_exists":
        return {"kind": kind, "passed": exists, "path_exists": target.exists()}
    if not exists:
        return {"kind": kind, "passed": False, "path_exists": False}
    content = target.read_text(encoding="utf-8")
    needles = _need_text(check)
    if kind == "file_contains":
        passed = all(needle in content for needle in needles)
    else:
        passed = all(needle not in content for needle in needles)
    return {"kind": kind, "passed": passed, "path_exists": True}


def _oracle_checks(oracle: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    checks = oracle.get("checks")
    if checks is None and ("kind" in oracle or "type" in oracle):
        return [oracle]
    if not isinstance(checks, list) or not checks:
        raise OracleInfrastructureError("oracle checks must be a non-empty array")
    return [_as_mapping(item, "oracle check") for item in checks]


def _run_oracle(oracle: Mapping[str, Any], root: Path) -> tuple[bool, list[dict[str, Any]], str | None]:
    try:
        results = [_run_check(check, root) for check in _oracle_checks(oracle)]
    except (OSError, UnicodeError, subprocess.SubprocessError, ValueError) as exc:
        return False, [], f"oracle execution failed: {type(exc).__name__}: {exc}"
    return all(bool(item.get("passed")) for item in results), results, None


def _scope_leakage(case: CaseSpec, selection: Mapping[str, Any], records: Mapping[str, Mapping[str, Any]]) -> int:
    leaks = 0
    selected = selection.get("records", [])
    if not isinstance(selected, list):
        return 1
    scope = case.query_scope
    session_id = _scope_value(scope, "session_id", "session")
    repository_id = _scope_value(scope, "repository_id", "repository")
    user_id = _scope_value(scope, "user_id", "user")
    revision = _scope_value(scope, "repository_revision", "revision")
    for item in selected:
        if not isinstance(item, Mapping):
            leaks += 1
            continue
        memory_id = str(item.get("memory_id", ""))
        raw = records.get(memory_id)
        if raw is None:
            # A selection not present in the pool is always a leakage.
            leaks += 1
            continue
        scope_kind = str(raw.get("scope", ""))
        scope_id = str(raw.get("scope_id", ""))
        if scope_kind == MemoryScope.SESSION.value and scope_id != session_id:
            leaks += 1
        elif scope_kind == MemoryScope.REPOSITORY.value and (
            scope_id != repository_id or str(raw.get("repository_revision")) != str(revision)
        ):
            leaks += 1
        elif scope_kind == MemoryScope.USER.value and scope_id != user_id:
            leaks += 1
    return leaks


def _selected_ids(manifest: Mapping[str, Any] | None) -> tuple[str, ...]:
    if not isinstance(manifest, Mapping):
        return ()
    value = manifest.get("included_memory_ids", [])
    if isinstance(value, list):
        return tuple(str(item) for item in value)
    return ()


def _token_manifest_check(built: Any) -> bool:
    memory = built.memory
    if not isinstance(memory, Mapping):
        return True
    memory_section = next((section for section in built.sections if section.name == "memory"), None)
    if memory_section is None:
        return int(memory.get("actual_context_token_cost", 0)) == 0
    if int(memory.get("actual_context_token_cost", -1)) != memory_section.estimated_tokens:
        return False
    records = memory.get("records", [])
    return isinstance(records, list) and all(
        isinstance(record, Mapping) and isinstance(record.get("actual_context_token_cost"), int)
        for record in records
    )


def _metric_ratio(numerator: int, denominator: int) -> float:
    return numerator / denominator if denominator else 1.0


def _run_case(
    case: CaseSpec,
    repository: Mapping[str, Any],
    store: SQLiteMemoryStore,
    pool_records: Mapping[str, Mapping[str, Any]],
    workspace: Path,
) -> dict[str, Any]:
    case_root = workspace / case.case_id
    _materialize_repository(repository, case_root)
    request = _request_for(case, repository)
    query_factory = _memory_query_factory(case)
    on_builder = BudgetedContextBuilder(
        memory_retriever=LexicalMemoryRetriever(store),
        memory_query_factory=query_factory,
    )
    off_builder = BudgetedContextBuilder()
    off_started = time.perf_counter()
    off_context = off_builder.build(request)
    off_latency = max(0.0, (time.perf_counter() - off_started) * 1000)
    cold_started = time.perf_counter()
    cold_context = on_builder.build(request)
    cold_latency = max(0.0, (time.perf_counter() - cold_started) * 1000)
    warm_started = time.perf_counter()
    warm_context = on_builder.build(request)
    warm_latency = max(0.0, (time.perf_counter() - warm_started) * 1000)
    oracle_success, oracle_results, oracle_error = _run_oracle(case.oracle, case_root)
    selected = _selected_ids(warm_context.memory)
    selected_set = set(selected)
    relevant = set(case.expected_relevant)
    irrelevant = selected_set - relevant
    leakage = _scope_leakage(
        case,
        warm_context.memory or {},
        pool_records,
    )
    prompt_bypass = sum(
        1
        for memory_id in selected_set
        if str(pool_records.get(memory_id, {}).get("final_status", pool_records.get(memory_id, {}).get("status", "")))
        in {"policy_rejected", "rejected"}
    )
    off_oracle_success, off_oracle_results, off_oracle_error = _run_oracle(case.oracle, case_root)
    # The observation-only executor never edits a fixture.  Keep this explicit so
    # no-memory equality is not confused with a Provider task result.
    behavior_delta = int(off_oracle_success != oracle_success)
    if oracle_error is not None or off_oracle_error is not None:
        valid = False
        failure_reason = oracle_error or off_oracle_error
    else:
        valid = True
        failure_reason = None
    memory = warm_context.memory if isinstance(warm_context.memory, Mapping) else {}
    return {
        "case_id": case.case_id,
        "category": case.category,
        "pool_id": case.memory_pool_id,
        "compatibility": case.compatibility,
        "valid": valid,
        "task_success": bool(valid and oracle_success),
        "executor_kind": "observation_only",
        "provider_evaluation": False,
        "oracle_success": oracle_success,
        "oracle_results": oracle_results,
        "off_oracle_success": off_oracle_success,
        "off_oracle_results": off_oracle_results,
        "selected_memory_ids": list(selected),
        "expected_relevant_memory_ids": sorted(relevant),
        "irrelevant_selected_memory_ids": sorted(irrelevant),
        "relevant_hits": len(selected_set & relevant),
        "relevant_count": len(relevant),
        "irrelevant_count": len(irrelevant),
        "scope_revision_stale_deleted_leakage": leakage,
        "prompt_injection_policy_bypass": prompt_bypass,
        "no_memory_behavior_delta": behavior_delta if case.category == "no_memory" else None,
        "manifest_token_matches_renderer": _token_manifest_check(warm_context),
        "off": {
            "model_total_tokens": off_context.total_input_tokens,
            "latency_ms": off_latency,
        },
        "cold": {
            "model_total_tokens": cold_context.total_input_tokens,
            "retrieval_tokens": int(memory.get("retrieval_token_cost", 0)),
            "memory_context_tokens": int(memory.get("actual_context_token_cost", 0)),
            "latency_ms": cold_latency,
            "retrieval_latency_ms": float(memory.get("duration_ms", 0.0)),
            "selected_memory_ids": list(_selected_ids(cold_context.memory)),
        },
        "warm": {
            "model_total_tokens": warm_context.total_input_tokens,
            "retrieval_tokens": int(memory.get("retrieval_token_cost", 0)),
            "memory_context_tokens": int(memory.get("actual_context_token_cost", 0)),
            "latency_ms": warm_latency,
            "retrieval_latency_ms": float(memory.get("duration_ms", 0.0)),
            "selected_memory_ids": list(selected),
        },
        "failure_reason": failure_reason,
    }


def _aggregate(results: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    valid = [item for item in results if bool(item.get("valid"))]
    successes = [item for item in valid if bool(item.get("task_success"))]
    relevant_total = sum(int(item.get("relevant_count", 0)) for item in valid)
    relevant_hits = sum(int(item.get("relevant_hits", 0)) for item in valid)
    selected_total = sum(
        len(item.get("selected_memory_ids", []))
        for item in valid
        if isinstance(item.get("selected_memory_ids"), list)
    )
    irrelevant_total = sum(int(item.get("irrelevant_count", 0)) for item in valid)
    retrieval_tokens = sum(int(item.get("warm", {}).get("retrieval_tokens", 0)) for item in valid)
    model_tokens = sum(int(item.get("warm", {}).get("model_total_tokens", 0)) for item in valid)
    return {
        "case_count": len(results),
        "valid_case_count": len(valid),
        "invalid_case_count": len(results) - len(valid),
        "task_success_count": len(successes),
        "task_success_rate": _metric_ratio(len(successes), len(valid)),
        "relevant_recall": _metric_ratio(relevant_hits, relevant_total),
        "precision": _metric_ratio(relevant_hits, selected_total),
        "irrelevant_injection": _metric_ratio(irrelevant_total, selected_total)
        if selected_total
        else 0.0,
        "irrelevant_memory_behavior_delta": sum(
            int(item["no_memory_behavior_delta"])
            for item in valid
            if item.get("no_memory_behavior_delta") is not None
        ),
        "scope_revision_stale_deleted_leakage": sum(
            int(item.get("scope_revision_stale_deleted_leakage", 0)) for item in results
        ),
        "prompt_injection_policy_bypass": sum(
            int(item.get("prompt_injection_policy_bypass", 0)) for item in results
        ),
        "retrieval_tokens_total": retrieval_tokens,
        "model_total_tokens": model_tokens,
        "cold_latency_ms": {
            "mean": _mean(item["cold"]["latency_ms"] for item in valid),
        },
        "warm_latency_ms": {
            "mean": _mean(item["warm"]["latency_ms"] for item in valid),
        },
        "retrieval_latency_ms": {
            "mean": _mean(item["warm"]["retrieval_latency_ms"] for item in valid),
        },
        "tokens_per_successful_task": (
            (model_tokens + retrieval_tokens) / len(successes) if successes else None
        ),
        "manifest_token_mismatch_count": sum(
            not bool(item.get("manifest_token_matches_renderer")) for item in results
        ),
    }


def _mean(values: Iterable[float]) -> float | None:
    numbers = [float(value) for value in values]
    return sum(numbers) / len(numbers) if numbers else None


def _pool_records(pool: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    records = pool.get("records", [])
    if not isinstance(records, list):
        raise HoldoutRunnerError("memory pool records must be an array")
    result: dict[str, Mapping[str, Any]] = {}
    for value in records:
        record = _as_mapping(value, "memory record")
        memory_id = _as_string(record.get("memory_id"), "memory_id")
        result[memory_id] = record
    return result


def _run_case_group(
    cases: Sequence[CaseSpec],
    repositories: Mapping[str, Any],
    pools: Mapping[str, Any],
    workspace: Path,
    now: str,
) -> list[dict[str, Any]]:
    stores: dict[str, SQLiteMemoryStore] = {}
    try:
        for pool_id, raw_pool in pools.items():
            pool = _as_mapping(raw_pool, f"memory pool {pool_id}")
            store = SQLiteMemoryStore(workspace / f"{pool_id}.db", clock=lambda: now)
            _seed_pool(store, pool, now)
            stores[pool_id] = store
        results: list[dict[str, Any]] = []
        for case in cases:
            repository = _as_mapping(repositories.get(case.repository_id), f"repository {case.repository_id}")
            pool = _as_mapping(pools.get(case.memory_pool_id), f"memory pool {case.memory_pool_id}")
            results.append(
                _run_case(
                    case,
                    repository,
                    stores[case.memory_pool_id],
                    _pool_records(pool),
                    workspace / "fixtures",
                )
            )
        return results
    finally:
        for store in stores.values():
            store.close()


def _redacted_summary(report: Mapping[str, Any]) -> dict[str, Any]:
    main_results = report.get("cases", [])
    compat_results = report.get("compatibility_arm", {}).get("cases", [])
    return {
        "schema_version": report["schema_version"],
        "benchmark": report["benchmark"],
        "execution_time": report["execution_time"],
        "algorithm_commit": report["algorithm_commit"],
        "runner_commit": report["runner_commit"],
        "suite_sha256": report["suite_sha256"],
        "executor_kind": report["executor_kind"],
        "provider_evaluation": report["provider_evaluation"],
        "valid_case_count": report["valid_case_count"],
        "invalid_case_count": report["invalid_case_count"],
        "metrics": report["metrics"],
        "compatibility_arm": {
            "case_count": len(compat_results),
            "warm_success_count": sum(
                bool(item.get("task_success")) for item in compat_results
            ),
            "valid_case_count": sum(bool(item.get("valid")) for item in compat_results),
        },
        "case_status": [
            {
                "case_id": item.get("case_id"),
                "valid": item.get("valid"),
                "task_success": item.get("task_success"),
                "selected_count": len(item.get("selected_memory_ids", [])),
                "relevant_hits": item.get("relevant_hits"),
                "irrelevant_count": item.get("irrelevant_count"),
                "leakage": item.get("scope_revision_stale_deleted_leakage"),
                "prompt_injection_policy_bypass": item.get("prompt_injection_policy_bypass"),
                "no_memory_behavior_delta": item.get("no_memory_behavior_delta"),
            }
            for item in main_results
            if isinstance(item, Mapping)
        ],
    }


def run_suite(
    suite_path: str | Path,
    *,
    suite_sha256: str,
    algorithm_commit: str,
    runner_commit: str,
    execution_time: str | None = None,
    expected_sha256: str = EXPECTED_HOLDOUT_SHA256,
) -> dict[str, Any]:
    suite_bytes = Path(suite_path).read_bytes()
    actual_sha256 = sha256_bytes(suite_bytes)
    if actual_sha256 != expected_sha256:
        raise HoldoutRunnerError(
            f"suite SHA-256 mismatch: {actual_sha256} != {expected_sha256}"
        )
    if suite_sha256 != actual_sha256:
        raise HoldoutRunnerError("provided suite_sha256 does not match the suite bytes")
    raw = load_suite(suite_path)
    cases = _case_list(raw["cases"], "cases")
    compat_cases, compat_pools = _compatibility_cases(raw)
    repositories = _as_mapping(raw["repositories"], "repositories")
    pools = dict(_as_mapping(raw["memory_pools"], "memory_pools"))
    pools.update(compat_pools)
    now = DEFAULT_NOW
    with tempfile.TemporaryDirectory(prefix="memory-holdout-v2-") as temporary:
        temporary_root = Path(temporary)
        results = _run_case_group(
            cases,
            repositories,
            {case_pool: pools[case_pool] for case_pool in {case.memory_pool_id for case in cases}},
            temporary_root,
            now,
        )
        compat_results = _run_case_group(
            compat_cases,
            repositories,
            {case_pool: pools[case_pool] for case_pool in {case.memory_pool_id for case in compat_cases}},
            temporary_root / "compatibility",
            now,
        ) if compat_cases else []
        compatibility_metadata = raw.get("compatibility_arm")
        compatibility_report: dict[str, Any]
        if compat_cases:
            compatibility_report = {
                "source_commit": "v2-inline",
                "executor_kind": "observation_only",
                "provider_evaluation": False,
                "case_count": len(compat_results),
                "valid_case_count": sum(bool(item.get("valid")) for item in compat_results),
                "invalid_case_count": sum(not bool(item.get("valid")) for item in compat_results),
                "warm_success_count": sum(bool(item.get("task_success")) for item in compat_results),
                "warm_success_total": len(compat_results),
                "manifest_tokens_match_renderer": all(
                    bool(item.get("manifest_token_matches_renderer")) for item in compat_results
                ),
                "cases": compat_results,
            }
        elif isinstance(compatibility_metadata, Mapping) and int(compatibility_metadata.get("count", 0)) == 3:
            if str(compatibility_metadata.get("source_commit")) != "5298ba0":
                raise HoldoutRunnerError("unsupported compatibility source commit")
            try:
                compatibility_report = _run_legacy_compatibility(temporary_root / "compatibility")
            except (OSError, ValueError, KeyError, TypeError, HoldoutRunnerError) as exc:
                # Main case observations are already in memory and must remain
                # reportable if the historical control has an infrastructure fault.
                compatibility_report = {
                    "source_commit": "5298ba0",
                    "executor_kind": "legacy_scripted_compatibility",
                    "provider_evaluation": False,
                    "case_count": 3,
                    "valid_case_count": 0,
                    "invalid_case_count": 3,
                    "warm_success_count": 0,
                    "warm_success_total": 3,
                    "manifest_tokens_match_renderer": False,
                    "cases": [],
                    "failure_reason": f"{type(exc).__name__}: {exc}",
                }
        else:
            compatibility_report = {
                "source_commit": "none",
                "executor_kind": "not_present",
                "provider_evaluation": False,
                "case_count": 0,
                "valid_case_count": 0,
                "invalid_case_count": 0,
                "warm_success_count": 0,
                "warm_success_total": 0,
                "manifest_tokens_match_renderer": True,
                "cases": [],
            }
    timestamp = execution_time or datetime.now(timezone.utc).isoformat()
    valid_count = sum(bool(item.get("valid")) for item in results)
    report: dict[str, Any] = {
        "schema_version": RUNNER_SCHEMA_VERSION,
        "benchmark": "memory-retrieval-holdout-v2",
        "execution_time": timestamp,
        "suite_sha256": actual_sha256,
        "algorithm_commit": algorithm_commit,
        "runner_commit": runner_commit,
        "executor_kind": "observation_only",
        "provider_evaluation": False,
        "real_model_claim": False,
        "main_case_count": len(results),
        "valid_case_count": valid_count,
        "invalid_case_count": len(results) - valid_count,
        "metrics": _aggregate(results),
        "cases": results,
        "compatibility_arm": compatibility_report,
        "safety": {
            "algorithm_source": "coding_agent.memory.retrieval.LexicalMemoryRetriever",
            "context_renderer_source": "coding_agent.context.BudgetedContextBuilder",
            "answer_key_used": False,
            "fixture_mutations": False,
            "absolute_paths_in_report": False,
        },
    }
    return report


def write_result_bundle(report: Mapping[str, Any], output_dir: str | Path) -> dict[str, str]:
    output = Path(output_dir)
    if output.exists():
        raise HoldoutRunnerError("result output directory already exists; refusing a second run")
    output.mkdir(parents=True)
    raw_path = output / "raw-result.json"
    summary_path = output / "redacted-summary.json"
    raw_bytes = (json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
    raw_path.write_bytes(raw_bytes)
    summary = _redacted_summary(report)
    summary_bytes = (json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
    summary_path.write_bytes(summary_bytes)
    metadata = {
        "schema_version": RUNNER_SCHEMA_VERSION,
        "suite_sha256": report["suite_sha256"],
        "algorithm_commit": report["algorithm_commit"],
        "runner_commit": report["runner_commit"],
        "execution_time": report["execution_time"],
        "valid_case_count": report["valid_case_count"],
        "invalid_case_count": report["invalid_case_count"],
        "raw_result_sha256": sha256_bytes(raw_bytes),
        "redacted_summary_sha256": sha256_bytes(summary_bytes),
        "raw_result_file": "raw-result.json",
        "redacted_summary_file": "redacted-summary.json",
    }
    (output / "execution-metadata.json").write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return {
        "raw_result_sha256": str(metadata["raw_result_sha256"]),
        "redacted_summary_sha256": str(metadata["redacted_summary_sha256"]),
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--suite", required=True, type=Path)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--algorithm-commit", required=True)
    parser.add_argument("--runner-commit", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    manifest = load_manifest(arguments.manifest)
    storage = _as_mapping(manifest.get("body_storage"), "manifest body_storage")
    expected_sha = str(manifest.get("suite_sha256", storage.get("content_sha256", "")))
    execution = _as_mapping(manifest.get("execution"), "manifest execution")
    if bool(execution.get("executed")) or bool(execution.get("results_generated")):
        raise HoldoutRunnerError("manifest says Holdout v2 has already executed")
    if not bool(execution.get("first_run_reserved")):
        raise HoldoutRunnerError("manifest does not reserve the first run")
    actual_sha = sha256_file(arguments.suite)
    if actual_sha != expected_sha:
        raise HoldoutRunnerError(f"suite SHA-256 mismatch before reading body: {actual_sha} != {expected_sha}")
    report = run_suite(
        arguments.suite,
        suite_sha256=actual_sha,
        algorithm_commit=arguments.algorithm_commit,
        runner_commit=arguments.runner_commit,
    )
    hashes = write_result_bundle(report, arguments.output)
    print(
        json.dumps(
            {
                "result_dir": arguments.output.name,
                "raw_result_sha256": hashes["raw_result_sha256"],
                "redacted_summary_sha256": hashes["redacted_summary_sha256"],
                "valid_case_count": report["valid_case_count"],
                "invalid_case_count": report["invalid_case_count"],
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except HoldoutRunnerError as exc:
        print(f"holdout runner error: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
