from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from coding_agent.application import AgentApplication
from coding_agent.domain import (
    EventType,
    InvariantViolation,
    Permission,
    RunPolicy,
    RuntimeState,
    ToolCall,
    ToolStatus,
)
from coding_agent.models.scripted import ScriptedBackend
from coding_agent.test_profiles import default_test_profiles
from coding_agent.tools.base import ToolContext, ToolDefinition, ToolRegistry
from coding_agent.tools.builtin import build_builtin_registry
from coding_agent.tools.harness import ToolHarness
from coding_agent.trajectory import replay
from coding_agent.workspace import WorkspaceManager, tree_fingerprint


PROJECT_ROOT = Path(__file__).resolve().parents[1]
GOLDEN_ROOT = Path(__file__).resolve().parent / "golden"


class EngineeringHardeningTest(unittest.TestCase):
    def assert_golden(self, name: str, actual: dict[str, object]) -> None:
        expected = json.loads((GOLDEN_ROOT / f"{name}.json").read_text(encoding="utf-8"))
        self.assertEqual(actual, expected)

    def test_success_trajectory_matches_semantic_golden(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            source = PROJECT_ROOT / "examples" / "fixture"
            source_before = tree_fingerprint(source)
            application = AgentApplication(Path(temporary) / "agent-home")
            backend = ScriptedBackend.from_file(
                PROJECT_ROOT / "examples" / "scripted_run.json"
            )

            result = application.run_task(
                source=source,
                task="修复 add 函数并运行测试",
                backend=backend,
            )
            projection = application.replay_session(
                result.session_id
            ).semantic_projection()

            self.assertIs(result.state, RuntimeState.COMPLETED)
            self.assertEqual(tree_fingerprint(source), source_before)
            self.assert_golden("bugfix_success", projection)

            corrupted_events = application.event_store.load(result.session_id)
            run_finished = next(
                event
                for event in corrupted_events
                if event.event_type is EventType.RUN_FINISHED
            )
            run_finished.payload["model_calls"] = 999
            with self.assertRaises(InvariantViolation):
                replay(corrupted_events)

    def test_permission_denial_is_observed_without_runtime_crash(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            source.mkdir()
            (source / "value.txt").write_text("old\n", encoding="utf-8")
            application = AgentApplication(root / "agent-home")
            backend = ScriptedBackend(
                [
                    {
                        "tool_calls": [
                            {
                                "id": "denied-edit",
                                "name": "edit_file",
                                "arguments": {
                                    "path": "value.txt",
                                    "old_text": "old",
                                    "new_text": "new",
                                },
                            }
                        ],
                        "usage": {"input_tokens": 10, "output_tokens": 5},
                    },
                    {
                        "final": "The edit was blocked by the write permission policy.",
                        "usage": {"input_tokens": 20, "output_tokens": 5},
                    },
                ]
            )
            policy = RunPolicy(
                allowed_permissions=frozenset(
                    {Permission.READ, Permission.EXECUTE_TEST}
                )
            )

            result = application.run_task(
                source=source,
                task="Change old to new.",
                backend=backend,
                policy=policy,
            )

            self.assertIs(result.state, RuntimeState.COMPLETED)
            self.assertEqual(
                Path(result.workspace_path, "value.txt").read_text(encoding="utf-8"),
                "old\n",
            )
            observations = [
                json.loads(message.content)
                for message in backend.requests[1].messages
                if message.role == "tool"
            ]
            self.assertEqual(observations[-1]["status"], "permission_denied")
            self.assertNotIn(
                "edit_file", {schema["name"] for schema in backend.requests[0].tools}
            )
            self.assert_golden(
                "permission_denied",
                application.replay_session(result.session_id).semantic_projection(),
            )

    def test_test_failure_is_observed_then_recovered_in_todo_demo(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            source = PROJECT_ROOT / "examples" / "todo_cli"
            source_before = tree_fingerprint(source)
            application = AgentApplication(Path(temporary) / "agent-home")
            backend = ScriptedBackend.from_file(
                PROJECT_ROOT / "examples" / "todo_cli_scripted_run.json"
            )

            result = application.run_task(
                source=source,
                task="Fix the empty input crash and run tests.",
                backend=backend,
            )

            self.assertIs(result.state, RuntimeState.COMPLETED)
            self.assertEqual(tree_fingerprint(source), source_before)
            failed_test_observation = [
                json.loads(message.content)
                for message in backend.requests[3].messages
                if message.tool_call_id == "todo-first-test"
            ]
            self.assertEqual(len(failed_test_observation), 1)
            self.assertFalse(failed_test_observation[0]["data"]["passed"])
            final_test_observation = [
                json.loads(message.content)
                for message in backend.requests[5].messages
                if message.tool_call_id == "todo-final-test"
            ]
            self.assertEqual(len(final_test_observation), 1)
            self.assertTrue(final_test_observation[0]["data"]["passed"])
            self.assertIn(
                "return None",
                Path(result.workspace_path, "todo_parser.py").read_text(encoding="utf-8"),
            )
            self.assert_golden(
                "test_failure_recovery",
                application.replay_session(result.session_id).semantic_projection(),
            )

    def test_invalid_backend_response_matches_runtime_failure_golden(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            source.mkdir()
            (source / "value.txt").write_text("value\n", encoding="utf-8")
            application = AgentApplication(root / "agent-home")

            result = application.run_task(
                source=source,
                task="Read value.",
                backend=ScriptedBackend([{"tool_calls": "invalid"}]),
            )

            self.assertIs(result.state, RuntimeState.FAILED)
            self.assert_golden(
                "runtime_failure",
                application.replay_session(result.session_id).semantic_projection(),
            )

    def test_unexpected_handler_exception_is_structured_and_audited(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            source.mkdir()
            (source / "value.txt").write_text("value\n", encoding="utf-8")
            workspace_manager = WorkspaceManager(root / "agent-home")
            workspace_manager.create(source, "fault-session")
            registry = ToolRegistry()
            definition = ToolDefinition(
                name="read_file",
                description="fault-injected read",
                input_schema={
                    "type": "object",
                    "properties": {"path": {"type": "string"}},
                    "required": ["path"],
                    "additionalProperties": False,
                },
                permission=Permission.READ,
            )

            def exploding_handler(arguments, context):
                raise RuntimeError("injected handler failure")

            registry.register(definition, exploding_handler)
            audit: list[dict[str, object]] = []
            harness = ToolHarness(registry, audit_sink=audit.append)

            result = harness.execute(
                ToolCall(
                    id="fault-call",
                    name="read_file",
                    arguments={"path": "value.txt"},
                ),
                ToolContext(
                    workspace=workspace_manager.get("fault-session"),
                    allowed_permissions=frozenset({Permission.READ}),
                ),
            )

            self.assertIs(result.status, ToolStatus.EXECUTION_ERROR)
            self.assertEqual(result.error["kind"], "unhandled_tool_error")
            self.assertEqual(
                [entry["event"] for entry in audit],
                ["tool_harness_started", "tool_harness_finished"],
            )
            self.assertEqual(
                build_builtin_registry(default_test_profiles()).names,
                ("read_file", "edit_file", "restricted_test", "run_command"),
            )


if __name__ == "__main__":
    unittest.main()
