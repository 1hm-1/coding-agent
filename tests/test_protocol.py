from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
from copy import deepcopy
import io
import json
from pathlib import Path
import shutil
import signal
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from coding_agent.cli import main as cli_main
from coding_agent.domain import Event, EventType, RuntimeState, utc_now
from coding_agent.models.base import ModelBackend
from coding_agent.domain import ModelRequest, ModelResponse, Usage
from coding_agent.protocol.headless import (
    CancellationController,
    EXIT_INVALID_REQUEST,
    EXIT_PRIVATE_IO,
    EXIT_UNSUPPORTED_PROTOCOL,
    ProtocolError,
    ProtocolOutputError,
    ProtocolWriter,
    load_execution_request,
    project_event,
    protocol_info,
    run_headless,
)
from coding_agent.protocol.schema import validate_document, validate_schema_bundle
from coding_agent.workspace import tree_fingerprint


ROOT = Path(__file__).resolve().parents[1]


class InterruptingBackend(ModelBackend):
    name = "scripted"

    def __init__(self, cancel: threading.Event):
        self.cancel = cancel

    def complete(self, request: ModelRequest) -> ModelResponse:
        del request
        self.cancel.set()
        return ModelResponse(text="must be reused after resume", usage=Usage(2, 1))


class FailingStream(io.StringIO):
    def write(self, value: str) -> int:
        del value
        raise OSError("simulated closed stdout")


class RuntimeIPCProducerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="coding-agent-ipc-")
        self.attempt = Path(self.temporary.name) / "attempt"
        self.input_root = self.attempt / "input"
        self.source = self.attempt / "source"
        self.agent_home = self.attempt / "runtime-home"
        self.input_root.mkdir(parents=True)
        shutil.copytree(ROOT / "examples" / "fixture", self.source)
        self.script = self.input_root / "script.json"
        self.request_file = self.input_root / "request.json"
        self._write_script([{"final": "done", "usage": {"input_tokens": 3, "output_tokens": 1}}])
        self.request = self._request_document()
        self._write_request(self.request)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _request_document(self) -> dict[str, object]:
        return {
            "protocol_version": 1,
            "kind": "execution_request",
            "execution_id": "exec-test",
            "correlation": {"run_id": "run-test", "attempt_id": "attempt-test"},
            "task": "Return a deterministic final answer.",
            "source": {
                "path": str(self.source),
                "expected_revision": tree_fingerprint(self.source),
                "read_only": True,
            },
            "runtime": {"agent_home": str(self.agent_home), "timeout_seconds": 10},
            "backend": {"kind": "scripted", "script_path": str(self.script)},
            "policy": {
                "max_steps": 32,
                "max_model_calls": 8,
                "max_tool_calls": 12,
                "max_output_tokens": 2048,
                "allowed_permissions": ["read", "write", "execute_test"],
            },
            "required_capabilities": ["structured_events", "cooperative_interrupt"],
            "metadata": {"request_origin": "test"},
            "extensions": {},
        }

    def _write_request(self, document: object) -> None:
        self.request_file.write_text(json.dumps(document), encoding="utf-8")
        self.request_file.chmod(0o600)

    def _write_script(self, document: object) -> None:
        self.script.write_text(json.dumps(document), encoding="utf-8")
        self.script.chmod(0o600)

    def _run(self, **kwargs: object) -> tuple[int, list[dict[str, object]], str]:
        output = io.StringIO()
        code = run_headless(1, self.request_file, output=output, **kwargs)
        records = [json.loads(line) for line in output.getvalue().splitlines()]
        return code, records, output.getvalue()

    def _semantic(self, records: list[dict[str, object]]) -> dict[str, object]:
        result = records[-1]
        failure = result["failure"]
        assert failure is None or isinstance(failure, dict)
        metrics = result["metrics"]
        assert isinstance(metrics, dict)
        return {
            "event_types": [record["type"] for record in records[:-1]],
            "result_status": result["status"],
            "failure_code": failure["code"] if failure else None,
            "agent_count": metrics["agent_count"],
            "delegation_count": metrics["delegation_count"],
        }

    def _golden(self, name: str) -> dict[str, object]:
        return json.loads((ROOT / "tests" / "golden" / name).read_text(encoding="utf-8"))

    def test_schema_bundle_capability_document_and_contract_vectors(self) -> None:
        validate_schema_bundle()
        info = protocol_info()
        validate_document(info, "capabilities")
        self.assertEqual(
            set(info["capabilities"]),
            {
                "structured_events",
                "cooperative_interrupt",
                "checkpoint_resume",
                "scripted_backend",
                "real_model_backend",
            },
        )
        vector = json.loads(
            (ROOT / "protocol" / "v1" / "execution-request.vector.json").read_text(
                encoding="utf-8"
            )
        )
        validate_document(vector, "execution_request")
        kernel = json.loads(
            (ROOT / "protocol" / "v1" / "v0.1-kernel.vector.json").read_text()
        )
        bridge = json.loads(
            (ROOT / "protocol" / "v1" / "v0.2-bridge.vector.json").read_text()
        )
        self.assertEqual(
            kernel["expected_terminal_contract"], bridge["expected_terminal_contract"]
        )

    def test_request_schema_is_strict_for_missing_unknown_and_wrong_type(self) -> None:
        mutations = []
        missing = deepcopy(self.request)
        del missing["task"]
        mutations.append(missing)
        unknown = deepcopy(self.request)
        unknown["unexpected"] = True
        mutations.append(unknown)
        wrong_type = deepcopy(self.request)
        wrong_type["policy"]["max_steps"] = "32"  # type: ignore[index]
        mutations.append(wrong_type)
        for document in mutations:
            with self.subTest(document=document):
                self._write_request(document)
                with self.assertRaises(ProtocolError) as raised:
                    load_execution_request(self.request_file)
                self.assertEqual(raised.exception.exit_code, EXIT_INVALID_REQUEST)

    def test_request_json_size_permissions_protocol_and_secret_fail_without_stdout(self) -> None:
        self.request_file.write_text("not-json", encoding="utf-8")
        self.request_file.chmod(0o600)
        with self.assertRaises(ProtocolError) as invalid:
            load_execution_request(self.request_file)
        self.assertEqual(invalid.exception.exit_code, EXIT_INVALID_REQUEST)

        self._write_request(self.request)
        self.request_file.chmod(0o644)
        with self.assertRaises(ProtocolError) as permissions:
            load_execution_request(self.request_file)
        self.assertEqual(permissions.exception.exit_code, EXIT_PRIVATE_IO)

        version = deepcopy(self.request)
        version["protocol_version"] = 2
        self._write_request(version)
        with self.assertRaises(ProtocolError) as unsupported:
            load_execution_request(self.request_file)
        self.assertEqual(unsupported.exception.exit_code, EXIT_UNSUPPORTED_PROTOCOL)

        secret = deepcopy(self.request)
        secret["task"] = "do not leak sk-super-secret-value"
        self._write_request(secret)
        with self.assertRaises(ProtocolError) as credential:
            load_execution_request(self.request_file)
        self.assertEqual(credential.exception.exit_code, EXIT_INVALID_REQUEST)

        with self.request_file.open("wb") as handle:
            handle.seek(1_048_576)
            handle.write(b"x")
        self.request_file.chmod(0o600)
        with self.assertRaises(ProtocolError) as oversized:
            load_execution_request(self.request_file)
        self.assertEqual(oversized.exception.exit_code, EXIT_INVALID_REQUEST)

    def test_backend_capability_revision_and_containment_fail_closed(self) -> None:
        backend = deepcopy(self.request)
        backend["backend"] = {
            "kind": "unknown-provider",
            "model": "model",
            "api_key_env": "OPENAI_API_KEY",
        }
        self._write_request(backend)
        with self.assertRaises(ProtocolError) as unsupported_backend:
            load_execution_request(self.request_file)
        self.assertEqual(unsupported_backend.exception.exit_code, EXIT_INVALID_REQUEST)

        unsupported = deepcopy(self.request)
        unsupported["required_capabilities"] = ["memory"]
        self._write_request(unsupported)
        with self.assertRaises(ProtocolError) as capability:
            load_execution_request(self.request_file)
        self.assertEqual(capability.exception.exit_code, EXIT_INVALID_REQUEST)

        revision = deepcopy(self.request)
        revision["source"]["expected_revision"] = "wrong"  # type: ignore[index]
        self._write_request(revision)
        with self.assertRaises(ProtocolError) as mismatch:
            load_execution_request(self.request_file)
        self.assertEqual(mismatch.exception.exit_code, EXIT_PRIVATE_IO)

        outside = deepcopy(self.request)
        outside["source"]["path"] = str(ROOT / "examples" / "fixture")  # type: ignore[index]
        outside["source"]["expected_revision"] = tree_fingerprint(  # type: ignore[index]
            ROOT / "examples" / "fixture"
        )
        self._write_request(outside)
        with self.assertRaises(ProtocolError) as containment:
            load_execution_request(self.request_file)
        self.assertEqual(containment.exception.exit_code, EXIT_PRIVATE_IO)

        source_link = self.attempt / "source-link"
        source_link.symlink_to(self.source, target_is_directory=True)
        symlinked = deepcopy(self.request)
        symlinked["source"]["path"] = str(source_link)  # type: ignore[index]
        self._write_request(symlinked)
        with self.assertRaises(ProtocolError) as symlink:
            load_execution_request(self.request_file)
        self.assertEqual(symlink.exception.exit_code, EXIT_PRIVATE_IO)

        extension = deepcopy(self.request)
        extension["extensions"] = {"future.feature": True}
        self._write_request(extension)
        with self.assertRaises(ProtocolError) as unsupported_extension:
            load_execution_request(self.request_file)
        self.assertEqual(unsupported_extension.exception.exit_code, EXIT_INVALID_REQUEST)

    def test_scripted_success_stream_is_contiguous_schema_valid_and_matches_golden(self) -> None:
        code, records, raw = self._run()
        self.assertEqual(code, 0)
        self.assertTrue(records)
        self.assertEqual([record["sequence"] for record in records], list(range(1, len(records) + 1)))
        self.assertEqual(sum(record["kind"] == "result" for record in records), 1)
        self.assertEqual(records[-1]["kind"], "result")
        for record in records[:-1]:
            validate_document(record, "event_envelope")
        validate_document(records[-1], "terminal_result")
        self.assertEqual(self._semantic(records), self._golden("runtime_ipc_success.json"))
        vector = json.loads(
            (ROOT / "protocol" / "v1" / "v0.2-bridge.vector.json").read_text()
        )
        metrics = records[-1]["metrics"]
        self.assertEqual(
            {
                "status": records[-1]["status"],
                "agent_count": metrics["agent_count"],  # type: ignore[index]
                "delegation_count": metrics["delegation_count"],  # type: ignore[index]
            },
            vector["expected_terminal_contract"],
        )
        self.assertNotIn(str(self.attempt), raw)
        artifact = records[-1]["artifacts"][0]  # type: ignore[index]
        self.assertFalse(str(artifact["relative_path"]).startswith("/"))

    def test_runtime_failure_has_one_failed_result_and_matches_golden(self) -> None:
        self._write_script([{"tool_calls": "not-an-array"}])
        code, records, _ = self._run()
        self.assertEqual(code, 0)
        self.assertEqual(self._semantic(records), self._golden("runtime_ipc_failure.json"))
        self.assertEqual(records[-1]["status"], "failed")

    def test_final_output_redacts_secret_and_absolute_paths(self) -> None:
        secret = "sk-output-secret-value"
        self._write_script([{"final": f"done {self.source} {secret} /etc/passwd"}])
        _, records, raw = self._run()
        self.assertNotIn(str(self.source), raw)
        self.assertNotIn(secret, raw)
        self.assertNotIn("/etc/passwd", raw)
        self.assertIn("[REDACTED]", str(records[-1]["final_output"]))
        self.assertIn("[PRIVATE_PATH]", str(records[-1]["final_output"]))

    def test_unknown_internal_event_is_not_projected(self) -> None:
        event = Event(
            schema_version=1,
            event_id="private-id",
            session_id="session-1",
            sequence=1,
            event_type=EventType.MESSAGE_ADDED,
            timestamp=utc_now(),
            state=RuntimeState.CREATED,
            payload={"message": {"content": "private"}},
        )
        self.assertIsNone(project_event(event, execution_id="exec-1", sequence=1))

    def test_writer_rejects_oversize_duplicate_result_and_io_failure(self) -> None:
        event = {
            "protocol_version": 1,
            "kind": "event",
            "execution_id": "exec-1",
            "runtime_session_id": "session-1",
            "sequence": 1,
            "timestamp": utc_now(),
            "type": "runtime.started",
            "payload": {"value": "x" * 1000},
        }
        with self.assertRaises(ProtocolOutputError):
            ProtocolWriter(io.StringIO(), max_record_bytes=128).write_event(event)
        with self.assertRaises(ProtocolOutputError):
            ProtocolWriter(FailingStream()).write_event(event)
        unserializable = deepcopy(event)
        unserializable["payload"] = {"value": float("nan")}
        with self.assertRaises(ProtocolOutputError):
            ProtocolWriter(io.StringIO()).write_event(unserializable)

        _, records, _ = self._run()
        writer = ProtocolWriter(io.StringIO())
        terminal = deepcopy(records[-1])
        terminal["sequence"] = 1
        writer.write_result(terminal)
        terminal["sequence"] = 2
        with self.assertRaises(ProtocolOutputError):
            writer.write_result(terminal)

        self._write_request(self.request)
        with self.assertRaises(ProtocolOutputError) as integrated_failure:
            run_headless(1, self.request_file, output=FailingStream())
        self.assertEqual(integrated_failure.exception.exit_code, 70)

    def test_cancel_before_start_commits_checkpoint_before_single_result(self) -> None:
        cancelled = threading.Event()
        cancelled.set()
        code, records, _ = self._run(cancel_event=cancelled)
        self.assertEqual(code, 0)
        self.assertEqual(self._semantic(records), self._golden("runtime_ipc_cancelled.json"))
        self.assertLess(
            [record.get("type") for record in records].index("runtime.interrupt_acknowledged"),
            len(records) - 1,
        )
        session_id = str(records[-1]["runtime_session_id"])
        connection = sqlite3.connect(self.agent_home / "state.db")
        try:
            row = connection.execute("SELECT state FROM sessions WHERE id=?", (session_id,)).fetchone()
        finally:
            connection.close()
        self.assertEqual(row, ("interrupted",))

    def test_cancel_during_model_and_tool_boundaries(self) -> None:
        model_cancel = threading.Event()
        _, model_records, _ = self._run(
            cancel_event=model_cancel,
            backend_override=InterruptingBackend(model_cancel),
        )
        self.assertEqual(model_records[-1]["status"], "cancelled")
        self.assertIn("model.call_finished", [record.get("type") for record in model_records])

        self.tearDown()
        self.setUp()
        self._write_script(
            [
                {
                    "tool_calls": [
                        {"id": "test-call", "name": "restricted_test", "arguments": {"profile": "python_unittest"}}
                    ]
                },
                {"final": "should not be consumed"},
            ]
        )
        tool_cancel = threading.Event()

        def cancel_on_running(event: Event) -> None:
            if event.event_type is EventType.TOOL_CALL_RUNNING:
                tool_cancel.set()

        _, tool_records, _ = self._run(cancel_event=tool_cancel, event_hook=cancel_on_running)
        self.assertEqual(tool_records[-1]["status"], "cancelled")
        types = [record.get("type") for record in tool_records]
        self.assertIn("tool.call_started", types)
        self.assertIn("tool.call_finished", types)
        self.assertLess(types.index("runtime.interrupt_acknowledged"), len(types) - 1)

    def test_repeated_cancel_is_idempotent(self) -> None:
        controller = CancellationController()
        controller.request("cancel")
        controller.request("timeout")
        self.assertTrue(controller.requested())
        self.assertEqual(controller.cause, "cancel")

    def test_repeated_sigint_during_tool_cleans_child_before_cancelled_result(self) -> None:
        (self.source / "test_slow.py").write_text(
            "import time\nimport unittest\n\n"
            "class SlowTest(unittest.TestCase):\n"
            "    def test_slow(self):\n"
            "        time.sleep(0.5)\n",
            encoding="utf-8",
        )
        self._write_script(
            [
                {
                    "tool_calls": [
                        {
                            "id": "slow-test",
                            "name": "restricted_test",
                            "arguments": {"profile": "python_unittest"},
                        }
                    ]
                },
                {"final": "must not complete"},
            ]
        )
        self.request = self._request_document()
        self._write_request(self.request)
        process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "coding_agent.cli",
                "run-headless",
                "--protocol-version",
                "1",
                "--request-file",
                str(self.request_file),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        assert process.stdout is not None
        prefix: list[str] = []
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            line = process.stdout.readline()
            if not line:
                break
            prefix.append(line)
            record = json.loads(line)
            if record.get("type") == "tool.call_started":
                process.send_signal(signal.SIGINT)
                process.send_signal(signal.SIGINT)
                break
        remaining_stdout, stderr = process.communicate(timeout=15)
        records = [json.loads(line) for line in "".join(prefix).splitlines()]
        records.extend(json.loads(line) for line in remaining_stdout.splitlines())
        self.assertEqual(process.returncode, 0, stderr)
        self.assertEqual(records[-1]["status"], "cancelled")
        self.assertEqual(sum(record["kind"] == "result" for record in records), 1)

        connection = sqlite3.connect(self.agent_home / "state.db")
        try:
            row = connection.execute(
                "SELECT result_json FROM tool_calls WHERE result_json IS NOT NULL"
            ).fetchone()
        finally:
            connection.close()
        self.assertIsNotNone(row)
        tool_result = json.loads(row[0])
        sandbox = tool_result.get("data", {}).get("sandbox", {})
        if sandbox.get("backend") == "linux_user_mount_pid_net":
            self.assertTrue(sandbox["cleanup_verified"])

    def test_runtime_deadline_returns_timed_out_after_safe_boundary(self) -> None:
        (self.source / "test_slow.py").write_text(
            "import time\nimport unittest\n\n"
            "class SlowTest(unittest.TestCase):\n"
            "    def test_slow(self):\n"
            "        time.sleep(0.2)\n",
            encoding="utf-8",
        )
        self._write_script(
            [
                {
                    "tool_calls": [
                        {
                            "id": "deadline-test",
                            "name": "restricted_test",
                            "arguments": {"profile": "python_unittest"},
                        }
                    ]
                }
            ]
        )
        self.request = self._request_document()
        self.request["runtime"]["timeout_seconds"] = 0.05  # type: ignore[index]
        self._write_request(self.request)
        code, records, _ = self._run()
        self.assertEqual(code, 0)
        self.assertEqual(records[-1]["status"], "timed_out")
        self.assertEqual(records[-1]["failure"]["code"], "runtime_timeout")  # type: ignore[index]
        self.assertIn("runtime.interrupt_acknowledged", [record.get("type") for record in records])

    def test_crash_after_events_before_result_is_recognizable(self) -> None:
        output = io.StringIO()

        def crash() -> None:
            raise RuntimeError("simulated bridge crash")

        with self.assertRaises(RuntimeError):
            run_headless(1, self.request_file, output=output, before_result=crash)
        records = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertTrue(records)
        self.assertFalse(any(record["kind"] == "result" for record in records))

    def test_cli_exit_mappings_have_no_protocol_stdout_on_start_failure(self) -> None:
        commands = []
        invalid = deepcopy(self.request)
        del invalid["task"]
        self._write_request(invalid)
        commands.append((1, EXIT_INVALID_REQUEST))
        unsupported = deepcopy(self.request)
        unsupported["protocol_version"] = 2
        commands.append((2, EXIT_UNSUPPORTED_PROTOCOL))
        for document, (request_version, expected) in zip((invalid, unsupported), commands):
            self._write_request(document)
            completed = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "coding_agent.cli",
                    "run-headless",
                    "--protocol-version",
                    str(request_version),
                    "--request-file",
                    str(self.request_file),
                ],
                capture_output=True,
                text=True,
                timeout=15,
                check=False,
            )
            self.assertEqual(completed.returncode, expected, completed.stderr)
            self.assertEqual(completed.stdout, "")

        self._write_request(self.request)
        self.request_file.chmod(0o644)
        completed = subprocess.run(
            [
                sys.executable,
                "-m",
                "coding_agent.cli",
                "run-headless",
                "--protocol-version",
                "1",
                "--request-file",
                str(self.request_file),
            ],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        self.assertEqual(completed.returncode, EXIT_PRIVATE_IO)
        self.assertEqual(completed.stdout, "")

        stdout = io.StringIO()
        stderr = io.StringIO()
        with patch("coding_agent.cli.run_headless", side_effect=RuntimeError("private detail")):
            with redirect_stdout(stdout), redirect_stderr(stderr):
                code = cli_main(
                    [
                        "run-headless",
                        "--protocol-version",
                        "1",
                        "--request-file",
                        str(self.request_file),
                    ]
                )
        self.assertEqual(code, 70)
        self.assertEqual(stdout.getvalue(), "")
        self.assertNotIn("private detail", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
