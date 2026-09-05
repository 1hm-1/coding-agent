from __future__ import annotations

import json
import os
import threading
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Protocol

from coding_agent.domain import (
    ALLOWED_TRANSITIONS,
    Event,
    EventType,
    InvariantViolation,
    JsonObject,
    Message,
    RuntimeSnapshot,
    RuntimeState,
    utc_now,
)
from coding_agent.persistence import JournalMutation, RunJournal
from coding_agent.persistence import ModelCallMutation, SummaryMutation, ToolCallMutation


class EventStore(Protocol):
    def append(self, event: Event) -> None:
        ...

    def load(self, session_id: str) -> list[Event]:
        ...

    def trace_path(self, session_id: str) -> Path:
        ...


class JsonlEventStore:
    """Durable-enough M1 sink; SQLite checkpoints replace it as authority later."""

    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def trace_path(self, session_id: str) -> Path:
        return self.root / f"{session_id}.jsonl"

    def append(self, event: Event) -> None:
        line = json.dumps(event.to_dict(), ensure_ascii=False, sort_keys=True)
        path = self.trace_path(event.session_id)
        with self._lock:
            existing = self.load(event.session_id)
            expected = len(existing) + 1
            if event.sequence != expected:
                raise InvariantViolation(
                    f"event sequence must be {expected}, got {event.sequence}"
                )
            with path.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")
                handle.flush()
                os.fsync(handle.fileno())

    def load(self, session_id: str) -> list[Event]:
        path = self.trace_path(session_id)
        if not path.exists():
            return []
        events: list[Event] = []
        with path.open("r", encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, start=1):
                if not line.strip():
                    continue
                try:
                    events.append(Event.from_dict(json.loads(line)))
                except (ValueError, TypeError, KeyError) as exc:
                    raise InvariantViolation(
                        f"invalid trace line {line_number} in {path}"
                    ) from exc
        return events


class TrajectoryRecorder:
    def __init__(
        self,
        store: EventStore,
        session_id: str,
        *,
        journal: RunJournal | None = None,
        snapshot_provider: Callable[[], RuntimeSnapshot] | None = None,
        lease_owner: str | None = None,
    ):
        self.store = store
        self.session_id = session_id
        if journal is None and all(
            hasattr(store, attribute)
            for attribute in ("commit", "create_session", "session_version")
        ):
            journal = store  # type: ignore[assignment]
        self.journal = journal
        self.snapshot_provider = snapshot_provider
        self.lease_owner = lease_owner
        self._version = 0
        self._state: RuntimeState | None = None
        if journal is not None:
            try:
                self._state = journal.load_snapshot(session_id).state
                self._sequence = journal.last_event_sequence(session_id)  # type: ignore[attr-defined]
                self._version = journal.session_version(session_id)  # type: ignore[attr-defined]
            except LookupError:
                self._sequence = 0
        else:
            existing = store.load(session_id)
            self._sequence = existing[-1].sequence if existing else 0

    @property
    def path(self) -> Path:
        return self.store.trace_path(self.session_id)

    @property
    def current_version(self) -> int:
        return self._version

    def create_session(
        self,
        snapshot: RuntimeSnapshot,
        initial_message: Message,
        *,
        session_created_payload: JsonObject | None = None,
    ) -> tuple[Event, ...]:
        if self.journal is None:
            raise InvariantViolation("create_session requires a journal")
        events = self.journal.create_session(
            snapshot,
            initial_message,
            session_created_payload=session_created_payload,
        )
        self._sequence = events[-1].sequence if events else 0
        self._version = snapshot.version
        self._state = snapshot.state
        return events

    def emit(
        self,
        event_type: EventType,
        state: RuntimeState,
        payload: JsonObject | None = None,
        *,
        snapshot_after: RuntimeSnapshot | None = None,
        expected_state: RuntimeState | None = None,
        message_to_append: Message | None = None,
        model_call: ModelCallMutation | None = None,
        tool_call: ToolCallMutation | None = None,
        summary: SummaryMutation | None = None,
        clear_interrupt: bool = False,
    ) -> Event:
        if self.journal is not None:
            if snapshot_after is None:
                if self.snapshot_provider is None:
                    raise InvariantViolation(
                        "a SQLite journal event requires a snapshot after mutation"
                    )
                snapshot_after = self.snapshot_provider()
            if snapshot_after.state is not state:
                raise InvariantViolation(
                    "event state must match the snapshot state committed with it"
                )
            expected = expected_state or self._state or snapshot_after.state
            result = self.journal.commit(
                JournalMutation(
                    session_id=self.session_id,
                    expected_version=self._version,
                    expected_state=expected,
                    snapshot_after=snapshot_after,
                    event_type=event_type,
                    payload=payload or {},
                    message_to_append=message_to_append,
                    model_call=model_call,
                    tool_call=tool_call,
                    summary=summary,
                    clear_interrupt=clear_interrupt,
                    lease_owner=self.lease_owner,
                )
            )
            self._sequence = result.event.sequence
            self._version = result.committed_version
            self._state = snapshot_after.state
            return result.event

        self._sequence += 1
        event = Event(
            schema_version=1,
            event_id=str(uuid.uuid4()),
            session_id=self.session_id,
            sequence=self._sequence,
            event_type=event_type,
            timestamp=utc_now(),
            state=state,
            payload=payload or {},
        )
        try:
            self.store.append(event)
        except Exception:
            self._sequence -= 1
            raise
        return event


