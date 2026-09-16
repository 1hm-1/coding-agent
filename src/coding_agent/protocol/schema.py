from __future__ import annotations

from datetime import datetime
import json
import math
from pathlib import Path
import re
import sys
from typing import Any, Mapping, Sequence
from urllib.parse import urlsplit


SCHEMA_FILES = {
    "capabilities": "capabilities.schema.json",
    "execution_request": "execution-request.schema.json",
    "event_envelope": "event-envelope.schema.json",
    "terminal_result": "terminal-result.schema.json",
}


class DocumentValidationError(ValueError):
    """A public protocol document does not satisfy its authority Schema."""


def schema_root() -> Path:
    source_root = Path(__file__).resolve().parents[3] / "protocol" / "v1"
    if all((source_root / filename).is_file() for filename in SCHEMA_FILES.values()):
        return source_root
    installed_root = Path(sys.prefix) / "share" / "production-coding-agent" / "protocol" / "v1"
    if all((installed_root / filename).is_file() for filename in SCHEMA_FILES.values()):
        return installed_root
    raise FileNotFoundError("Runtime IPC schema bundle is unavailable")


def load_schema(name: str) -> dict[str, Any]:
    try:
        filename = SCHEMA_FILES[name]
    except KeyError as exc:
        raise ValueError(f"unknown Runtime IPC schema: {name}") from exc
    try:
        decoded = json.loads((schema_root() / filename).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError(f"Runtime IPC schema is unreadable: {name}") from exc
    if not isinstance(decoded, dict):
        raise ValueError(f"Runtime IPC schema must be an object: {name}")
    validate_schema_definition(decoded)
    return decoded


def validate_schema_bundle() -> None:
    identifiers: set[str] = set()
    for name in SCHEMA_FILES:
        schema = load_schema(name)
        identifier = schema.get("$id")
        if not isinstance(identifier, str) or not identifier:
            raise ValueError(f"Runtime IPC schema has no $id: {name}")
        if identifier in identifiers:
            raise ValueError(f"duplicate Runtime IPC schema $id: {identifier}")
        identifiers.add(identifier)


def validate_schema_definition(schema: Mapping[str, Any]) -> None:
    if schema.get("$schema") != "https://json-schema.org/draft/2020-12/schema":
        raise ValueError("Runtime IPC schema must declare Draft 2020-12")
    _check_schema_node(schema, root=schema, path="$", is_root=True)


def validate_document(value: Any, schema_name: str) -> None:
    schema = load_schema(schema_name)
    _validate(value, schema, root=schema, path="$")


def _check_schema_node(
    schema: Mapping[str, Any],
    *,
    root: Mapping[str, Any],
    path: str,
    is_root: bool = False,
) -> None:
    if not isinstance(schema, Mapping):
        raise ValueError(f"{path} schema node must be an object")
    reference = schema.get("$ref")
    if reference is not None:
        if not isinstance(reference, str):
            raise ValueError(f"{path} $ref must be a string")
        _resolve_reference(reference, root)
    pattern = schema.get("pattern")
    if pattern is not None:
        if not isinstance(pattern, str):
            raise ValueError(f"{path} pattern must be a string")
        re.compile(pattern)
    for keyword in ("properties", "$defs"):
        children = schema.get(keyword, {})
        if not isinstance(children, Mapping):
            raise ValueError(f"{path}.{keyword} must be an object")
        for name, child in children.items():
            if not isinstance(child, Mapping):
                raise ValueError(f"{path}.{keyword}.{name} must be an object")
            _check_schema_node(child, root=root, path=f"{path}.{keyword}.{name}")
    for keyword in ("items", "contains", "additionalProperties", "propertyNames", "not", "if", "then", "else"):
        child = schema.get(keyword)
        if isinstance(child, Mapping):
            _check_schema_node(child, root=root, path=f"{path}.{keyword}")
        elif child is not None and keyword == "additionalProperties" and isinstance(child, bool):
            pass
        elif child is not None and keyword in {"items", "contains", "propertyNames", "not", "if", "then", "else"}:
            raise ValueError(f"{path}.{keyword} must be an object")
    for keyword in ("allOf", "anyOf", "oneOf"):
        children = schema.get(keyword, [])
        if not isinstance(children, list):
            raise ValueError(f"{path}.{keyword} must be an array")
        for index, child in enumerate(children):
            if not isinstance(child, Mapping):
                raise ValueError(f"{path}.{keyword}[{index}] must be an object")
            _check_schema_node(child, root=root, path=f"{path}.{keyword}[{index}]")
    if is_root and not isinstance(schema.get("type"), (str, list)):
        raise ValueError("Runtime IPC root schema must declare type")


def _validate(value: Any, schema: Mapping[str, Any], *, root: Mapping[str, Any], path: str) -> None:
    reference = schema.get("$ref")
    if isinstance(reference, str):
        _validate(value, _resolve_reference(reference, root), root=root, path=path)

    if "const" in schema and value != schema["const"]:
        raise DocumentValidationError(f"{path} must equal {schema['const']!r}")
    if "enum" in schema and value not in schema["enum"]:
        raise DocumentValidationError(f"{path} is not an allowed value")

    expected = schema.get("type")
    if expected is not None:
        accepted = [expected] if isinstance(expected, str) else expected
        if not isinstance(accepted, list) or not any(_matches_type(value, item) for item in accepted):
            raise DocumentValidationError(f"{path} has an invalid type")

    for keyword in ("allOf",):
        for child in _schema_sequence(schema.get(keyword), path, keyword):
            _validate(value, child, root=root, path=path)
    for keyword in ("anyOf", "oneOf"):
        children = _schema_sequence(schema.get(keyword), path, keyword)
        if not children:
            continue
        matches = sum(_is_valid(value, child, root=root, path=path) for child in children)
        if keyword == "anyOf" and matches == 0:
            raise DocumentValidationError(f"{path} does not match any allowed schema")
        if keyword == "oneOf" and matches != 1:
            raise DocumentValidationError(f"{path} must match exactly one allowed schema")
    negated = schema.get("not")
    if isinstance(negated, Mapping) and _is_valid(value, negated, root=root, path=path):
        raise DocumentValidationError(f"{path} matches a forbidden schema")
    condition = schema.get("if")
    if isinstance(condition, Mapping):
        branch = schema.get("then") if _is_valid(value, condition, root=root, path=path) else schema.get("else")
        if isinstance(branch, Mapping):
            _validate(value, branch, root=root, path=path)

    if isinstance(value, Mapping):
        _validate_object(value, schema, root=root, path=path)
    elif isinstance(value, list):
        _validate_array(value, schema, root=root, path=path)
    elif isinstance(value, str):
        _validate_string(value, schema, path=path)
    elif _matches_type(value, "number"):
        _validate_number(value, schema, path=path)


def _validate_object(
    value: Mapping[str, Any], schema: Mapping[str, Any], *, root: Mapping[str, Any], path: str
) -> None:
    if len(value) < int(schema.get("minProperties", 0)):
        raise DocumentValidationError(f"{path} has too few properties")
    maximum = schema.get("maxProperties")
    if isinstance(maximum, int) and len(value) > maximum:
        raise DocumentValidationError(f"{path} has too many properties")
    required = schema.get("required", [])
    if isinstance(required, list):
        missing = [name for name in required if name not in value]
        if missing:
            raise DocumentValidationError(f"{path} is missing required fields: {', '.join(missing)}")
    properties = schema.get("properties", {})
    if not isinstance(properties, Mapping):
        properties = {}
    additional = schema.get("additionalProperties", True)
    unknown = [name for name in value if name not in properties]
    if additional is False and unknown:
        raise DocumentValidationError(f"{path} has unknown fields: {', '.join(sorted(unknown))}")
    property_names = schema.get("propertyNames")
    for name, child in value.items():
        if isinstance(property_names, Mapping):
            _validate(name, property_names, root=root, path=f"{path}.<property>")
        child_schema = properties.get(name)
        if isinstance(child_schema, Mapping):
            _validate(child, child_schema, root=root, path=f"{path}.{name}")
        elif isinstance(additional, Mapping):
            _validate(child, additional, root=root, path=f"{path}.{name}")


def _validate_array(
    value: list[Any], schema: Mapping[str, Any], *, root: Mapping[str, Any], path: str
) -> None:
    if len(value) < int(schema.get("minItems", 0)):
        raise DocumentValidationError(f"{path} has too few items")
    maximum = schema.get("maxItems")
    if isinstance(maximum, int) and len(value) > maximum:
        raise DocumentValidationError(f"{path} has too many items")
    if schema.get("uniqueItems") is True:
        serialized = [json.dumps(item, sort_keys=True, separators=(",", ":")) for item in value]
        if len(serialized) != len(set(serialized)):
            raise DocumentValidationError(f"{path} must contain unique items")
    item_schema = schema.get("items")
    if isinstance(item_schema, Mapping):
        for index, item in enumerate(value):
            _validate(item, item_schema, root=root, path=f"{path}[{index}]")
    contains = schema.get("contains")
    if isinstance(contains, Mapping) and not any(
        _is_valid(item, contains, root=root, path=f"{path}[{index}]")
        for index, item in enumerate(value)
    ):
        raise DocumentValidationError(f"{path} does not contain a required item")


def _validate_string(value: str, schema: Mapping[str, Any], *, path: str) -> None:
    if len(value) < int(schema.get("minLength", 0)):
        raise DocumentValidationError(f"{path} is too short")
    maximum = schema.get("maxLength")
    if isinstance(maximum, int) and len(value) > maximum:
        raise DocumentValidationError(f"{path} is too long")
    pattern = schema.get("pattern")
    if isinstance(pattern, str) and re.search(pattern, value) is None:
        raise DocumentValidationError(f"{path} has an invalid format")
    format_name = schema.get("format")
    if format_name == "uri":
        parsed = urlsplit(value)
        if not parsed.scheme or (parsed.scheme in {"http", "https"} and not parsed.netloc):
            raise DocumentValidationError(f"{path} must be an absolute URI")
    elif format_name == "date-time":
        try:
            datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise DocumentValidationError(f"{path} must be an ISO date-time") from exc


def _validate_number(value: Any, schema: Mapping[str, Any], *, path: str) -> None:
    numeric = float(value)
    if not math.isfinite(numeric):
        raise DocumentValidationError(f"{path} must be finite")
    minimum = schema.get("minimum")
    if isinstance(minimum, (int, float)) and numeric < float(minimum):
        raise DocumentValidationError(f"{path} is below the minimum")
    maximum = schema.get("maximum")
    if isinstance(maximum, (int, float)) and numeric > float(maximum):
        raise DocumentValidationError(f"{path} is above the maximum")
    exclusive = schema.get("exclusiveMinimum")
    if isinstance(exclusive, (int, float)) and numeric <= float(exclusive):
        raise DocumentValidationError(f"{path} must be above the exclusive minimum")


def _matches_type(value: Any, expected: Any) -> bool:
    if expected == "null":
        return value is None
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if expected == "string":
        return isinstance(value, str)
    if expected == "array":
        return isinstance(value, list)
    if expected == "object":
        return isinstance(value, Mapping)
    return False


def _resolve_reference(reference: str, root: Mapping[str, Any]) -> Mapping[str, Any]:
    if not reference.startswith("#/"):
        raise ValueError("only local Runtime IPC schema references are supported")
    current: Any = root
    for part in reference[2:].split("/"):
        key = part.replace("~1", "/").replace("~0", "~")
        if not isinstance(current, Mapping) or key not in current:
            raise ValueError(f"unresolved Runtime IPC schema reference: {reference}")
        current = current[key]
    if not isinstance(current, Mapping):
        raise ValueError(f"Runtime IPC schema reference is not an object: {reference}")
    return current


def _schema_sequence(value: Any, path: str, keyword: str) -> Sequence[Mapping[str, Any]]:
    if value is None:
        return ()
    if not isinstance(value, list) or any(not isinstance(item, Mapping) for item in value):
        raise ValueError(f"{path}.{keyword} must be an array of schemas")
    return value


def _is_valid(value: Any, schema: Mapping[str, Any], *, root: Mapping[str, Any], path: str) -> bool:
    try:
        _validate(value, schema, root=root, path=path)
    except DocumentValidationError:
        return False
    return True
