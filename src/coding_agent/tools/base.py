from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Protocol

from coding_agent.domain import (
    JsonObject,
    Permission,
    RecoveryMode,
    SchemaValidationError,
    ToolDeadlineExceeded,
    ToolStatus,
)
from coding_agent.workspace import WorkspaceGuard


@dataclass(frozen=True)
class ToolDefinition:
    name: str
    description: str
    input_schema: JsonObject
    permission: Permission
    timeout_seconds: float = 30.0
    recovery_mode: RecoveryMode = RecoveryMode.READ_ONLY

    def model_schema(self) -> JsonObject:
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.input_schema,
        }


@dataclass
class ToolContext:
    workspace: WorkspaceGuard
    allowed_permissions: frozenset[Permission]
    deadline: float = field(default_factory=lambda: float("inf"))
    max_output_chars: int = 20_000
    execution_id: str | None = None

    def check_deadline(self) -> None:
        if time.monotonic() >= self.deadline:
            raise ToolDeadlineExceeded("tool deadline exceeded")


@dataclass(frozen=True)
class ToolOutcome:
    data: JsonObject = field(default_factory=dict)
    status: ToolStatus = ToolStatus.SUCCESS
    error: JsonObject | None = None
    truncated: bool = False


class ToolHandler(Protocol):
    def __call__(self, arguments: JsonObject, context: ToolContext) -> ToolOutcome:
        ...


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, tuple[ToolDefinition, ToolHandler]] = {}

    def register(self, definition: ToolDefinition, handler: ToolHandler) -> None:
        if not definition.name or not definition.name.replace("_", "").isalnum():
            raise ValueError(f"invalid tool name: {definition.name!r}")
        if definition.name in self._tools:
            raise ValueError(f"duplicate tool registration: {definition.name}")
        if definition.timeout_seconds <= 0:
            raise ValueError("tool timeout must be positive")
        schema = definition.input_schema
        if schema.get("type") != "object":
            raise ValueError("tool input schema root must be an object")
        if schema.get("additionalProperties", True) is not False:
            raise ValueError("tool schemas must reject unknown fields")
        self._tools[definition.name] = (definition, handler)

    def get(self, name: str) -> tuple[ToolDefinition, ToolHandler] | None:
        return self._tools.get(name)

    def schemas_for(self, permissions: frozenset[Permission]) -> list[JsonObject]:
        return [
            definition.model_schema()
            for definition, _ in self._tools.values()
            if definition.permission in permissions
        ]

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(self._tools)


def validate_schema(value: Any, schema: Mapping[str, Any], *, path: str = "$") -> None:
    """Validate the deliberately small JSON Schema subset used by built-in tools."""

    if "enum" in schema and value not in schema["enum"]:
        raise SchemaValidationError(f"{path} must be one of {schema['enum']!r}")

    expected = schema.get("type")
    if expected == "object":
        if not isinstance(value, dict):
            raise SchemaValidationError(f"{path} must be an object")
        properties = schema.get("properties", {})
        for required in schema.get("required", []):
            if required not in value:
                raise SchemaValidationError(f"{path}.{required} is required")
        if schema.get("additionalProperties") is False:
            unknown = sorted(set(value) - set(properties))
            if unknown:
                raise SchemaValidationError(f"{path} has unknown fields: {', '.join(unknown)}")
        for key, child in value.items():
            if key in properties:
                validate_schema(child, properties[key], path=f"{path}.{key}")
        return

    if expected == "array":
        if not isinstance(value, list):
            raise SchemaValidationError(f"{path} must be an array")
        if "minItems" in schema and len(value) < int(schema["minItems"]):
            raise SchemaValidationError(f"{path} has too few items")
        if "maxItems" in schema and len(value) > int(schema["maxItems"]):
            raise SchemaValidationError(f"{path} has too many items")
        item_schema = schema.get("items")
        if item_schema:
            for index, item in enumerate(value):
                validate_schema(item, item_schema, path=f"{path}[{index}]")
        return

    if expected == "string":
        if not isinstance(value, str):
            raise SchemaValidationError(f"{path} must be a string")
        if "minLength" in schema and len(value) < int(schema["minLength"]):
            raise SchemaValidationError(f"{path} is too short")
        if "maxLength" in schema and len(value) > int(schema["maxLength"]):
            raise SchemaValidationError(f"{path} is too long")
        return

    if expected == "integer":
        if isinstance(value, bool) or not isinstance(value, int):
            raise SchemaValidationError(f"{path} must be an integer")
        if "minimum" in schema and value < int(schema["minimum"]):
            raise SchemaValidationError(f"{path} is below the minimum")
        if "maximum" in schema and value > int(schema["maximum"]):
            raise SchemaValidationError(f"{path} is above the maximum")
        return

    if expected == "number":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise SchemaValidationError(f"{path} must be a number")
        return

    if expected == "boolean":
        if not isinstance(value, bool):
            raise SchemaValidationError(f"{path} must be a boolean")
        return

    if expected is not None:
        raise SchemaValidationError(f"{path} uses unsupported schema type {expected!r}")


AuditSink = Callable[[JsonObject], None]
