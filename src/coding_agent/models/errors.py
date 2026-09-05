from __future__ import annotations

from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Mapping

from coding_agent.domain import BackendError, JsonObject, redact_sensitive_text


RETRYABLE_KINDS = frozenset({"timeout", "rate_limit", "provider_unavailable"})


def retry_after_seconds(headers: Mapping[str, Any]) -> float | None:
    """Parse the standard Retry-After header without retaining provider headers."""

    raw = headers.get("retry-after") or headers.get("Retry-After")
    if raw is None:
        return None
    try:
        return max(0.0, float(raw))
    except (TypeError, ValueError):
        try:
            target = parsedate_to_datetime(str(raw))
            if target.tzinfo is None:
                target = target.replace(tzinfo=timezone.utc)
            return max(0.0, target.timestamp() - datetime.now(timezone.utc).timestamp())
        except (TypeError, ValueError, OverflowError, IndexError):
            return None


def response_message(body: Any, default: str) -> str:
    if isinstance(body, Mapping):
        error = body.get("error")
        if isinstance(error, Mapping):
            for key in ("message", "type", "code"):
                value = error.get(key)
                if value:
                    return redact_sensitive_text(str(value))
        message = body.get("message")
        if message:
            return redact_sensitive_text(str(message))
    return redact_sensitive_text(default)


def classify_http_error(
    status_code: int,
    body: Any,
    headers: Mapping[str, Any],
    *,
    provider: str,
) -> BackendError:
    error_code = ""
    if isinstance(body, Mapping) and isinstance(body.get("error"), Mapping):
        error_code = str(
            body["error"].get("code") or body["error"].get("type") or ""
        ).lower()
    if status_code == 408:
        kind = "timeout"
    elif status_code == 429:
        kind = "rate_limit"
    elif status_code in {401, 403}:
        kind = "authentication"
    elif status_code in {400, 422}:
        kind = "content_blocked" if any(
            marker in error_code for marker in ("safety", "content_filter", "blocked")
        ) else "invalid_request"
    elif 500 <= status_code <= 599:
        kind = "provider_unavailable"
    else:
        kind = "provider_error"
    return BackendError(
        response_message(body, f"{provider} returned HTTP {status_code}"),
        kind=kind,
        retry_after=retry_after_seconds(headers) if kind == "rate_limit" else None,
        provider_metadata={"provider": provider, "status_code": status_code},
    )


def ensure_object(value: Any, description: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise BackendError(
            f"{description} must be an object",
            kind="protocol_error",
        )
    return value


def usage_token_count(value: Any, field: str) -> int:
    """Validate one provider token count without accepting coercible junk values."""

    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise BackendError(
            f"{field} must be a non-negative integer",
            kind="protocol_error",
        )
    return value


def safe_provider_metadata(provider: str, *, model: str, finish_reason: str) -> JsonObject:
    return {
        "provider": provider,
        "model": model,
        "finish_reason": finish_reason,
    }
