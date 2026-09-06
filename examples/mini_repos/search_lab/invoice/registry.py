from __future__ import annotations

from invoice.rules import (
    rule_01,
    rule_02,
    rule_03,
    rule_04,
    rule_05,
    rule_06,
    rule_07,
    rule_08,
    rule_09,
    rule_10,
    rule_11,
    rule_12,
)

_RULES = (
    rule_01,
    rule_02,
    rule_03,
    rule_04,
    rule_05,
    rule_06,
    rule_07,
    rule_08,
    rule_09,
    rule_10,
    rule_11,
    rule_12,
)


def rule_for(region: str):
    normalized = region.strip().upper()
    return next(rule for rule in _RULES if rule.RULE_CODE == normalized)
