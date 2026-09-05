from __future__ import annotations

import unittest

from checkout import checkout_total


class CheckoutTest(unittest.TestCase):
    def test_discount_keeps_cents(self) -> None:
        self.assertEqual(checkout_total("coffee", 1, 0.15), 16.99)

    def test_quantity_and_zero_discount(self) -> None:
        self.assertEqual(checkout_total("tea", 2), 17.0)


if __name__ == "__main__":
    unittest.main()
