from __future__ import annotations

from invoice.registry import rule_for


def premium_total(amount: float, region: str) -> float:
    return rule_for(region).adjust(amount)
