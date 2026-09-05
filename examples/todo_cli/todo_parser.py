from __future__ import annotations

from dataclasses import dataclass


ALLOWED_ACTIONS = frozenset({"add", "done"})


@dataclass(frozen=True)
class Command:
    action: str
    text: str


def parse_command(raw: str) -> Command | None:
    """Parse '<action> <text>'; blank and unknown commands are ignored."""

    action, text = raw.strip().split(maxsplit=1)
    if action not in ALLOWED_ACTIONS:
        return None
    return Command(action=action, text=text)

