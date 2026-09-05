from __future__ import annotations

import unittest

from todo_parser import Command, parse_command


class TodoParserTest(unittest.TestCase):
    def test_parses_add_command(self) -> None:
        self.assertEqual(
            parse_command("add buy milk"),
            Command(action="add", text="buy milk"),
        )

    def test_rejects_unknown_command(self) -> None:
        self.assertIsNone(parse_command("remove buy milk"))

    def test_empty_input_is_ignored(self) -> None:
        self.assertIsNone(parse_command(""))

    def test_whitespace_input_is_ignored(self) -> None:
        self.assertIsNone(parse_command("   "))


if __name__ == "__main__":
    unittest.main()

