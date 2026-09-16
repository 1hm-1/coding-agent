from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
from importlib.metadata import PackageNotFoundError, version
import json
import os
from pathlib import Path
import re
import signal
import stat
import sys
import threading
import time
from typing import Any, Callable, Iterator, Mapping, TextIO
import uuid

from coding_agent.application import AgentApplication
from coding_agent.domain import (
    Event,
    EventType,
    Permission,
    RunPolicy,
    RunResult,
    RuntimeState,
    redact_sensitive_text,
    utc_now,
)
from coding_agent.models.anthropic import AnthropicBackend
from coding_agent.models.base import ModelBackend
from coding_agent.models.openai_compatible import OpenAICompatibleBackend
from coding_agent.models.scripted import ScriptedBackend
from coding_agent.protocol.schema import (
    DocumentValidationError,
    SCHEMA_FILES,
    schema_root,
    validate_document,
    validate_schema_bundle,
)
from coding_agent.workspace import tree_fingerprint


PROTOCOL_VERSION = 1
RUNTIME_NAME = "coding-agent"
RUNTIME_VERSION = "0.2.0.dev0"
MAX_REQUEST_BYTES = 1_048_576
MAX_RECORD_BYTES = 262_144

EXIT_INVALID_REQUEST = 64
EXIT_UNSUPPORTED_PROTOCOL = 65
EXIT_SOFTWARE = 70
EXIT_PRIVATE_IO = 74

ENABLED_CAPABILITIES = frozenset(
    {
        "structured_events",
        "cooperative_interrupt",
        "checkpoint_resume",
        "scripted_backend",
        "real_model_backend",
    }
)
BACKEND_KINDS = frozenset({"scripted", "openai-compatible", "anthropic"})


class ProtocolError(ValueError):
    """A stable, safe-to-report Runtime IPC process failure."""

    def __init__(self, message: str, *, exit_code: int):
        super().__init__(redact_sensitive_text(message))
        self.exit_code = exit_code


class ProtocolOutputError(ProtocolError):
    def __init__(self, message: str):
        super().__init__(message, exit_code=EXIT_SOFTWARE)


@dataclass(frozen=True)
class ValidatedExecutionRequest:
    document: dict[str, Any]
    request_file: Path
    attempt_root: Path
    source: Path
    agent_home: Path
    script_path: Path | None