@dataclass(frozen=True)
class ReplayResult:
    session_id: str
    final_state: RuntimeState
    event_count: int
    model_calls: int
    tool_calls: int
    tool_failures: int
    input_tokens: int
    output_tokens: int
    test_runs: int
    final_test_passed: bool | None
    source_unchanged: bool | None
    failure_kind: str | None
    transition_path: tuple[str, ...]
    tool_order: tuple[str, ...]
    tool_status_counts: dict[str, int]
    test_outcomes: tuple[bool | None, ...]

    def semantic_projection(self) -> JsonObject:
        """Return a deterministic behavior contract suitable for golden tests."""

        return {
            "schema_version": 1,
            "final_state": self.final_state.value,
            "transition_path": list(self.transition_path),
            "tool_order": list(self.tool_order),
            "tool_status_counts": dict(sorted(self.tool_status_counts.items())),
            "test_outcomes": list(self.test_outcomes),
            "metrics": {
                "model_calls": self.model_calls,
                "tool_calls": self.tool_calls,
                "tool_failures": self.tool_failures,
                "input_tokens": self.input_tokens,
                "output_tokens": self.output_tokens,
                "test_runs": self.test_runs,
            },
            "constraints": {
                "source_unchanged": self.source_unchanged,
                "final_test_passed": self.final_test_passed,
            },
            "failure_kind": self.failure_kind,
        }


