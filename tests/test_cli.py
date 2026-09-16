from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess
import sys
import unittest


class CliSmokeTest(unittest.TestCase):
    def test_entrypoint_and_all_subcommands_load_in_subprocesses(self) -> None:
        commands = (
            None,
            "protocol-info",
            "run-headless",
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

    def test_protocol_info_is_single_json_record_with_current_schema_digests(self) -> None:
        completed = subprocess.run(
            [
                sys.executable,
                "-m",
                "coding_agent.cli",
                "protocol-info",
                "--protocol-version",
                "1",
                "--output",
                "json",
            ],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(completed.stderr, "")
        self.assertEqual(len(completed.stdout.splitlines()), 1)
        document = json.loads(completed.stdout)
        self.assertEqual(
            set(document),
            {
                "protocol_version",
                "kind",
                "runtime_name",
                "runtime_version",
                "supported_protocol_versions",
                "capabilities",
                "backend_kinds",
                "sandbox_modes",
                "schema_digests",
                "limits",
                "extensions",
            },
        )
        self.assertEqual(document["protocol_version"], 1)
        self.assertEqual(document["kind"], "protocol_info")
        self.assertEqual(document["runtime_name"], "coding-agent")
        self.assertEqual(document["runtime_version"], "0.2.0.dev0")
        self.assertEqual(
            set(document["capabilities"]),
            {
                "checkpoint_resume",
                "cooperative_interrupt",
                "real_model_backend",
                "scripted_backend",
                "structured_events",
            },
        )
        schema_root = Path(__file__).resolve().parents[1] / "protocol" / "v1"
        expected = {
            "capabilities": "capabilities.schema.json",
            "execution_request": "execution-request.schema.json",
            "event_envelope": "event-envelope.schema.json",
            "terminal_result": "terminal-result.schema.json",
        }
        self.assertEqual(
            document["schema_digests"],
            {
                name: hashlib.sha256((schema_root / filename).read_bytes()).hexdigest()
                for name, filename in expected.items()
            },
        )

    def test_protocol_info_rejects_unsupported_protocol_without_stdout(self) -> None:
        completed = subprocess.run(
            [
                sys.executable,
                "-m",
                "coding_agent.cli",
                "protocol-info",
                "--protocol-version",
                "2",
                "--output",
                "json",
            ],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        self.assertEqual(completed.returncode, 65)
        self.assertEqual(completed.stdout, "")
        self.assertIn("unsupported protocol version", completed.stderr)


if __name__ == "__main__":
    unittest.main()
