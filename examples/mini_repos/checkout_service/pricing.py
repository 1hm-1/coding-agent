from __future__ import annotations


def apply_discount(subtotal: float, rate: float) -> float:
    if not 0 <= rate <= 1:
        raise ValueError("discount rate must be between zero and one")
    return round(subtotal * (1 - rate), 0)
