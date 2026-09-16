from __future__ import annotations

from dataclasses import dataclass
import re

from coding_agent.memory.domain import MemoryRecord, MemoryScope, MemoryValidationError


class MemoryPolicyError(MemoryValidationError):
    """A structurally valid memory violates trust or containment policy."""


@dataclass(frozen=True)
class MemoryWriteContext:
    actor_id: str
    session_id: str | None = None
    repository_id: str | None = None
    user_id: str | None = None
    allowed_scopes: frozenset[MemoryScope] = frozenset()

    def __post_init__(self) -> None:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,255}", self.actor_id):
            raise MemoryPolicyError("memory actor_id is invalid")

    def scope_id(self, scope: MemoryScope) -> str | None:
        return {
            MemoryScope.SESSION: self.session_id,
            MemoryScope.REPOSITORY: self.repository_id,
            MemoryScope.USER: self.user_id,
        }[scope]


class MemoryPolicy:
    _secret_patterns = (
        re.compile(r"(?i)\b(api[_-]?key|access[_-]?token|secret|password)\s*[:=]\s*\S+"),
        re.compile(r"\bsk-[A-Za-z0-9_-]{12,}\b"),
        re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    )
    _absolute_path = re.compile(r"(?<![A-Za-z0-9_.-])(?:/[A-Za-z0-9_.-]+){2,}(?:/|\b)")
    _windows_path = re.compile(r"(?i)\b[A-Z]:\\(?:[^\\\s]+\\)+[^\\\s]+")
    _instruction_patterns = (
        re.compile(
            r"(?is)\b(?:ignore|disregard|override)\b.{0,80}"
            r"\b(?:instructions?|system prompt|policy)\b"
        ),
        re.compile(r"(?i)\b(?:run|execute|invoke|call)\s+(?:the\s+)?(?:tool|shell|command)\b"),
    )
    _tool_output_field = re.compile(
        r'(?i)"(?:stdout|stderr|tool_name|tool_call_id|execution_id)"\s*:'
    )

    def validate_proposal(
        self,
        record: MemoryRecord,
        context: MemoryWriteContext,
        *,
        provenance_valid: bool,
    ) -> None:
        if record.scope not in context.allowed_scopes:
            raise MemoryPolicyError("memory scope is not allowed for this write")
        expected_scope_id = context.scope_id(record.scope)
        if expected_scope_id is None or record.scope_id != expected_scope_id:
            raise MemoryPolicyError("memory scope ownership does not match write context")
        if not provenance_valid:
            raise MemoryPolicyError("memory provenance could not be validated")
        if any(pattern.search(record.content) for pattern in self._secret_patterns):
            raise MemoryPolicyError("memory content may contain a secret")
        if self._absolute_path.search(record.content) or self._windows_path.search(record.content):
            raise MemoryPolicyError("memory content may contain a host absolute path")
        if any(pattern.search(record.content) for pattern in self._instruction_patterns):
            raise MemoryPolicyError("memory content may contain embedded instructions")
        if self._tool_output_field.search(record.content):
            raise MemoryPolicyError("memory content appears to contain complete tool output")

    def validate_actor(self, record: MemoryRecord, context: MemoryWriteContext) -> None:
        expected_scope_id = context.scope_id(record.scope)
        if record.scope not in context.allowed_scopes or expected_scope_id != record.scope_id:
            raise MemoryPolicyError("actor cannot mutate this memory scope")
