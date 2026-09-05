from __future__ import annotations

from catalog import get_product
from pricing import apply_discount


def checkout_total(sku: str, quantity: int, discount: float = 0.0) -> float:
    if quantity <= 0:
        raise ValueError("quantity must be positive")
    product = get_product(sku)
    subtotal = product.price * quantity
    return apply_discount(subtotal, discount)
