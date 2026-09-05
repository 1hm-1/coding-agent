from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Product:
    sku: str
    price: float


CATALOG = {
    "coffee": Product(sku="coffee", price=19.99),
    "tea": Product(sku="tea", price=8.50),
}


def get_product(sku: str) -> Product:
    try:
        return CATALOG[sku]
    except KeyError as exc:
        raise ValueError(f"unknown sku: {sku}") from exc
