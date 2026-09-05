from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Mapping, Sequence


JsonObject = dict[str, Any]


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class RuntimeState(str, Enum):
    CREATED = "created"
    PREPARING_WORKSPACE = "preparing_workspace"
    BUILDING_CONTEXT = "building_context"
    CALLING_MODEL = "calling_model"
    DISPATCHING_TOOL = "dispatching_tool"
    RECORDING_OBSERVATION = "recording_observation"
    INTERRUPTED = "interrupted"
    WAITING_APPROVAL = "waiting_approval"
    RETRY_WAIT = "retry_wait"
    COMPLETED = "completed"
    FAILED = "failed"


TERMINAL_STATES = frozenset({RuntimeState.COMPLETED, RuntimeState.FAILED})


ALLOWED_TRANSITIONS: Mapping[RuntimeState, frozenset[RuntimeState]] = {
    RuntimeState.CREATED: frozenset(
        {
            RuntimeState.PREPARING_WORKSPACE,
            RuntimeState.INTERRUPTED,
            RuntimeState.FAILED,
        }
    ),
    RuntimeState.PREPARING_WORKSPACE: frozenset(
        {RuntimeState.BUILDING_CONTEXT, RuntimeState.INTERRUPTED, RuntimeState.FAILED}
    ),
    RuntimeState.BUILDING_CONTEXT: frozenset(
        {RuntimeState.CALLING_MODEL, RuntimeState.INTERRUPTED, RuntimeState.FAILED}
    ),
    RuntimeState.CALLING_MODEL: frozenset(
        {
            RuntimeState.DISPATCHING_TOOL,
            RuntimeState.COMPLETED,
            RuntimeState.RETRY_WAIT,
            RuntimeState.INTERRUPTED,
            RuntimeState.FAILED,
        }
    ),
    RuntimeState.DISPATCHING_TOOL: frozenset(
        {
            RuntimeState.RECORDING_OBSERVATION,
            RuntimeState.WAITING_APPROVAL,
            RuntimeState.INTERRUPTED,
            RuntimeState.FAILED,
        }
    ),
    RuntimeState.RECORDING_OBSERVATION: frozenset(
        {
            RuntimeState.DISPATCHING_TOOL,
            RuntimeState.BUILDING_CONTEXT,
            RuntimeState.INTERRUPTED,
            RuntimeState.FAILED,
        }
    ),
    RuntimeState.INTERRUPTED: frozenset(
        {
            RuntimeState.CREATED,
            RuntimeState.PREPARING_WORKSPACE,
            RuntimeState.BUILDING_CONTEXT,
            RuntimeState.CALLING_MODEL,
            RuntimeState.RETRY_WAIT,
            RuntimeState.DISPATCHING_TOOL,
            RuntimeState.RECORDING_OBSERVATION,
            RuntimeState.FAILED,
        }
    ),
    RuntimeState.WAITING_APPROVAL: frozenset(
        {
            RuntimeState.DISPATCHING_TOOL,
            RuntimeState.RECORDING_OBSERVATION,
            RuntimeState.FAILED,
        }
    ),
    RuntimeState.RETRY_WAIT: frozenset(
        {RuntimeState.CALLING_MODEL, RuntimeState.INTERRUPTED, RuntimeState.FAILED}
    ),
    RuntimeState.COMPLETED: frozenset(),
    RuntimeState.FAILED: frozenset(),
}


class Permission(str, Enum):
    READ = "read"
    WRITE = "write"
    EXECUTE_TEST = "execute_test"
    EXECUTE_COMMAND = "execute_command"


class ToolStatus(str, Enum):
    SUCCESS = "success"
    INVALID_ARGUMENTS = "invalid_arguments"
    PERMISSION_DENIED = "permission_denied"
    TIMEOUT = "timeout"
    NOT_FOUND = "not_found"
    EXECUTION_ERROR = "execution_error"


class ToolCallState(str, Enum):
    PREPARED = "prepared"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    UNCERTAIN = "uncertain"


class RecoveryMode(str, Enum):
    READ_ONLY = "read_only"
    RECONCILABLE_WRITE = "reconcilable_write"
    REPEATABLE_OBSERVATION = "repeatable_observation"
    NON_IDEMPOTENT = "non_idempotent"


class EventType(str, Enum):
    SESSION_CREATED = "session_created"
    MESSAGE_ADDED = "message_added"
    STATE_TRANSITION = "state_transition"
    WORKSPACE_CREATED = "workspace_created"
    CONTEXT_BUILT = "context_built"
    MODEL_CALL_STARTED = "model_call_started"
    MODEL_CALL_SUCCEEDED = "model_call_succeeded"
    MODEL_CALL_FAILED = "model_call_failed"
    MODEL_CALL_UNCERTAIN = "model_call_uncertain"
    TOOL_CALL_STARTED = "tool_call_started"
    TOOL_CALL_PREPARED = "tool_call_prepared"
    TOOL_CALL_RUNNING = "tool_call_running"
    TOOL_CALL_FINISHED = "tool_call_finished"
    TOOL_CALL_UNCERTAIN = "tool_call_uncertain"
    TOOL_RESULT_REATTACHED = "tool_result_reattached"
    APPROVAL_REQUESTED = "approval_requested"
    CALL_RESOLVED = "call_resolved"
    RETRY_SCHEDULED = "retry_scheduled"
    FALLBACK_SELECTED = "fallback_selected"
    RESUME_STARTED = "resume_started"
    COMPRESSION_STARTED = "compression_started"
    COMPRESSION_FINISHED = "compression_finished"
    COMPRESSION_REJECTED = "compression_rejected"
    SUMMARY_INVALIDATED = "summary_invalidated"
    RUN_FINISHED = "run_finished"


