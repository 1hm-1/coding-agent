from __future__ import annotations

import unittest

from history import find_rule, rule_count


class HistoryTest(unittest.TestCase):
    def test_fixture_contains_a_long_audit_history(self) -> None:
        self.assertGreaterEqual(rule_count(), 100)

    def test_lookup_preserves_rule_order(self) -> None:
        self.assertTrue(find_rule("rule-001").startswith("rule-001:"))
        self.assertIsNone(find_rule("missing"))


if __name__ == "__main__":
    unittest.main()