class ProtocolWriter:
    """Strict one-record-per-line writer with one terminal result."""

    def __init__(self, output: TextIO | None = None, *, max_record_bytes: int = MAX_RECORD_BYTES):
        self.output = output or sys.stdout
        self.max_record_bytes = max_record_bytes
        self.sequence = 0
        self.result_written = False

    def write_event(self, document: Mapping[str, Any]) -> None:
        if self.result_written:
            raise ProtocolOutputError("cannot write an event after the terminal result")
        self._write(document, "event_envelope")

    def write_result(self, document: Mapping[str, Any]) -> None:
        if self.result_written:
            raise ProtocolOutputError("terminal result was already written")
        self._write(document, "terminal_result")
        self.result_written = True

    def next_sequence(self) -> int:
        return self.sequence + 1

    def _write(self, document: Mapping[str, Any], schema_name: str) -> None:
        candidate = dict(document)
        expected = self.sequence + 1
        if candidate.get("sequence") != expected:
            raise ProtocolOutputError(f"protocol sequence must be {expected}")
        try:
            validate_document(candidate, schema_name)
            serialized = json.dumps(
                candidate,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
        except (DocumentValidationError, TypeError, ValueError) as exc:
            raise ProtocolOutputError(f"protocol record serialization failed: {exc}") from exc
        if len(serialized.encode("utf-8")) > self.max_record_bytes:
            raise ProtocolOutputError("protocol record exceeds the record limit")
        try:
            self.output.write(serialized + "\n")
            self.output.flush()
        except (BrokenPipeError, OSError, UnicodeError) as exc:
            raise ProtocolOutputError("protocol stdout write failed") from exc
        self.sequence = expected


class CancellationController:
    def __init__(self, event: threading.Event | None = None):
        self.event = event or threading.Event()
        self._lock = threading.Lock()
        self._cause: str | None = None

    @property
    def cause(self) -> str | None:
        with self._lock:
            return self._cause

    def request(self, cause: str = "cancel") -> None:
        with self._lock:
            if self._cause is None:
                self._cause = cause
            self.event.set()

    def requested(self) -> bool:
        return self.event.is_set()


def protocol_info(protocol_version: int = PROTOCOL_VERSION) -> dict[str, object]:
    """Return the side-effect-free Runtime IPC capability document."""

    _require_protocol_version(protocol_version)
    try:
        validate_schema_bundle()
        document: dict[str, object] = {
            "protocol_version": PROTOCOL_VERSION,
            "kind": "protocol_info",
            "runtime_name": RUNTIME_NAME,
            "runtime_version": _runtime_version(),
            "supported_protocol_versions": [PROTOCOL_VERSION],
            "capabilities": sorted(ENABLED_CAPABILITIES),
            "backend_kinds": sorted(BACKEND_KINDS),
            "sandbox_modes": ["native_linux", "fail_closed"],
            "schema_digests": schema_digests(),
            "limits": {
                "max_request_bytes": MAX_REQUEST_BYTES,
                "max_record_bytes": MAX_RECORD_BYTES,
            },
            "extensions": {},
        }
        validate_document(document, "capabilities")
        return document
    except (OSError, ValueError) as exc:
        if isinstance(exc, ProtocolError):
            raise
        raise ProtocolError(
            "Runtime IPC schema bundle is unavailable or invalid", exit_code=EXIT_SOFTWARE
        ) from exc


def write_protocol_info(protocol_version: int, *, output: TextIO | None = None) -> None:
    stream = output or sys.stdout
    document = protocol_info(protocol_version)
    serialized = json.dumps(
        document, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    )
    if len(serialized.encode("utf-8")) > MAX_RECORD_BYTES:
        raise ProtocolError("protocol info exceeds the record limit", exit_code=EXIT_SOFTWARE)
    try:
        stream.write(serialized + "\n")
        stream.flush()
    except (BrokenPipeError, OSError, UnicodeError) as exc:
        raise ProtocolOutputError("protocol stdout write failed") from exc


def load_execution_request(
    request_file: str | Path,
    *,
    protocol_version: int = PROTOCOL_VERSION,
) -> ValidatedExecutionRequest:
    _require_protocol_version(protocol_version)
    lexical = Path(request_file)
    if not lexical.is_absolute():
        raise ProtocolError("request file must be an absolute private path", exit_code=EXIT_PRIVATE_IO)
    try:
        file_stat = lexical.lstat()
    except OSError as exc:
        raise ProtocolError("private request file is unavailable", exit_code=EXIT_PRIVATE_IO) from exc
    if stat.S_ISLNK(file_stat.st_mode) or not stat.S_ISREG(file_stat.st_mode):
        raise ProtocolError("private request file must be a regular non-symlink", exit_code=EXIT_PRIVATE_IO)
    if file_stat.st_mode & 0o077:
        raise ProtocolError(
            "private request file permissions must be 0600 or stricter", exit_code=EXIT_PRIVATE_IO
        )
    if file_stat.st_size > MAX_REQUEST_BYTES:
        raise ProtocolError("execution request exceeds the request limit", exit_code=EXIT_INVALID_REQUEST)
    if lexical.parent.name != "input":
        raise ProtocolError(
            "request file must be inside the attempt input directory", exit_code=EXIT_PRIVATE_IO
        )
    try:
        resolved = lexical.resolve(strict=True)
        raw_bytes = resolved.read_bytes()
    except OSError as exc:
        raise ProtocolError("private request file cannot be read", exit_code=EXIT_PRIVATE_IO) from exc
    if len(raw_bytes) > MAX_REQUEST_BYTES:
        raise ProtocolError("execution request exceeds the request limit", exit_code=EXIT_INVALID_REQUEST)
    try:
        raw_text = raw_bytes.decode("utf-8")
        decoded = json.loads(raw_text)
    except (UnicodeError, ValueError) as exc:
        raise ProtocolError(
            "execution request is not valid UTF-8 JSON", exit_code=EXIT_INVALID_REQUEST
        ) from exc
    if not isinstance(decoded, dict):
        raise ProtocolError("execution request must be a JSON object", exit_code=EXIT_INVALID_REQUEST)
    request_version = decoded.get("protocol_version")
    if isinstance(request_version, int) and request_version != PROTOCOL_VERSION:
        raise ProtocolError(
            f"unsupported protocol version: {request_version}",
            exit_code=EXIT_UNSUPPORTED_PROTOCOL,
        )
    try:
        validate_document(decoded, "execution_request")
    except (DocumentValidationError, ValueError) as exc:
        raise ProtocolError(f"invalid execution request: {exc}", exit_code=EXIT_INVALID_REQUEST) from exc
    _reject_request_secrets(raw_text, decoded)
    attempt_root = resolved.parent.parent.resolve(strict=True)
    if attempt_root == Path(attempt_root.anchor):
        raise ProtocolError("attempt root is too broad", exit_code=EXIT_PRIVATE_IO)
    return _validate_private_inputs(decoded, resolved, attempt_root)


def run_headless(
    protocol_version: int,
    request_file: str | Path,
    *,
    output: TextIO | None = None,
    cancel_event: threading.Event | None = None,
    before_result: Callable[[], None] | None = None,
    backend_override: ModelBackend | None = None,
    event_hook: Callable[[Event], None] | None = None,
) -> int:
    controller = CancellationController(cancel_event)
    with _headless_signal_handler(controller):
        request = load_execution_request(request_file, protocol_version=protocol_version)
        return _run_validated_headless(
            request,
            output=output,
            controller=controller,
            before_result=before_result,
            backend_override=backend_override,
            event_hook=event_hook,
        )


def _run_validated_headless(
    request: ValidatedExecutionRequest,
    *,
    output: TextIO | None,
    controller: CancellationController,
    before_result: Callable[[], None] | None,
    backend_override: ModelBackend | None,
    event_hook: Callable[[Event], None] | None,
) -> int:
    writer = ProtocolWriter(output)
    application = AgentApplication(request.agent_home)
    session_id = f"runtime-{uuid.uuid4()}"
    output_failure: ProtocolOutputError | None = None
    started = time.monotonic()
    private_items = [
        str(request.attempt_root),
        str(request.source),
        str(request.agent_home),
        str(request.request_file),
    ]
    if request.script_path is not None:
        private_items.append(str(request.script_path))
    private_values = tuple(private_items)
    secret_values = _request_secret_values(request.document)

    def observe(event: Event) -> None:
        nonlocal output_failure
        if output_failure is not None:
            return
        projected = project_event(
            event,
            execution_id=str(request.document["execution_id"]),
            sequence=writer.next_sequence(),
            private_values=private_values,
            secret_values=secret_values,
        )
        if projected is None:
            return
        try:
            writer.write_event(projected)
        except ProtocolOutputError as exc:
            output_failure = exc
        if event_hook is not None:
            event_hook(event)

    timeout_seconds = float(_object(request.document, "runtime")["timeout_seconds"])
    timer = threading.Timer(timeout_seconds, controller.request, args=("timeout",))
    timer.daemon = True
    backend = backend_override or _backend_from_request(request)
    policy = _policy_from_request(request.document)
    timer.start()
    try:
        result = application.run_task(
            source=request.source,
            task=str(request.document["task"]),
            backend=backend,
            policy=policy,
            session_id=session_id,
            event_observer=observe,
            external_interrupt_requested=controller.requested,
            manage_signals=False,
        )
        timer.cancel()
        if output_failure is not None:
            raise output_failure
        terminal = terminal_result(
            request,
            result,
            sequence=writer.next_sequence(),
            events=application.journal.list_events(session_id) if application.journal else [],
            duration_ms=(time.monotonic() - started) * 1000.0,
            cancellation_cause=controller.cause,
            private_values=private_values,
            secret_values=secret_values,
        )
        if before_result is not None:
            before_result()
        writer.write_result(terminal)
        return 0
    finally:
        timer.cancel()
        application.close()


def project_event(
    event: Event,
    *,
    execution_id: str,
    sequence: int,
    private_values: tuple[str, ...] = (),
    secret_values: tuple[str, ...] = (),
) -> dict[str, Any] | None:
    event_name: str | None = None
    payload: dict[str, Any] = {}
    raw = event.payload
    if event.event_type is EventType.SESSION_CREATED:
        event_name = "runtime.started"
        payload = {"backend": _safe_identifier(raw.get("backend"), "unknown")}
    elif event.event_type is EventType.STATE_TRANSITION:
        target = str(raw.get("to", event.state.value))
        if target == RuntimeState.INTERRUPTED.value:
            event_name = "runtime.interrupt_acknowledged"
            payload = {"checkpoint_state": RuntimeState.INTERRUPTED.value}
        else:
            event_name = "runtime.state_changed"
            payload = {
                "from": _safe_identifier(raw.get("from"), "unknown"),
                "to": _safe_identifier(target, "unknown"),
                "reason": _safe_identifier(raw.get("reason"), "state_transition"),
            }
    elif event.event_type is EventType.MODEL_CALL_STARTED:
        event_name = "model.call_started"
        payload = {
            "backend": _safe_identifier(raw.get("backend"), "unknown"),
            "attempt": _nonnegative_int(raw.get("attempt")),
        }
    elif event.event_type in {
        EventType.MODEL_CALL_SUCCEEDED,
        EventType.MODEL_CALL_FAILED,
        EventType.MODEL_CALL_UNCERTAIN,
    }:
        event_name = "model.call_finished"
        payload = {
            "status": (
                "success"
                if event.event_type is EventType.MODEL_CALL_SUCCEEDED
                else "uncertain"
                if event.event_type is EventType.MODEL_CALL_UNCERTAIN
                else "failed"
            ),
            "error_kind": _safe_identifier(raw.get("kind"), "none"),
        }
    elif event.event_type in {EventType.TOOL_CALL_STARTED, EventType.TOOL_CALL_RUNNING}:
        call = raw.get("call")
        call_map = call if isinstance(call, Mapping) else {}
        event_name = "tool.call_started"
        payload = {
            "tool_name": _safe_identifier(call_map.get("name"), "unknown"),
            "attempt": _nonnegative_int(raw.get("attempt", 1)),
        }
    elif event.event_type in {EventType.TOOL_CALL_FINISHED, EventType.TOOL_CALL_UNCERTAIN}:
        result = raw.get("result")
        result_map = result if isinstance(result, Mapping) else {}
        event_name = "tool.call_finished"
        payload = {
            "tool_name": _safe_identifier(result_map.get("tool_name"), "unknown"),
            "status": _safe_identifier(
                "uncertain"
                if event.event_type is EventType.TOOL_CALL_UNCERTAIN
                else result_map.get("status"),
                "unknown",
            ),
            "duration_ms": _nonnegative_number(result_map.get("duration_ms")),
        }
    elif event.event_type is EventType.RUN_FINISHED:
        event_name = "runtime.finished"
        payload = {"state": _safe_identifier(raw.get("final_state"), event.state.value)}
    if event_name is None:
        return None
    projected = {
        "protocol_version": PROTOCOL_VERSION,
        "kind": "event",
        "execution_id": execution_id,
        "runtime_session_id": event.session_id,
        "sequence": sequence,
        "timestamp": event.timestamp,
        "type": event_name,
        "payload": _sanitize_json(
            payload, private_values=private_values, secret_values=secret_values
        ),
    }
    validate_document(projected, "event_envelope")
    return projected


def terminal_result(
    request: ValidatedExecutionRequest,
    result: RunResult,
    *,
    sequence: int,
    events: list[Event],
    duration_ms: float,
    cancellation_cause: str | None,
    private_values: tuple[str, ...] = (),
    secret_values: tuple[str, ...] = (),
) -> dict[str, Any]:
    status_name, failure = _terminal_status(result, cancellation_cause)
    final_output = result.final_answer if status_name == "succeeded" else None
    if final_output is not None:
        final_output = _sanitize_text(final_output, private_values, secret_values)[:200_000]
        if not final_output:
            status_name = "failed"
            failure = _failure("runtime", "empty_final_output", "Runtime returned no safe output")
            final_output = None
    document = {
        "protocol_version": PROTOCOL_VERSION,
        "kind": "result",
        "execution_id": str(request.document["execution_id"]),
        "runtime_session_id": result.session_id,
        "sequence": sequence,
        "timestamp": utc_now(),
        "status": status_name,
        "final_output": final_output,
        "failure": _sanitize_json(
            failure, private_values=private_values, secret_values=secret_values
        ),
        "metrics": _public_metrics(events, result, duration_ms),
        "artifacts": _trajectory_artifacts(request, result),
    }
    validate_document(document, "terminal_result")
    return document


def schema_digests() -> dict[str, str]:
    root = schema_root()
    return {
        name: hashlib.sha256((root / filename).read_bytes()).hexdigest()
        for name, filename in SCHEMA_FILES.items()
    }


def _validate_private_inputs(
    document: dict[str, Any], request_file: Path, attempt_root: Path
) -> ValidatedExecutionRequest:
    source_raw = _object(document, "source")
    runtime_raw = _object(document, "runtime")
    backend_raw = _object(document, "backend")
    source = _contained_path(source_raw["path"], attempt_root, "source", must_exist=True)
    if source.is_symlink() or not source.is_dir():
        raise ProtocolError("private source must be a non-symlink directory", exit_code=EXIT_PRIVATE_IO)
    agent_home = _contained_path(
        runtime_raw["agent_home"], attempt_root, "runtime home", must_exist=False
    )
    if agent_home.exists() and (agent_home.is_symlink() or not agent_home.is_dir()):
        raise ProtocolError("private runtime home must be a directory", exit_code=EXIT_PRIVATE_IO)
    if _overlaps(source, agent_home):
        raise ProtocolError("source and runtime home must not overlap", exit_code=EXIT_PRIVATE_IO)
    kind = str(backend_raw["kind"])
    if kind not in BACKEND_KINDS:
        raise ProtocolError("execution request selects an unsupported backend", exit_code=EXIT_INVALID_REQUEST)
    script_path: Path | None = None
    if kind == "scripted":
        script_path = _contained_path(
            backend_raw["script_path"], attempt_root, "script", must_exist=True
        )
        if script_path.is_symlink() or not script_path.is_file():
            raise ProtocolError("private script must be a non-symlink file", exit_code=EXIT_PRIVATE_IO)
    else:
        api_key_env = str(backend_raw["api_key_env"])
        allowed = {
            "openai-compatible": {"OPENAI_API_KEY", "DEEPSEEK_API_KEY"},
            "anthropic": {"ANTHROPIC_API_KEY"},
        }[kind]
        if api_key_env not in allowed:
            raise ProtocolError(
                "backend secret environment name is not allowed", exit_code=EXIT_INVALID_REQUEST
            )
    required = document["required_capabilities"]
    assert isinstance(required, list)
    missing = sorted(set(str(item) for item in required) - ENABLED_CAPABILITIES)
    if missing:
        raise ProtocolError(
            f"required Runtime capabilities are unavailable: {', '.join(missing)}",
            exit_code=EXIT_INVALID_REQUEST,
        )
    if document["extensions"]:
        raise ProtocolError(
            "semantic Runtime IPC extensions are not supported", exit_code=EXIT_INVALID_REQUEST
        )
    try:
        actual_revision = tree_fingerprint(source)
    except OSError as exc:
        raise ProtocolError("private source cannot be fingerprinted", exit_code=EXIT_PRIVATE_IO) from exc
    if str(source_raw["expected_revision"]) != actual_revision:
        raise ProtocolError("private source revision does not match", exit_code=EXIT_PRIVATE_IO)
    return ValidatedExecutionRequest(
        document=document,
        request_file=request_file,
        attempt_root=attempt_root,
        source=source,
        agent_home=agent_home,
        script_path=script_path,
    )


def _backend_from_request(request: ValidatedExecutionRequest) -> ModelBackend:
    raw = _object(request.document, "backend")
    kind = str(raw["kind"])
    if kind == "scripted":
        assert request.script_path is not None
        try:
            return ScriptedBackend.from_file(request.script_path)
        except (OSError, ValueError) as exc:
            raise ProtocolError(
                "private scripted backend input is invalid", exit_code=EXIT_PRIVATE_IO
            ) from exc
    timeout = float(_object(request.document, "runtime")["timeout_seconds"])
    model = str(raw["model"])
    api_key_env = str(raw["api_key_env"])
    if kind == "openai-compatible":
        return OpenAICompatibleBackend(
            model=model,
            base_url=str(raw.get("base_url") or "https://api.openai.com/v1"),
            api_key_env=api_key_env,
            timeout=timeout,
            thinking=str(raw["thinking"]) if raw.get("thinking") is not None else None,
        )
    return AnthropicBackend(
        model=model,
        base_url=str(raw.get("base_url") or "https://api.anthropic.com/v1"),
        api_key_env=api_key_env,
        timeout=timeout,
    )


def _policy_from_request(document: Mapping[str, Any]) -> RunPolicy:
    raw = _object(document, "policy")
    return RunPolicy(
        max_steps=int(raw["max_steps"]),
        max_model_calls=int(raw["max_model_calls"]),
        max_tool_calls=int(raw["max_tool_calls"]),
        max_output_tokens=int(raw["max_output_tokens"]),
        allowed_permissions=frozenset(
            Permission(str(item)) for item in raw["allowed_permissions"]
        ),
    )


def _terminal_status(
    result: RunResult, cancellation_cause: str | None
) -> tuple[str, dict[str, Any] | None]:
    if result.state is RuntimeState.COMPLETED:
        return "succeeded", None
    if result.state is RuntimeState.INTERRUPTED:
        if cancellation_cause == "timeout":
            return "timed_out", _failure(
                "timeout", "runtime_timeout", "Runtime deadline elapsed"
            )
        return "cancelled", _failure("cancel", "cancelled", "Cancellation was acknowledged")
    raw_failure = result.failure or {}
    code = _safe_identifier(raw_failure.get("kind"), "runtime_failed")
    return "failed", _failure(
        _failure_category(code), code, "Runtime ended with a classified failure"
    )


def _failure(category: str, code: str, message: str) -> dict[str, Any]:
    return {
        "category": category,
        "code": code,
        "safe_message": message,
        "retryable": code in {"timeout", "rate_limit", "provider_unavailable"},
        "details": {},
    }


def _failure_category(code: str) -> str:
    if "model" in code or code in {"timeout", "rate_limit", "provider_unavailable"}:
        return "model"
    if "tool" in code or "permission" in code:
        return "tool"
    if "context" in code or "compression" in code:
        return "context"
    if "workspace" in code or "source" in code:
        return "workspace"
    if "budget" in code:
        return "policy"
    return "runtime"


def _public_metrics(events: list[Event], result: RunResult, duration_ms: float) -> dict[str, Any]:
    input_tokens = 0
    output_tokens = 0
    for event in events:
        if event.event_type not in {
            EventType.MODEL_CALL_SUCCEEDED,
            EventType.COMPRESSION_FINISHED,
        }:
            continue
        usage = event.payload.get("usage")
        if isinstance(usage, Mapping):
            input_tokens += _nonnegative_int(usage.get("input_tokens"))
            output_tokens += _nonnegative_int(usage.get("output_tokens"))
    return {
        "model_calls": max(0, result.model_calls),
        "tool_calls": max(0, result.tool_calls),
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "duration_ms": max(0.0, duration_ms),
        "agent_count": 1,
        "delegation_count": 0,
        "extensions": {},
    }


def _trajectory_artifacts(
    request: ValidatedExecutionRequest, result: RunResult
) -> list[dict[str, Any]]:
    path = Path(result.trace_path)
    try:
        resolved = path.resolve(strict=True)
        relative = resolved.relative_to(request.attempt_root).as_posix()
        content = resolved.read_bytes()
    except (OSError, ValueError):
        return []
    return [
        {
            "kind": "trajectory",
            "relative_path": relative,
            "media_type": "application/x-ndjson",
            "sha256": hashlib.sha256(content).hexdigest(),
            "size_bytes": len(content),
        }
    ]


def _reject_request_secrets(raw_text: str, document: Mapping[str, Any]) -> None:
    if re.search(r"(?i)authorization\s*[:=]?\s*bearer\s+\S+", raw_text):
        raise ProtocolError(
            "execution request contains a credential value", exit_code=EXIT_INVALID_REQUEST
        )
    if re.search(r"(?i)\b(?:sk-|xox[baprs]-)[A-Za-z0-9_-]{6,}", raw_text):
        raise ProtocolError(
            "execution request contains a credential value", exit_code=EXIT_INVALID_REQUEST
        )
    backend = document.get("backend")
    if isinstance(backend, Mapping) and isinstance(backend.get("api_key_env"), str):
        value = os.environ.get(str(backend["api_key_env"]))
        if value and len(value) >= 8 and value in raw_text:
            raise ProtocolError(
                "execution request contains a credential value", exit_code=EXIT_INVALID_REQUEST
            )


def _request_secret_values(document: Mapping[str, Any]) -> tuple[str, ...]:
    backend = document.get("backend")
    if not isinstance(backend, Mapping) or not isinstance(backend.get("api_key_env"), str):
        return ()
    value = os.environ.get(str(backend["api_key_env"]))
    return (value,) if value else ()


def _contained_path(value: Any, root: Path, label: str, *, must_exist: bool) -> Path:
    lexical = Path(str(value))
    if not lexical.is_absolute():
        raise ProtocolError(f"private {label} must be absolute", exit_code=EXIT_PRIVATE_IO)
    if lexical.is_symlink():
        raise ProtocolError(
            f"private {label} must not be a symlink", exit_code=EXIT_PRIVATE_IO
        )
    try:
        resolved = lexical.resolve(strict=must_exist)
        resolved.relative_to(root)
    except (OSError, ValueError) as exc:
        raise ProtocolError(
            f"private {label} is outside the attempt root", exit_code=EXIT_PRIVATE_IO
        ) from exc
    if resolved == root:
        raise ProtocolError(
            f"private {label} cannot equal the attempt root", exit_code=EXIT_PRIVATE_IO
        )
    return resolved


def _overlaps(first: Path, second: Path) -> bool:
    try:
        first.relative_to(second)
        return True
    except ValueError:
        pass
    try:
        second.relative_to(first)
        return True
    except ValueError:
        return False


def _sanitize_json(
    value: Any, *, private_values: tuple[str, ...], secret_values: tuple[str, ...]
) -> Any:
    if isinstance(value, str):
        return _sanitize_text(value, private_values, secret_values)
    if isinstance(value, Mapping):
        return {
            _sanitize_text(str(key), private_values, secret_values)[:128]: _sanitize_json(
                child, private_values=private_values, secret_values=secret_values
            )
            for key, child in value.items()
        }
    if isinstance(value, list):
        return [
            _sanitize_json(item, private_values=private_values, secret_values=secret_values)
            for item in value
        ]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return _sanitize_text(str(value), private_values, secret_values)


def _sanitize_text(
    value: str, private_values: tuple[str, ...], secret_values: tuple[str, ...]
) -> str:
    safe = redact_sensitive_text(value)
    for secret in sorted((item for item in secret_values if item), key=len, reverse=True):
        safe = safe.replace(secret, "[REDACTED]")
    for private in sorted((item for item in private_values if item), key=len, reverse=True):
        safe = safe.replace(private, "[PRIVATE_PATH]")
    return re.sub(r"(?<![A-Za-z0-9_.-])/(?:[^\s,;:'\"<>]+)", "[PRIVATE_PATH]", safe)


def _safe_identifier(value: Any, fallback: str) -> str:
    normalized = re.sub(
        r"[^a-z0-9_.-]+", "_", str(value or fallback).lower()
    ).strip("_.-")
    return (normalized or fallback)[:128]


def _nonnegative_int(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _nonnegative_number(value: Any) -> float:
    try:
        return max(0.0, float(value or 0.0))
    except (TypeError, ValueError):
        return 0.0


def _object(value: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    child = value[key]
    if not isinstance(child, Mapping):
        raise ProtocolError(
            f"execution request field {key} must be an object", exit_code=EXIT_INVALID_REQUEST
        )
    return child


def _require_protocol_version(value: int) -> None:
    if value != PROTOCOL_VERSION:
        raise ProtocolError(
            f"unsupported protocol version: {value}",
            exit_code=EXIT_UNSUPPORTED_PROTOCOL,
        )


def _runtime_version() -> str:
    try:
        installed = version("production-coding-agent")
    except PackageNotFoundError:
        installed = RUNTIME_VERSION
    return RUNTIME_VERSION if installed == "0.1.0" else installed


@contextmanager
def _headless_signal_handler(controller: CancellationController) -> Iterator[None]:
    if threading.current_thread() is not threading.main_thread():
        yield
        return
    previous = signal.getsignal(signal.SIGINT)

    def handle_interrupt(signum: int, frame: object) -> None:
        del signum, frame
        controller.request("cancel")

    signal.signal(signal.SIGINT, handle_interrupt)
    try:
        yield
    finally:
        signal.signal(signal.SIGINT, previous)
