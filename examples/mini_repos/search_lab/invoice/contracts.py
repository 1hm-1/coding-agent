from __future__ import annotations

from typing import Protocol


class PricingRule(Protocol):
    RULE_CODE: str

    @staticmethod
    def adjust(amount: float) -> float: ...
