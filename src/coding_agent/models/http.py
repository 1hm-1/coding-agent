from __future__ import annotations

import os
from typing import Any, Mapping

from coding_agent.domain import BackendError
from coding_agent.models.errors import classify_http_error, ensure_object


def secret_from_environment(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise BackendError(
            f"required provider secret {name} is not configured",
            kind="authentication",
        )
    return value


def post_json(
    *,
    url: str,
    headers: Mapping[str, str],
    payload: Mapping[str, Any],
    timeout: Any,
    transport: Any | None,
    provider: str,
) -> Mapping[str, Any]:
    """POST JSON through an injectable transport and normalize HTTP failures."""

    client = None
    try:
        if transport is not None and hasattr(transport, "post"):
            response = transport.post(url, headers=dict(headers), json=dict(payload), timeout=timeout)
        elif transport is not None and hasattr(transport, "request"):
            response = transport.request(
                "POST", url, headers=dict(headers), json=dict(payload), timeout=timeout
            )
        else:
            try:
                import httpx
            except ImportError as exc:
                raise BackendError(
                    "httpx is required for the default provider transport",
                    kind="provider_unavailable",
                ) from exc
            if transport is None:
                client = httpx.Client(timeout=timeout)
            else:
                client = httpx.Client(transport=transport, timeout=timeout)
            response = client.post(url, headers=dict(headers), json=dict(payload))
    except BackendError:
        raise
    except TimeoutError as exc:
        raise BackendError(str(exc) or "provider request timed out", kind="timeout") from exc
    except Exception as exc:
        try:
            import httpx

            timeout_types = (httpx.TimeoutException,)
        except ImportError:
            timeout_types = ()
        if timeout_types and isinstance(exc, timeout_types):
            raise BackendError("provider request timed out", kind="timeout") from exc
        raise BackendError(
            f"provider transport failed: {type(exc).__name__}",
            kind="provider_unavailable",
        ) from exc
    finally:
        if client is not None:
            client.close()

    status_code = int(getattr(response, "status_code", 0))
    try:
        body = response.json()
    except Exception:
        body = None
    if status_code < 200 or status_code >= 300:
        response_headers = getattr(response, "headers", {})
        raise classify_http_error(
            status_code,
            body,
            response_headers,
            provider=provider,
        )
    return ensure_object(body, f"{provider} response")
