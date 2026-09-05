from __future__ import annotations

from dataclasses import dataclass, replace
import hashlib
import json
from typing import Any, Mapping, Sequence

from coding_agent.domain import (
    ContextBuildInput,
    ErrorFact,
    Event,
    EventType,
    FileFact,
    Message,
    ModelRequest,
    ModelResponse,
    RepositorySnapshot,
    SummaryRecord,
    TestFact,
    utc_now,
)
from coding_agent.models.base import ModelBackend


class SummaryValidationError(ValueError):
    """A summarizer response cannot be used as a derived context cache."""

    def __init__(self, message: str, *, kind: str = "compression_rejected"):
        super().__init__(message)
        self.kind = kind


@dataclass(frozen=True)
class EventRange:
    start: int
    end: int
    source_event_hash: str


@dataclass(frozen=True)
class CompressionResult:
    summary: SummaryRecord
    request: ModelRequest
    response: ModelResponse
    event_range: EventRange


def event_range_hash(events: Sequence[Event], start: int, end: int) -> str:
    if start <= 0 or end < start:
        raise ValueError("event range must be positive and ordered")
    selected = [event for event in events if start <= event.sequence <= end]
    if len(selected) != end - start + 1:
        raise ValueError("event range is not contiguous")
    digest = hashlib.sha256()
    for event in selected:
        encoded = json.dumps(
            event.to_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        digest.update(encoded)
        digest.update(b"\n")
    return digest.hexdigest()


def select_event_range(
    events: Sequence[Event],
    *,
    after_sequence: int = 2,
) -> EventRange | None:
    """Select the oldest committed trajectory range eligible for compression."""

    eligible = [
        event
        for event in events
        if event.sequence > after_sequence
        and event.event_type not in {EventType.RUN_FINISHED, EventType.COMPRESSION_STARTED}
    ]
    if not eligible:
        return None
    start = eligible[0].sequence
    end = eligible[-1].sequence
    selected = [event for event in events if start <= event.sequence <= end]
    # A source range must contain every committed sequence. If a caller supplied
    # a sparse event list, do not create a lineage record that cannot be checked.
    if len(selected) != end - start + 1:
        return None
    return EventRange(start, end, event_range_hash(events, start, end))


def stale_summary(
    summary: SummaryRecord,
    snapshot: RepositorySnapshot,
) -> SummaryRecord:
    """Mark a derived summary stale when its workspace facts no longer match."""

    current_hashes = {fact.path: fact.content_hash for fact in snapshot.read_files}
    changed_facts: list[FileFact] = []
    any_stale = summary.stale or summary.workspace_revision != snapshot.workspace_revision
    for fact in summary.files_read:
        current = current_hashes.get(fact.path)
        is_stale = fact.stale or current is None or current != fact.content_hash
        any_stale = any_stale or is_stale
        changed_facts.append(replace(fact, stale=is_stale))
    changed_edits = []
    for fact in summary.edits:
        current = current_hashes.get(fact.path)
        is_stale = fact.stale or (
            current is not None and fact.content_hash and current != fact.content_hash
        )
        any_stale = any_stale or is_stale
        changed_edits.append(replace(fact, stale=is_stale))
    return replace(
        summary,
        files_read=tuple(changed_facts),
        edits=tuple(changed_edits),
        stale=any_stale,
    )


def _json_payload(messages: Sequence[Message], events: Sequence[Event]) -> str:
    payload = {
        "events": [event.to_dict() for event in events],
        "messages": [message.to_dict() for message in messages],
    }
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


class RequiredFactVerifier:
    """Check facts that must survive compression before a summary is committed."""

    def verify(
        self,
        summary: SummaryRecord,
        *,
        task: str,
        messages: Sequence[Message],
        events: Sequence[Event],
        repository_snapshot: RepositorySnapshot,
    ) -> None:
        if not summary.goals and not summary.constraints:
            raise SummaryValidationError(
                "summary omitted task/constraint facts",
                kind="compression_required_fact_missing",
            )
        task_markers = " ".join((*summary.goals, *summary.constraints))
        if task.strip() and not _contains_task_fact(task, task_markers):
            raise SummaryValidationError(
                "summary omitted task/constraint facts",
                kind="compression_required_fact_missing",
            )

        edited_paths = _edited_paths(events)
        summary_edit_paths = {fact.path for fact in summary.edits}
        missing_edits = sorted(edited_paths - summary_edit_paths)
        if missing_edits:
            raise SummaryValidationError(
                f"summary omitted edited files: {', '.join(missing_edits)}",
                kind="compression_required_fact_missing",
            )

        latest_test = repository_snapshot.last_test or _latest_test(messages)
        if latest_test is not None:
            if not summary.tests:
                raise SummaryValidationError(
                    "summary omitted the latest test result",
                    kind="compression_required_fact_missing",
                )
            if summary.tests[-1].passed != latest_test.passed:
                raise SummaryValidationError(
                    "summary latest test result disagrees with the source observation",
                    kind="compression_required_fact_conflict",
                )

        active_failure = _active_failure(events)
        if active_failure is not None:
            represented = {fact.kind for fact in summary.errors}
            if active_failure.kind not in represented and not summary.unresolved:
                raise SummaryValidationError(
                    "summary omitted the active failure",
                    kind="compression_required_fact_missing",
                )

        if not isinstance(summary.unresolved, tuple):
            raise SummaryValidationError(
                "summary unresolved facts must be an array",
                kind="compression_schema_invalid",
            )


def _contains_task_fact(task: str, markers: str) -> bool:
    """Allow paraphrase while requiring overlap on a meaningful task term."""

    stopwords = {
        "a",
        "an",
        "and",
        "for",
        "in",
        "of",
        "on",
        "or",
        "the",
        "to",
    }
    task_terms = {
        token
        for token in _fact_tokens(task)
        if token not in stopwords
    }
    marker_terms = set(_fact_tokens(markers))
    return bool(task_terms & marker_terms)


def _fact_tokens(value: str) -> tuple[str, ...]:
    current: list[str] = []
    tokens: list[str] = []
    for character in value.lower():
        if character.isalnum() or character == "_":
            current.append(character)
        elif current:
            tokens.append("".join(current))
            current = []
    if current:
        tokens.append("".join(current))
    return tuple(tokens)


def _edited_paths(events: Sequence[Event]) -> set[str]:
    paths: set[str] = set()
    for event in events:
        if event.event_type not in {
            EventType.TOOL_CALL_PREPARED,
            EventType.TOOL_CALL_STARTED,
            EventType.TOOL_CALL_RUNNING,
        }:
            continue
        call = event.payload.get("call")
        if not isinstance(call, Mapping) or call.get("name") != "edit_file":
            continue
        arguments = call.get("arguments")
        if isinstance(arguments, Mapping) and arguments.get("path") is not None:
            paths.add(str(arguments["path"]))
    return paths


def _latest_test(messages: Sequence[Message]) -> TestFact | None:
    for message in reversed(messages):
        if message.role != "tool":
            continue
        try:
            raw = json.loads(message.content)
        except (TypeError, ValueError):
            continue
        if not isinstance(raw, Mapping) or raw.get("tool_name") != "restricted_test":
            continue
        data = raw.get("data")
        if not isinstance(data, Mapping):
            continue
        passed = data.get("passed")
        if passed is not None and not isinstance(passed, bool):
            continue
        return TestFact(
            profile=str(data.get("profile", "unknown")),
            passed=passed,
            summary=str(data.get("stdout", ""))[:1000],
            stderr_summary=str(data.get("stderr", ""))[:1000],
        )
    return None


def _active_failure(events: Sequence[Event]) -> ErrorFact | None:
    resolved_model_calls: set[str] = set()
    resolved_tool_calls: set[str] = set()
    for event in reversed(events):
        if event.event_type is EventType.MODEL_CALL_SUCCEEDED:
            request_id = event.payload.get("request_id")
            if request_id is not None:
                resolved_model_calls.add(str(request_id))
            continue
        if event.event_type is EventType.MODEL_CALL_FAILED:
            request_id = str(event.payload.get("request_id", ""))
            if request_id and request_id in resolved_model_calls:
                continue
            return ErrorFact(
                kind=str(event.payload.get("kind", "provider_error")),
                message=str(event.payload.get("message", "")),
                source_event_sequence=event.sequence,
            )
        if event.event_type is EventType.TOOL_CALL_FINISHED:
            result = event.payload.get("result")
            if not isinstance(result, Mapping):
                continue
            call_id = result.get("call_id")
            call_id_text = str(call_id) if call_id is not None else ""
            if str(result.get("status")) == "success":
                if call_id_text:
                    resolved_tool_calls.add(call_id_text)
                continue
            if call_id_text and call_id_text in resolved_tool_calls:
                continue
            error = result.get("error")
            kind = error.get("kind") if isinstance(error, Mapping) else "tool_failure"
            message = error.get("message") if isinstance(error, Mapping) else ""
            return ErrorFact(
                kind=str(kind),
                message=str(message),
                source_event_sequence=event.sequence,
            )
    return None


class CompressionEngine:
    """Bounded summary-model invocation and schema/fact validation."""

    SUMMARY_SYSTEM_PROMPT = (
        "Return only a JSON object with schema_version, goals, constraints, decisions, "
        "files_read, edits, tests, errors, and unresolved. Do not invent facts."
    )

    def __init__(
        self,
        summarizer: ModelBackend,
        *,
        max_output_tokens: int = 2048,
        verifier: RequiredFactVerifier | None = None,
    ):
        self.summarizer = summarizer
        if max_output_tokens <= 0:
            raise ValueError("compression max_output_tokens must be positive")
        self.max_output_tokens = max_output_tokens
        self.verifier = verifier or RequiredFactVerifier()

    def compress(
        self,
        request: ContextBuildInput,
        *,
        events: Sequence[Event],
        event_range: EventRange,
    ) -> CompressionResult:
        selected_events = tuple(
            event
            for event in events
            if event_range.start <= event.sequence <= event_range.end
        )
        if len(selected_events) != event_range.end - event_range.start + 1:
            raise SummaryValidationError("compression source event range is not contiguous")
        compression_request = ModelRequest(
            request_id=(
                f"{request.session_id}:compression:{event_range.start}:{event_range.end}"
            ),
            messages=(
                Message(role="system", content=self.SUMMARY_SYSTEM_PROMPT),
                Message(
                    role="user",
                    content=_json_payload(request.messages, selected_events),
                ),
            ),
            tools=(),
            max_output_tokens=self.max_output_tokens,
            metadata={
                "session_id": request.session_id,
                "call_kind": "compression",
                "source_event_start": event_range.start,
                "source_event_end": event_range.end,
            },
        )
        response = self.summarizer.complete(compression_request)
        if response.tool_calls:
            raise SummaryValidationError(
                "summarizer response cannot contain tool calls",
                kind="compression_schema_invalid",
            )
        summary = self._decode_summary(
            response,
            request=request,
            event_range=event_range,
            source_message_end=len(request.messages) - 1,
        )
        self.verifier.verify(
            summary,
            task=request.task,
            messages=request.messages,
            events=selected_events,
            repository_snapshot=request.repository_snapshot,
        )
        return CompressionResult(
            summary=summary,
            request=compression_request,
            response=response,
            event_range=event_range,
        )

    @staticmethod
    def _decode_summary(
        response: ModelResponse,
        *,
        request: ContextBuildInput,
        event_range: EventRange,
        source_message_end: int,
    ) -> SummaryRecord:
        try:
            decoded = json.loads(response.text)
        except (TypeError, ValueError) as exc:
            raise SummaryValidationError(
                "summarizer response is not valid JSON",
                kind="compression_schema_invalid",
            ) from exc
        if isinstance(decoded, Mapping) and isinstance(decoded.get("summary"), Mapping):
            decoded = decoded["summary"]
        if not isinstance(decoded, Mapping):
            raise SummaryValidationError(
                "summarizer response must be a JSON object",
                kind="compression_schema_invalid",
            )
        allowed = {
            "schema_version",
            "goals",
            "constraints",
            "decisions",
            "files_read",
            "edits",
            "tests",
            "errors",
            "unresolved",
        }
        unknown = sorted(set(decoded) - allowed)
        if unknown:
            raise SummaryValidationError(
                f"summarizer response has unknown fields: {', '.join(unknown)}",
                kind="compression_schema_invalid",
            )
        missing = sorted(allowed - set(decoded))
        if missing:
            raise SummaryValidationError(
                f"summarizer response is missing fields: {', '.join(missing)}",
                kind="compression_schema_invalid",
            )
        schema_version = decoded.get("schema_version")
        if isinstance(schema_version, bool) or not isinstance(schema_version, int):
            raise SummaryValidationError(
                "summary schema_version must be an integer",
                kind="compression_schema_invalid",
            )
        for name in ("goals", "constraints", "decisions", "unresolved"):
            values = decoded[name]
            if not isinstance(values, list) or any(not isinstance(value, str) for value in values):
                raise SummaryValidationError(
                    f"summary {name} must be an array of strings",
                    kind="compression_schema_invalid",
                )
        for name in ("files_read", "edits", "tests", "errors"):
            values = decoded[name]
            if not isinstance(values, list) or any(not isinstance(value, Mapping) for value in values):
                raise SummaryValidationError(
                    f"summary {name} must be an array of objects",
                    kind="compression_schema_invalid",
                )
        payload: dict[str, Any] = {
            "summary_id": (
                f"summary-{request.session_id}-{event_range.end}-"
                f"{event_range.source_event_hash[:12]}"
            ),
            "schema_version": schema_version,
            "session_id": request.session_id,
            "source_event_start": event_range.start,
            "source_event_end": event_range.end,
            "source_event_hash": event_range.source_event_hash,
            "workspace_revision": request.repository_snapshot.workspace_revision,
            "source_message_end": source_message_end,
            "created_at": utc_now(),
        }
        payload.update({key: decoded[key] for key in allowed if key in decoded})
        if payload["schema_version"] != 1:
            raise SummaryValidationError(
                "unsupported summary schema version",
                kind="compression_schema_invalid",
            )
        try:
            return SummaryRecord.from_dict(payload)
        except (KeyError, TypeError, ValueError) as exc:
            raise SummaryValidationError(
                f"invalid summary schema: {exc}",
                kind="compression_schema_invalid",
            ) from exc


def summary_from_json(raw: str) -> SummaryRecord:
    decoded = json.loads(raw)
    if not isinstance(decoded, Mapping):
        raise SummaryValidationError("summary JSON must contain an object")
    return SummaryRecord.from_dict(decoded)
