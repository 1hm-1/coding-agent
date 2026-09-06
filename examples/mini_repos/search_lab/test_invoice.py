from __future__ import annotations

import unittest

from invoice.api import premium_total


class InvoiceTest(unittest.TestCase):
    def test_east_premium_total(self) -> None:
        self.assertAlmostEqual(premium_total(100.0, "EAST"), 112.0)

    def test_west_total_is_unchanged(self) -> None:
        self.assertEqual(premium_total(100.0, "WEST"), 105.0)


if __name__ == "__main__":
    unittest.main()