def replay(events: list[Event]) -> ReplayResult:
    if not events:
        raise InvariantViolation("cannot replay an empty trajectory")
    session_id = events[0].session_id
    current_state = RuntimeState.CREATED
    model_calls = 0
    tool_calls = 0
    tool_failures = 0
    input_tokens = 0
    output_tokens = 0
    test_runs = 0
    final_test_passed: bool | None = None
    source_unchanged: bool | None = None
    failure_kind: str | None = None
    transition_path: list[str] = []
    tool_order: list[str] = []
    tool_status_counts: dict[str, int] = {}
    test_outcomes: list[bool | None] = []
    run_finished_payload: JsonObject | None = None

    for expected_sequence, event in enumerate(events, start=1):
        if event.session_id != session_id:
            raise InvariantViolation("trajectory contains multiple session ids")
        if event.sequence != expected_sequence:
            raise InvariantViolation(
                f"trajectory sequence gap: expected {expected_sequence}, got {event.sequence}"
            )
        if event.event_type is EventType.STATE_TRANSITION:
            from_state = RuntimeState(str(event.payload["from"]))
            to_state = RuntimeState(str(event.payload["to"]))
            if from_state is not current_state:
                raise InvariantViolation(
                    f"transition starts at {from_state.value}, expected {current_state.value}"
                )
            if to_state not in ALLOWED_TRANSITIONS[from_state]:
                raise InvariantViolation(
                    f"illegal replay transition {from_state.value} -> {to_state.value}"
                )
            if event.state is not to_state:
                raise InvariantViolation("transition event state does not match target state")
            current_state = to_state
            transition_path.append(f"{from_state.value}->{to_state.value}")
        elif event.state is not current_state:
            raise InvariantViolation(
                f"event {event.event_type.value} recorded in unexpected state {event.state.value}"
            )

        if event.event_type is EventType.MODEL_CALL_STARTED:
            model_calls += 1
        elif event.event_type is EventType.MODEL_CALL_SUCCEEDED:
            usage = event.payload.get("usage", {})
            if not isinstance(usage, dict):
                raise InvariantViolation("model usage must be an object")
            input_tokens += int(usage.get("input_tokens", 0))
            output_tokens += int(usage.get("output_tokens", 0))
        elif event.event_type is EventType.TOOL_CALL_PREPARED:
            tool_calls += 1
            call = event.payload.get("call", {})
            if not isinstance(call, dict) or "name" not in call:
                raise InvariantViolation("tool start event is missing the tool name")
            tool_order.append(str(call["name"]))
        elif event.event_type is EventType.TOOL_CALL_STARTED:
            # M1 traces used TOOL_CALL_STARTED as the only tool-intent event.
            tool_calls += 1
            call = event.payload.get("call", {})
            if not isinstance(call, dict) or "name" not in call:
                raise InvariantViolation("tool start event is missing the tool name")
            tool_order.append(str(call["name"]))
        elif event.event_type is EventType.TOOL_CALL_FINISHED:
            result = event.payload.get("result", {})
            if not isinstance(result, dict):
                raise InvariantViolation("tool result must be an object")
            status = str(result.get("status", "missing"))
            tool_status_counts[status] = tool_status_counts.get(status, 0) + 1
            if status != "success":
                tool_failures += 1
            if result.get("tool_name") == "restricted_test":
                data = result.get("data", {})
                if not isinstance(data, dict):
                    raise InvariantViolation("restricted test data must be an object")
                test_runs += 1
                passed = data.get("passed")
                final_test_passed = bool(passed) if isinstance(passed, bool) else None
                test_outcomes.append(final_test_passed)
        elif event.event_type is EventType.RUN_FINISHED:
            if run_finished_payload is not None:
                raise InvariantViolation("trajectory has more than one run_finished event")
            run_finished_payload = event.payload

    if run_finished_payload is not None:
        declared_state = RuntimeState(str(run_finished_payload["final_state"]))
        if declared_state is not current_state:
            raise InvariantViolation("run_finished final state disagrees with replay")
        if int(run_finished_payload.get("model_calls", -1)) != model_calls:
            raise InvariantViolation("run_finished model call count disagrees with replay")
        if int(run_finished_payload.get("tool_calls", -1)) != tool_calls:
            raise InvariantViolation("run_finished tool call count disagrees with replay")
        unchanged = run_finished_payload.get("source_unchanged")
        source_unchanged = bool(unchanged) if isinstance(unchanged, bool) else None
        failure = run_finished_payload.get("failure")
        if isinstance(failure, dict) and failure.get("kind") is not None:
            failure_kind = str(failure["kind"])

    return ReplayResult(
        session_id=session_id,
        final_state=current_state,
        event_count=len(events),
        model_calls=model_calls,
        tool_calls=tool_calls,
        tool_failures=tool_failures,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        test_runs=test_runs,
        final_test_passed=final_test_passed,
        source_unchanged=source_unchanged,
        failure_kind=failure_kind,
        transition_path=tuple(transition_path),
        tool_order=tuple(tool_order),
        tool_status_counts=tool_status_counts,
        test_outcomes=tuple(test_outcomes),
    )


def semantic_projection(events: list[Event]) -> JsonObject:
    return replay(events).semantic_projection()
