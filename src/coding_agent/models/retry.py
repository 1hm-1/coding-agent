from __future__ import annotations

from typing import Callable


def bounded_exponential_backoff(
    retry_count: int,
    *,
    base_seconds: float,
    max_seconds: float,
    jitter_seconds: float = 0.0,
    random_source: Callable[[], float] | None = None,
) -> float:
    """Return a bounded backoff delay; callers own persistence and sleeping."""

    if retry_count <= 0:
        raise ValueError("retry_count must be positive")
    if base_seconds < 0 or max_seconds < 0 or jitter_seconds < 0:
        raise ValueError("backoff values cannot be negative")
    if base_seconds > max_seconds:
        raise ValueError("base_seconds cannot exceed max_seconds")
    delay = min(max_seconds, base_seconds * (2 ** (retry_count - 1)))
    if jitter_seconds:
        sample = (random_source or __import__("random").random)()
        delay += ((sample * 2.0) - 1.0) * jitter_seconds
    return max(0.0, min(max_seconds, delay))

