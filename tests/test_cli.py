from __future__ import annotations

import subprocess
import sys
import unittest


class CliSmokeTest(unittest.TestCase):
    def test_entrypoint_and_all_subcommands_load_in_subprocesses(self) -> None:
        commands = (
            None,
            "run-scripted",
            "run",
            "replay",
            "sessions",
            "show",
            "resume",
            "interrupt",
            "resolve-call",
            "export-trace",
            "evaluate",
        )
        for command in commands:
            with self.subTest(command=command):
                argv = [sys.executable, "-m", "coding_agent.cli"]
                if command is not None:
                    argv.append(command)
                argv.append("--help")
                completed = subprocess.run(
                    argv,
                    capture_output=True,
                    text=True,
                    timeout=10,
                    check=False,
                )
                self.assertEqual(completed.returncode, 0, completed.stderr)
                self.assertIn("usage:", completed.stdout)


if __name__ == "__main__":
    unittest.main()
