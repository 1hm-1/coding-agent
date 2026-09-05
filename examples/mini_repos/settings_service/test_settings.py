from __future__ import annotations

import unittest

from service import accepts_requests, start_service
from settings import parse_mode


class SettingsTest(unittest.TestCase):
    def test_mode_parser_trims_and_normalizes(self) -> None:
        self.assertEqual(parse_mode(" SAFE "), "safe")
        self.assertEqual(start_service(" FAST "), "started:fast")

    def test_unknown_mode_is_rejected(self) -> None:
        self.assertIsNone(parse_mode("debug"))
        self.assertFalse(accepts_requests("debug"))


if __name__ == "__main__":
    unittest.main()
