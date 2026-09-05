from __future__ import annotations

from settings import parse_mode


def start_service(raw_mode: str) -> str:
    mode = parse_mode(raw_mode)
    if mode is None:
        return "rejected"
    return f"started:{mode}"


def accepts_requests(raw_mode: str) -> bool:
    return parse_mode(raw_mode) is not None
