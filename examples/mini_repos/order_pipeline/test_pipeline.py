from __future__ import annotations

import unittest

from pipeline import render_report


class PipelineTest(unittest.TestCase):
    def test_report_normalizes_and_formats_amount(self) -> None:
        self.assertEqual(render_report("  Late-Fee  ", 3.5), "late-fee=$3.50")

    def test_zero_amount_is_explicit(self) -> None:
        self.assertEqual(render_report("Credit", 0), "credit=$0.00")


if __name__ == "__main__":
    unittest.main()