@dataclass(frozen=True)
class Message:
    role: str
    content: str
    tool_call_id: str | None = None
    metadata: JsonObject = field(default_factory=dict)

    def to_dict(self) -> JsonObject:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "Message":
        metadata = raw.get("metadata", {})
        if not isinstance(metadata, Mapping):
            raise ValueError("message metadata must be an object")
        return cls(
            role=str(raw["role"]),
            content=str(raw.get("content", "")),
            tool_call_id=(
                str(raw["tool_call_id"])
                if raw.get("tool_call_id") is not None
                else None
            ),
            metadata=dict(metadata),
        )


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    arguments: JsonObject

    def to_dict(self) -> JsonObject:
        return {"id": self.id, "name": self.name, "arguments": self.arguments}

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "ToolCall":
        arguments = raw.get("arguments", {})
        if not isinstance(arguments, Mapping):
            raise ValueError("tool call arguments must be an object")
        return cls(
            id=str(raw["id"]),
            name=str(raw["name"]),
            arguments=dict(arguments),
        )


@dataclass(frozen=True)
class ToolResult:
    call_id: str
    tool_name: str
    status: ToolStatus
    data: JsonObject = field(default_factory=dict)
    error: JsonObject | None = None
    duration_ms: float = 0.0
    truncated: bool = False

    @property
    def ok(self) -> bool:
        return self.status is ToolStatus.SUCCESS

    def to_dict(self) -> JsonObject:
        return {
            "call_id": self.call_id,
            "tool_name": self.tool_name,
            "status": self.status.value,
            "data": self.data,
            "error": self.error,
            "duration_ms": self.duration_ms,
            "truncated": self.truncated,
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "ToolResult":
        data = raw.get("data", {})
        error = raw.get("error")
        if not isinstance(data, Mapping):
            raise ValueError("tool result data must be an object")
        if error is not None and not isinstance(error, Mapping):
            raise ValueError("tool result error must be an object or null")
        return cls(
            call_id=str(raw["call_id"]),
            tool_name=str(raw["tool_name"]),
            status=ToolStatus(str(raw["status"])),
            data=dict(data),
            error=dict(error) if error is not None else None,
            duration_ms=float(raw.get("duration_ms", 0.0)),
            truncated=bool(raw.get("truncated", False)),
        )


@dataclass(frozen=True)
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0

    def to_dict(self) -> JsonObject:
        return asdict(self)


@dataclass(frozen=True)
class ModelRequest:
    request_id: str
    messages: Sequence[Message]
    tools: Sequence[JsonObject]
    max_output_tokens: int
    metadata: JsonObject = field(default_factory=dict)

    def to_dict(self) -> JsonObject:
        return {
            "request_id": self.request_id,
            "messages": [message.to_dict() for message in self.messages],
            "tools": [dict(tool) for tool in self.tools],
            "max_output_tokens": self.max_output_tokens,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True)
class ModelResponse:
    text: str = ""
    tool_calls: Sequence[ToolCall] = field(default_factory=tuple)
    usage: Usage = field(default_factory=Usage)
    finish_reason: str = "stop"
    provider_metadata: JsonObject = field(default_factory=dict)

    def to_dict(self) -> JsonObject:
        return {
            "text": self.text,
            "tool_calls": [call.to_dict() for call in self.tool_calls],
            "usage": self.usage.to_dict(),
            "finish_reason": self.finish_reason,
            "provider_metadata": dict(self.provider_metadata),
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "ModelResponse":
        if not isinstance(raw, Mapping):
            raise ValueError("model response must be an object")
        raw_calls = raw.get("tool_calls", [])
        usage = raw.get("usage", {})
        metadata = raw.get("provider_metadata", {})
        if not isinstance(raw_calls, list):
            raise ValueError("model response tool_calls must be an array")
        if not isinstance(usage, Mapping):
            raise ValueError("model response usage must be an object")
        if not isinstance(metadata, Mapping):
            raise ValueError("model response provider_metadata must be an object")
        if any(not isinstance(call, Mapping) for call in raw_calls):
            raise ValueError("model response tool calls must be objects")
        return cls(
            text=str(raw.get("text", "")),
            tool_calls=tuple(
                ToolCall.from_dict(call) for call in raw_calls
            ),
            usage=Usage(
                input_tokens=int(usage.get("input_tokens", 0)),
                output_tokens=int(usage.get("output_tokens", 0)),
            ),
            finish_reason=str(raw.get("finish_reason", "stop")),
            provider_metadata=dict(metadata),
        )


@dataclass(frozen=True)
class Event:
    schema_version: int
    event_id: str
    session_id: str
    sequence: int
    event_type: EventType
    timestamp: str
    state: RuntimeState
    payload: JsonObject

    def to_dict(self) -> JsonObject:
        return {
            "schema_version": self.schema_version,
            "event_id": self.event_id,
            "session_id": self.session_id,
            "sequence": self.sequence,
            "event_type": self.event_type.value,
            "timestamp": self.timestamp,
            "state": self.state.value,
            "payload": self.payload,
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "Event":
        return cls(
            schema_version=int(raw["schema_version"]),
            event_id=str(raw["event_id"]),
            session_id=str(raw["session_id"]),
            sequence=int(raw["sequence"]),
            event_type=EventType(str(raw["event_type"])),
            timestamp=str(raw["timestamp"]),
            state=RuntimeState(str(raw["state"])),
            payload=dict(raw.get("payload", {})),
        )


@dataclass(frozen=True)
class RunPolicy:
    max_steps: int = 32
    max_model_calls: int = 8
    max_tool_calls: int = 12
    max_output_tokens: int = 2048
    max_retries: int = 2
    retry_base_delay_seconds: float = 1.0
    retry_max_delay_seconds: float = 30.0
    max_retry_wait_seconds: float = 120.0
    retry_jitter_seconds: float = 0.0
    allowed_permissions: frozenset[Permission] = field(
        default_factory=lambda: frozenset(Permission)
    )

    def to_dict(self) -> JsonObject:
        return {
            "max_steps": self.max_steps,
            "max_model_calls": self.max_model_calls,
            "max_tool_calls": self.max_tool_calls,
            "max_output_tokens": self.max_output_tokens,
            "max_retries": self.max_retries,
            "retry_base_delay_seconds": self.retry_base_delay_seconds,
            "retry_max_delay_seconds": self.retry_max_delay_seconds,
            "max_retry_wait_seconds": self.max_retry_wait_seconds,
            "retry_jitter_seconds": self.retry_jitter_seconds,
            "allowed_permissions": sorted(
                permission.value for permission in self.allowed_permissions
            ),
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "RunPolicy":
        allowed_permissions = raw.get(
            "allowed_permissions", [permission.value for permission in Permission]
        )
        if not isinstance(allowed_permissions, (list, tuple, set, frozenset)):
            raise ValueError("allowed_permissions must be an array")
        try:
            permissions = frozenset(Permission(str(value)) for value in allowed_permissions)
        except ValueError as exc:
            raise ValueError("policy contains an unknown permission") from exc
        return cls(
            max_steps=int(raw.get("max_steps", 32)),
            max_model_calls=int(raw.get("max_model_calls", 8)),
            max_tool_calls=int(raw.get("max_tool_calls", 12)),
            max_output_tokens=int(raw.get("max_output_tokens", 2048)),
            max_retries=int(raw.get("max_retries", 2)),
            retry_base_delay_seconds=float(raw.get("retry_base_delay_seconds", 1.0)),
            retry_max_delay_seconds=float(raw.get("retry_max_delay_seconds", 30.0)),
            max_retry_wait_seconds=float(raw.get("max_retry_wait_seconds", 120.0)),
            retry_jitter_seconds=float(raw.get("retry_jitter_seconds", 0.0)),
            allowed_permissions=permissions,
        )


@dataclass(frozen=True)
class FileFact:
    """A file fact carried by a validated, derived context summary."""

    path: str
    content_hash: str
    revision: str = ""
    source_event_sequence: int | None = None
    stale: bool = False

    def to_dict(self) -> JsonObject:
        return {
            "path": self.path,
            "content_hash": self.content_hash,
            "revision": self.revision,
            "source_event_sequence": self.source_event_sequence,
            "stale": self.stale,
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "FileFact":
        if not isinstance(raw, Mapping):
            raise ValueError("file fact must be an object")
        return cls(
            path=str(raw["path"]),
            content_hash=str(raw["content_hash"]),
            revision=str(raw.get("revision", "")),
            source_event_sequence=(
                int(raw["source_event_sequence"])
                if raw.get("source_event_sequence") is not None
                else None
            ),
            stale=bool(raw.get("stale", False)),
        )


@dataclass(frozen=True)
class EditFact:
    """A derived description of an edit; it is never an authorization input."""

    path: str
    pre_revision: str = ""
    post_revision: str = ""
    content_hash: str = ""
    source_event_sequence: int | None = None
    stale: bool = False

    def to_dict(self) -> JsonObject:
        return {
            "path": self.path,
            "pre_revision": self.pre_revision,
            "post_revision": self.post_revision,
            "content_hash": self.content_hash,
            "source_event_sequence": self.source_event_sequence,
            "stale": self.stale,
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "EditFact":
        if not isinstance(raw, Mapping):
            raise ValueError("edit fact must be an object")
        return cls(
            path=str(raw["path"]),
            pre_revision=str(raw.get("pre_revision", "")),
            post_revision=str(raw.get("post_revision", "")),
            content_hash=str(raw.get("content_hash", "")),
            source_event_sequence=(
                int(raw["source_event_sequence"])
                if raw.get("source_event_sequence") is not None
                else None
            ),
            stale=bool(raw.get("stale", False)),
        )


@dataclass(frozen=True)
class TestFact:
    """A structured test observation suitable for context retention and evals."""

    profile: str
    passed: bool | None
    summary: str = ""
    stderr_summary: str = ""
    source_event_sequence: int | None = None

    def to_dict(self) -> JsonObject:
        return {
            "profile": self.profile,
            "passed": self.passed,
            "summary": self.summary,
            "stderr_summary": self.stderr_summary,
            "source_event_sequence": self.source_event_sequence,
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "TestFact":
        if not isinstance(raw, Mapping):
            raise ValueError("test fact must be an object")
        passed = raw.get("passed")
        if passed is not None and not isinstance(passed, bool):
            raise ValueError("test fact passed must be boolean or null")
        return cls(
            profile=str(raw["profile"]),
            passed=passed,
            summary=str(raw.get("summary", "")),
            stderr_summary=str(raw.get("stderr_summary", "")),
            source_event_sequence=(
                int(raw["source_event_sequence"])
                if raw.get("source_event_sequence") is not None
                else None
            ),
        )


@dataclass(frozen=True)
class ErrorFact:
    """A classified error retained for reasoning and failure analysis."""

    kind: str
    message: str
    source_event_sequence: int | None = None
    active: bool = True

    def to_dict(self) -> JsonObject:
        return {
            "kind": self.kind,
            "message": self.message,
            "source_event_sequence": self.source_event_sequence,
            "active": self.active,
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "ErrorFact":
        if not isinstance(raw, Mapping):
            raise ValueError("error fact must be an object")
        return cls(
            kind=str(raw["kind"]),
            message=str(raw.get("message", "")),
            source_event_sequence=(
                int(raw["source_event_sequence"])
                if raw.get("source_event_sequence") is not None
                else None
            ),
            active=bool(raw.get("active", True)),
        )


@dataclass(frozen=True)
class RepositorySnapshot:
    """Bounded metadata from the isolated workspace, never raw source authority."""

    workspace_revision: str
    file_paths: tuple[str, ...] = ()
    diff_summary: str = ""
    read_files: tuple[FileFact, ...] = ()
    last_test: TestFact | None = None

    @property
    def revision(self) -> str:
        return self.workspace_revision

    @property
    def files(self) -> tuple[str, ...]:
        return self.file_paths

    @property
    def current_diff(self) -> str:
        return self.diff_summary

    @property
    def latest_test(self) -> TestFact | None:
        return self.last_test

    def to_dict(self) -> JsonObject:
        return {
            "workspace_revision": self.workspace_revision,
            "file_paths": list(self.file_paths),
            "diff_summary": self.diff_summary,
            "read_files": [fact.to_dict() for fact in self.read_files],
            "last_test": self.last_test.to_dict() if self.last_test is not None else None,
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "RepositorySnapshot":
        if not isinstance(raw, Mapping):
            raise ValueError("repository snapshot must be an object")
        file_paths = raw.get("file_paths", raw.get("files", []))
        read_files = raw.get("read_files", [])
        last_test = raw.get("last_test", raw.get("latest_test"))
        if not isinstance(file_paths, (list, tuple)):
            raise ValueError("repository snapshot file_paths must be an array")
        if not isinstance(read_files, (list, tuple)):
            raise ValueError("repository snapshot read_files must be an array")
        if last_test is not None and not isinstance(last_test, Mapping):
            raise ValueError("repository snapshot last_test must be an object or null")
        return cls(
            workspace_revision=str(raw["workspace_revision"]),
            file_paths=tuple(str(path) for path in file_paths),
            diff_summary=str(raw.get("diff_summary", raw.get("current_diff", ""))),
            read_files=tuple(FileFact.from_dict(fact) for fact in read_files),
            last_test=TestFact.from_dict(last_test) if last_test is not None else None,
        )


@dataclass(frozen=True)
class ContextBuildInput:
    session_id: str
    task: str
    messages: Sequence[Message]
    runtime_state: RuntimeState
    policy: RunPolicy
    repository_snapshot: RepositorySnapshot
    latest_summary: "SummaryRecord | None"
    provider: str
    model: str
    pending_tool_calls: Sequence[ToolCall] = ()
    active_call_id: str | None = None
    active_call_kind: str | None = None

    def to_dict(self) -> JsonObject:
        return {
            "session_id": self.session_id,
            "task": self.task,
            "messages": [message.to_dict() for message in self.messages],
            "runtime_state": self.runtime_state.value,
            "policy": self.policy.to_dict(),
            "repository_snapshot": self.repository_snapshot.to_dict(),
            "latest_summary": (
                self.latest_summary.to_dict() if self.latest_summary is not None else None
            ),
            "provider": self.provider,
            "model": self.model,
            "pending_tool_calls": [call.to_dict() for call in self.pending_tool_calls],
            "active_call_id": self.active_call_id,
            "active_call_kind": self.active_call_kind,
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "ContextBuildInput":
        if not isinstance(raw, Mapping):
            raise ValueError("context build input must be an object")
        messages = raw.get("messages", [])
        pending = raw.get("pending_tool_calls", [])
        repository = raw.get("repository_snapshot")
        latest_summary = raw.get("latest_summary")
        if not isinstance(messages, list) or any(not isinstance(item, Mapping) for item in messages):
            raise ValueError("context messages must be an array of objects")
        if not isinstance(pending, list) or any(not isinstance(item, Mapping) for item in pending):
            raise ValueError("context pending tool calls must be an array of objects")
        if not isinstance(repository, Mapping):
            raise ValueError("context repository snapshot must be an object")
        if latest_summary is not None and not isinstance(latest_summary, Mapping):
            raise ValueError("context latest summary must be an object or null")
        return cls(
            session_id=str(raw["session_id"]),
            task=str(raw["task"]),
            messages=tuple(Message.from_dict(item) for item in messages),
            runtime_state=RuntimeState(str(raw["runtime_state"])),
            policy=RunPolicy.from_dict(raw.get("policy", {})),
            repository_snapshot=RepositorySnapshot.from_dict(repository),
            latest_summary=(
                SummaryRecord.from_dict(latest_summary)
                if latest_summary is not None
                else None
            ),
            provider=str(raw["provider"]),
            model=str(raw["model"]),
            pending_tool_calls=tuple(ToolCall.from_dict(item) for item in pending),
            active_call_id=(
                str(raw["active_call_id"])
                if raw.get("active_call_id") is not None
                else None
            ),
            active_call_kind=(
                str(raw["active_call_kind"])
                if raw.get("active_call_kind") is not None
                else None
            ),
        )


@dataclass(frozen=True)
class ContextSection:
    name: str
    messages: tuple[Message, ...]
    estimated_tokens: int
    source_refs: tuple[str, ...] = ()
    truncated: bool = False

    def manifest(self) -> JsonObject:
        return {
            "name": self.name,
            "tokens": self.estimated_tokens,
            "estimated_tokens": self.estimated_tokens,
            "source_refs": list(self.source_refs),
            "truncated": self.truncated,
            "message_count": len(self.messages),
        }

    def to_dict(self) -> JsonObject:
        return {
            **self.manifest(),
            "messages": [message.to_dict() for message in self.messages],
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "ContextSection":
        messages = raw.get("messages", [])
        if not isinstance(messages, list) or any(not isinstance(item, Mapping) for item in messages):
            raise ValueError("context section messages must be an array of objects")
        refs = raw.get("source_refs", [])
        if not isinstance(refs, list):
            raise ValueError("context section source_refs must be an array")
        return cls(
            name=str(raw["name"]),
            messages=tuple(Message.from_dict(item) for item in messages),
            estimated_tokens=int(raw.get("estimated_tokens", raw.get("tokens", 0))),
            source_refs=tuple(str(ref) for ref in refs),
            truncated=bool(raw.get("truncated", False)),
        )


@dataclass(frozen=True)
class BuiltContext:
    messages: tuple[Message, ...]
    sections: tuple[ContextSection, ...]
    total_input_tokens: int
    budget_tokens: int
    manifest_version: int = 1
    provider: str = ""
    model: str = ""
    workspace_revision: str = ""
    counter: str = ""
    capability_source: str = "registry"
    last_test: JsonObject | None = None
    summary_id: str | None = None
    compressed: bool = False
    pre_compression_input_tokens: int | None = None
    high_watermark_tokens: int | None = None
    target_after_compression_tokens: int | None = None

    @property
    def needs_compression(self) -> bool:
        if self.compressed:
            return False
        if self.high_watermark_tokens is None:
            return False
        source_tokens = (
            self.pre_compression_input_tokens
            if self.pre_compression_input_tokens is not None
            else self.total_input_tokens
        )
        return source_tokens > self.high_watermark_tokens

    def manifest(self) -> JsonObject:
        return {
            "manifest_version": self.manifest_version,
            "provider": self.provider,
            "model": self.model,
            "budget_tokens": self.budget_tokens,
            "total_input_tokens": self.total_input_tokens,
            "pre_compression_input_tokens": self.pre_compression_input_tokens,
            "high_watermark_tokens": self.high_watermark_tokens,
            "target_after_compression_tokens": self.target_after_compression_tokens,
            "sections": [section.manifest() for section in self.sections],
            "summary_id": self.summary_id,
            "workspace_revision": self.workspace_revision,
            "counter": self.counter,
            "capability_source": self.capability_source,
            "last_test": self.last_test,
            "compressed": self.compressed,
        }

    def to_dict(self) -> JsonObject:
        return {
            **self.manifest(),
            "messages": [message.to_dict() for message in self.messages],
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, sort_keys=True)

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "BuiltContext":
        messages = raw.get("messages", [])
        sections = raw.get("sections", [])
        if not isinstance(messages, list) or any(not isinstance(item, Mapping) for item in messages):
            raise ValueError("built context messages must be an array of objects")
        if not isinstance(sections, list) or any(not isinstance(item, Mapping) for item in sections):
            raise ValueError("built context sections must be an array of objects")
        return cls(
            messages=tuple(Message.from_dict(item) for item in messages),
            sections=tuple(ContextSection.from_dict(item) for item in sections),
            total_input_tokens=int(raw["total_input_tokens"]),
            budget_tokens=int(raw["budget_tokens"]),
            manifest_version=int(raw.get("manifest_version", 1)),
            provider=str(raw.get("provider", "")),
            model=str(raw.get("model", "")),
            workspace_revision=str(raw.get("workspace_revision", "")),
            counter=str(raw.get("counter", "")),
            capability_source=str(raw.get("capability_source", "registry")),
            last_test=(dict(raw["last_test"]) if isinstance(raw.get("last_test"), Mapping) else None),
            summary_id=(str(raw["summary_id"]) if raw.get("summary_id") is not None else None),
            compressed=bool(raw.get("compressed", False)),
            pre_compression_input_tokens=(
                int(raw["pre_compression_input_tokens"])
                if raw.get("pre_compression_input_tokens") is not None
                else None
            ),
            high_watermark_tokens=(
                int(raw["high_watermark_tokens"])
                if raw.get("high_watermark_tokens") is not None
                else None
            ),
            target_after_compression_tokens=(
                int(raw["target_after_compression_tokens"])
                if raw.get("target_after_compression_tokens") is not None
                else None
            ),
        )


@dataclass(frozen=True)
class SummaryRecord:
    """Validated derived cache; original events remain the source of truth."""

    summary_id: str
    schema_version: int
    session_id: str
    source_event_start: int
    source_event_end: int
    source_event_hash: str
    workspace_revision: str
    goals: tuple[str, ...] = ()
    constraints: tuple[str, ...] = ()
    decisions: tuple[str, ...] = ()
    files_read: tuple[FileFact, ...] = ()
    edits: tuple[EditFact, ...] = ()
    tests: tuple[TestFact, ...] = ()
    errors: tuple[ErrorFact, ...] = ()
    unresolved: tuple[str, ...] = ()
    created_at: str = field(default_factory=utc_now)
    source_message_end: int | None = None
    superseded_by: str | None = None
    stale: bool = False

    def __post_init__(self) -> None:
        if not self.summary_id or not self.session_id:
            raise ValueError("summary id and session id are required")
        if self.schema_version != 1:
            raise ValueError("unsupported summary schema version")
        if self.source_event_start <= 0 or self.source_event_end < self.source_event_start:
            raise ValueError("summary event range is invalid")
        if not self.source_event_hash or not self.workspace_revision:
            raise ValueError("summary lineage hash and workspace revision are required")
        if self.source_message_end is not None and self.source_message_end < 0:
            raise ValueError("summary source message end cannot be negative")

    def to_dict(self) -> JsonObject:
        return {
            "summary_id": self.summary_id,
            "schema_version": self.schema_version,
            "session_id": self.session_id,
            "source_event_start": self.source_event_start,
            "source_event_end": self.source_event_end,
            "source_event_hash": self.source_event_hash,
            "workspace_revision": self.workspace_revision,
            "goals": list(self.goals),
            "constraints": list(self.constraints),
            "decisions": list(self.decisions),
            "files_read": [fact.to_dict() for fact in self.files_read],
            "edits": [fact.to_dict() for fact in self.edits],
            "tests": [fact.to_dict() for fact in self.tests],
            "errors": [fact.to_dict() for fact in self.errors],
            "unresolved": list(self.unresolved),
            "created_at": self.created_at,
            "source_message_end": self.source_message_end,
            "superseded_by": self.superseded_by,
            "stale": self.stale,
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "SummaryRecord":
        if not isinstance(raw, Mapping):
            raise ValueError("summary record must be an object")
        def strings(name: str) -> tuple[str, ...]:
            values = raw.get(name, [])
            if not isinstance(values, (list, tuple)):
                raise ValueError(f"summary {name} must be an array")
            return tuple(str(value) for value in values)

        def objects(name: str, factory: Any) -> tuple[Any, ...]:
            values = raw.get(name, [])
            if not isinstance(values, (list, tuple)):
                raise ValueError(f"summary {name} must be an array")
            if any(not isinstance(value, Mapping) for value in values):
                raise ValueError(f"summary {name} entries must be objects")
            return tuple(factory(value) for value in values)

        return cls(
            summary_id=str(raw["summary_id"]),
            schema_version=int(raw.get("schema_version", 1)),
            session_id=str(raw["session_id"]),
            source_event_start=int(raw["source_event_start"]),
            source_event_end=int(raw["source_event_end"]),
            source_event_hash=str(raw["source_event_hash"]),
            workspace_revision=str(raw["workspace_revision"]),
            goals=strings("goals"),
            constraints=strings("constraints"),
            decisions=strings("decisions"),
            files_read=objects("files_read", FileFact.from_dict),
            edits=objects("edits", EditFact.from_dict),
            tests=objects("tests", TestFact.from_dict),
            errors=objects("errors", ErrorFact.from_dict),
            unresolved=strings("unresolved"),
            created_at=str(raw.get("created_at", utc_now())),
            source_message_end=(
                int(raw["source_message_end"])
                if raw.get("source_message_end") is not None
                else None
            ),
            superseded_by=(
                str(raw["superseded_by"])
                if raw.get("superseded_by") is not None
                else None
            ),
            stale=bool(raw.get("stale", False)),
        )


SNAPSHOT_VERSION = 2
SUPPORTED_SNAPSHOT_VERSIONS = frozenset({1, SNAPSHOT_VERSION})


@dataclass(frozen=True)
class RuntimeSnapshot:
    """Explicit, versioned JSON state persisted by the SQLite journal."""

    session_id: str
    task: str
    source_path: str
    state: RuntimeState
    policy: RunPolicy
    source_fingerprint: str
    workspace_path: str | None = None
    pending_tool_calls: tuple[ToolCall, ...] = ()
    active_tool_result: ToolResult | None = None
    active_call_id: str | None = None
    active_call_kind: str | None = None
    resume_target_state: RuntimeState | None = None
    retry_metadata: JsonObject = field(default_factory=dict)
    interrupt_requested_at: str | None = None
    context_version: str = "1"
    final_answer: str | None = None
    failure: JsonObject | None = None
    step_count: int = 0
    model_calls: int = 0
    tool_calls: int = 0
    created_at: str = field(default_factory=utc_now)
    updated_at: str = field(default_factory=utc_now)
    version: int = 0
    snapshot_version: int = SNAPSHOT_VERSION

    @classmethod
    def from_session(
        cls,
        session: "Session",
        *,
        state: RuntimeState | None = None,
        version: int | None = None,
        updated_at: str | None = None,
    ) -> "RuntimeSnapshot":
        return cls(
            session_id=session.id,
            task=session.task,
            source_path=session.source_path,
            state=state or session.state,
            policy=session.policy,
            source_fingerprint=session.source_fingerprint,
            workspace_path=session.workspace_path,
            pending_tool_calls=tuple(session.pending_tool_calls),
            active_tool_result=session.active_tool_result,
            active_call_id=session.active_call_id,
            active_call_kind=session.active_call_kind,
            resume_target_state=session.resume_target_state,
            retry_metadata=dict(session.retry_metadata),
            interrupt_requested_at=session.interrupt_requested_at,
            context_version=session.context_version,
            final_answer=session.final_answer,
            failure=dict(session.failure) if session.failure is not None else None,
            step_count=session.step_count,
            model_calls=session.model_calls,
            tool_calls=session.tool_calls,
            created_at=session.created_at,
            updated_at=updated_at or session.updated_at,
            version=session.version if version is None else version,
        )

    def to_dict(self) -> JsonObject:
        return {
            "snapshot_version": self.snapshot_version,
            "session_id": self.session_id,
            "task": self.task,
            "source_path": self.source_path,
            "state": self.state.value,
            "policy": self.policy.to_dict(),
            "source_fingerprint": self.source_fingerprint,
            "workspace_path": self.workspace_path,
            "pending_tool_calls": [call.to_dict() for call in self.pending_tool_calls],
            "active_tool_result": (
                self.active_tool_result.to_dict()
                if self.active_tool_result is not None
                else None
            ),
            "active_call_id": self.active_call_id,
            "active_call_kind": self.active_call_kind,
            "resume_target_state": (
                self.resume_target_state.value
                if self.resume_target_state is not None
                else None
            ),
            "retry_metadata": dict(self.retry_metadata),
            "interrupt_requested_at": self.interrupt_requested_at,
            "context_version": self.context_version,
            "final_answer": self.final_answer,
            "failure": self.failure,
            "step_count": self.step_count,
            "model_calls": self.model_calls,
            "tool_calls": self.tool_calls,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "version": self.version,
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, sort_keys=True)

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "RuntimeSnapshot":
        if not isinstance(raw, Mapping):
            raise ValueError("runtime snapshot must be an object")
        version = int(raw.get("snapshot_version", -1))
        if version not in SUPPORTED_SNAPSHOT_VERSIONS:
            raise ValueError(f"unsupported runtime snapshot version: {version}")
        policy = raw.get("policy")
        pending_tool_calls = raw.get("pending_tool_calls", [])
        active_tool_result = raw.get("active_tool_result")
        failure = raw.get("failure")
        retry_metadata = raw.get("retry_metadata", {})
        if not isinstance(policy, Mapping):
            raise ValueError("snapshot policy must be an object")
        if not isinstance(pending_tool_calls, list):
            raise ValueError("snapshot pending_tool_calls must be an array")
        if any(not isinstance(call, Mapping) for call in pending_tool_calls):
            raise ValueError("snapshot pending tool calls must be objects")
        if active_tool_result is not None and not isinstance(active_tool_result, Mapping):
            raise ValueError("snapshot active_tool_result must be an object or null")
        if failure is not None and not isinstance(failure, Mapping):
            raise ValueError("snapshot failure must be an object or null")
        if not isinstance(retry_metadata, Mapping):
            raise ValueError("snapshot retry_metadata must be an object")
        resume_target = raw.get("resume_target_state")
        return cls(
            session_id=str(raw["session_id"]),
            task=str(raw["task"]),
            source_path=str(raw["source_path"]),
            state=RuntimeState(str(raw["state"])),
            policy=RunPolicy.from_dict(policy),
            source_fingerprint=str(raw["source_fingerprint"]),
            workspace_path=(
                str(raw["workspace_path"])
                if raw.get("workspace_path") is not None
                else None
            ),
            pending_tool_calls=tuple(
                ToolCall.from_dict(call) for call in pending_tool_calls
            ),
            active_tool_result=(
                ToolResult.from_dict(active_tool_result)
                if active_tool_result is not None
                else None
            ),
            active_call_id=(
                str(raw["active_call_id"])
                if raw.get("active_call_id") is not None
                else None
            ),
            active_call_kind=(
                str(raw["active_call_kind"])
                if raw.get("active_call_kind") is not None
                else None
            ),
            resume_target_state=(
                RuntimeState(str(resume_target)) if resume_target is not None else None
            ),
            retry_metadata=dict(retry_metadata),
            interrupt_requested_at=(
                str(raw["interrupt_requested_at"])
                if raw.get("interrupt_requested_at") is not None
                else None
            ),
            context_version=str(raw.get("context_version", "1")),
            final_answer=(
                str(raw["final_answer"])
                if raw.get("final_answer") is not None
                else None
            ),
            failure=dict(failure) if failure is not None else None,
            step_count=int(raw.get("step_count", 0)),
            model_calls=int(raw.get("model_calls", 0)),
            tool_calls=int(raw.get("tool_calls", 0)),
            created_at=str(raw["created_at"]),
            updated_at=str(raw["updated_at"]),
            version=int(raw.get("version", 0)),
            snapshot_version=version,
        )

    @classmethod
    def from_json(cls, raw: str) -> "RuntimeSnapshot":
        decoded = json.loads(raw)
        if not isinstance(decoded, Mapping):
            raise ValueError("runtime snapshot JSON must contain an object")
        return cls.from_dict(decoded)


@dataclass
class Session:
    id: str
    task: str
    source_path: str
    state: RuntimeState
    policy: RunPolicy
    workspace_path: str | None = None
    messages: list[Message] = field(default_factory=list)
    pending_tool_calls: list[ToolCall] = field(default_factory=list)
    active_tool_result: ToolResult | None = None
    active_call_id: str | None = None
    active_call_kind: str | None = None
    resume_target_state: RuntimeState | None = None
    retry_metadata: JsonObject = field(default_factory=dict)
    interrupt_requested_at: str | None = None
    context_version: str = "1"
    final_answer: str | None = None
    failure: JsonObject | None = None
    step_count: int = 0
    model_calls: int = 0
    tool_calls: int = 0
    source_fingerprint: str = ""
    created_at: str = field(default_factory=utc_now)
    updated_at: str = field(default_factory=utc_now)
    version: int = 0

    def to_snapshot(
        self,
        *,
        state: RuntimeState | None = None,
        version: int | None = None,
        updated_at: str | None = None,
    ) -> RuntimeSnapshot:
        return RuntimeSnapshot.from_session(
            self,
            state=state,
            version=version,
            updated_at=updated_at,
        )

    @classmethod
    def from_snapshot(
        cls,
        snapshot: RuntimeSnapshot,
        messages: Sequence[Message] = (),
    ) -> "Session":
        return cls(
            id=snapshot.session_id,
            task=snapshot.task,
            source_path=snapshot.source_path,
            state=snapshot.state,
            policy=snapshot.policy,
            workspace_path=snapshot.workspace_path,
            messages=list(messages),
            pending_tool_calls=list(snapshot.pending_tool_calls),
            active_tool_result=snapshot.active_tool_result,
            active_call_id=snapshot.active_call_id,
            active_call_kind=snapshot.active_call_kind,
            resume_target_state=snapshot.resume_target_state,
            retry_metadata=dict(snapshot.retry_metadata),
            interrupt_requested_at=snapshot.interrupt_requested_at,
            context_version=snapshot.context_version,
            final_answer=snapshot.final_answer,
            failure=dict(snapshot.failure) if snapshot.failure is not None else None,
            step_count=snapshot.step_count,
            model_calls=snapshot.model_calls,
            tool_calls=snapshot.tool_calls,
            source_fingerprint=snapshot.source_fingerprint,
            created_at=snapshot.created_at,
            updated_at=snapshot.updated_at,
            version=snapshot.version,
        )


def apply_snapshot(session: Session, snapshot: RuntimeSnapshot) -> None:
    """Apply committed non-message state to an in-memory Session."""

    if session.id != snapshot.session_id:
        raise ValueError("snapshot session id does not match session")
    session.task = snapshot.task
    session.source_path = snapshot.source_path
    session.state = snapshot.state
    session.policy = snapshot.policy
    session.workspace_path = snapshot.workspace_path
    session.pending_tool_calls = list(snapshot.pending_tool_calls)
    session.active_tool_result = snapshot.active_tool_result
    session.active_call_id = snapshot.active_call_id
    session.active_call_kind = snapshot.active_call_kind
    session.resume_target_state = snapshot.resume_target_state
    session.retry_metadata = dict(snapshot.retry_metadata)
    session.interrupt_requested_at = snapshot.interrupt_requested_at
    session.context_version = snapshot.context_version
    session.final_answer = snapshot.final_answer
    session.failure = dict(snapshot.failure) if snapshot.failure is not None else None
    session.step_count = snapshot.step_count
    session.model_calls = snapshot.model_calls
    session.tool_calls = snapshot.tool_calls
    session.source_fingerprint = snapshot.source_fingerprint
    session.created_at = snapshot.created_at
    session.updated_at = snapshot.updated_at
    session.version = snapshot.version


def snapshot_from_session(
    session: Session,
    *,
    state: RuntimeState | None = None,
    version: int | None = None,
    updated_at: str | None = None,
) -> RuntimeSnapshot:
    return RuntimeSnapshot.from_session(
        session,
        state=state,
        version=version,
        updated_at=updated_at,
    )


@dataclass(frozen=True)
class RunResult:
    session_id: str
    state: RuntimeState
    final_answer: str | None
    failure: JsonObject | None
    workspace_path: str | None
    trace_path: str
    step_count: int
    model_calls: int
    tool_calls: int


class AgentError(Exception):
    """Base class for classified agent failures."""


class ContextBudgetError(AgentError):
    """A context request cannot satisfy its configured budget contract."""

    def __init__(
        self,
        message: str,
        *,
        kind: str = "context_budget_error",
        details: JsonObject | None = None,
    ):
        super().__init__(message)
        self.kind = kind
        self.details = dict(details or {})


class InvariantViolation(AgentError):
    pass


class SchemaValidationError(AgentError):
    pass


class WorkspaceViolation(AgentError):
    pass


class ToolDeadlineExceeded(AgentError):
    pass


class ToolExecutionFailure(AgentError):
    def __init__(self, message: str, *, kind: str = "execution_error", data: JsonObject | None = None):
        super().__init__(redact_sensitive_text(message))
        self.kind = kind
        self.data = data or {}


RETRYABLE_BACKEND_ERROR_KINDS = frozenset(
    {"timeout", "rate_limit", "provider_unavailable"}
)


def redact_sensitive_text(value: str) -> str:
    """Remove common credential forms before an error can reach a trace."""

    import re

    redacted = re.sub(
        r"(?i)(authorization\s*[:=]?\s*bearer\s+)[^\s,]+",
        r"\1[REDACTED]",
        value,
    )
    redacted = re.sub(r"(?i)\b(sk-[A-Za-z0-9_-]+|xox[baprs]-[A-Za-z0-9-]+)\b", "[REDACTED]", redacted)
    return redacted


class BackendError(AgentError):
    def __init__(
        self,
        message: str,
        *,
        kind: str = "provider_error",
        retry_after: float | None = None,
        provider_metadata: JsonObject | None = None,
    ):
        super().__init__(redact_sensitive_text(message))
        self.kind = kind
        self.retry_after = retry_after
        self.provider_metadata = dict(provider_metadata or {})

    @property
    def retryable(self) -> bool:
        return self.kind in RETRYABLE_BACKEND_ERROR_KINDS
