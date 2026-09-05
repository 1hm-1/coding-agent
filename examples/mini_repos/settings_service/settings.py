from __future__ import annotations


VALID_MODES = frozenset({"safe", "fast"})


def parse_mode(raw: str) -> str | None:
    value = raw.lower()
    if value not in VALID_MODES:
        return None
    return value
