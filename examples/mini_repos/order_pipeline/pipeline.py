from __future__ import annotations

from formatting import format_amount
from normalization import normalize_label


def render_report(label: str, amount: float) -> str:
    normalized = normalize_label(label)
    return format_amount(normalized, amount)
