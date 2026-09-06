from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from coding_agent.application import AgentApplication
from coding_agent.domain import EventType, RunPolicy, RuntimeState
from coding_agent.models.scripted import ScriptedBackend
from coding_agent.workspace import tree_fingerprint
from tests.native_support import require_native_sandbox


class VerticalSliceTest(unittest.TestCase):
    @require_native_sandbox
    def test_read_edit_test_final_isolated_and_replayable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            source.mkdir()
            (source / "calculator.py").write_text(
                "def add(left, right):\n    return left - right\n",
                encoding="utf-8",
            )
            (source / "test_calculator.py").write_text(
                "import unittest\n"
                "from calculator import add\n\n"
                "class TestCalculator(unittest.TestCase):\n"
                "    def test_add(self):\n"
                "        self.assertEqual(add(2, 3), 5)\n",
                encoding="utf-8",
            )
            source_before = tree_fingerprint(source)
            backend = ScriptedBackend(
                [
                    {
                        "tool_calls": [
                            {
                                "id": "call-read",
                                "name": "read_file",
                                "arguments": {"path": "calculator.py"},
                            }
                        ]
                    },
                    {
                        "tool_calls": [
                            {
                                "id": "call-edit",
                                "name": "edit_file",
                                "arguments": {
                                    "path": "calculator.py",
                                    "old_text": "return left - right",
                                    "new_text": "return left + right",
                                },
                            }
                        ]
                    },
                    {
                        "tool_calls": [
                            {
                                "id": "call-test",
                                "name": "restricted_test",
                                "arguments": {"profile": "python_unittest"},
                            }
                        ]
                    },
                    {"final": "Fixed the operator and verified the tests."},
                ]
            )
            application = AgentApplication(root / "agent-home")

            result = application.run_task(
                source=source,
                task="Fix add and run the tests.",
                backend=backend,
            )

            self.assertIs(result.state, RuntimeState.COMPLETED)
            self.assertEqual(result.model_calls, 4)
            self.assertEqual(result.tool_calls, 3)
            self.assertEqual(tree_fingerprint(source), source_before)
            self.assertIn(
                "return left + right",
                Path(result.workspace_path, "calculator.py").read_text(encoding="utf-8"),
            )
            self.assertEqual(
                {schema["name"] for schema in backend.requests[0].tools},
                {
                    "read_file",
                    "edit_file",
                    "search_files",
                    "restricted_test",
                    "run_command",
                },
            )
            runtime_budgets = []
            for request in backend.requests:
                runtime_message = next(
                    message
                    for message in request.messages
                    if message.metadata.get("context_kind") == "runtime_state"
                )
                runtime_budgets.append(
                    json.loads(runtime_message.content)["remaining_budgets"]
                )
            self.assertEqual(
                [budget["tool_calls"] for budget in runtime_budgets],
                [12, 11, 10, 9],
            )
            self.assertEqual(
                [budget["model_calls"] for budget in runtime_budgets],
                [8, 7, 6, 5],
            )
            second_request_observations = [
                message for message in backend.requests[1].messages if message.role == "tool"
            ]
            self.assertEqual(len(second_request_observations), 1)
            self.assertEqual(
                json.loads(second_request_observations[0].content)["status"],
                "success",
            )

            events = application.event_store.load(result.session_id)
            self.assertEqual(
                [event.sequence for event in events],
                list(range(1, len(events) + 1)),
            )
            test_results = [
                event.payload["result"]
                for event in events
                if event.event_type is EventType.TOOL_CALL_FINISHED
                and event.payload["result"]["tool_name"] == "restricted_test"
            ]
            self.assertEqual(len(test_results), 1)
            self.assertTrue(test_results[0]["data"]["passed"])

            replayed = application.replay_session(result.session_id)
            self.assertIs(replayed.final_state, RuntimeState.COMPLETED)
            self.assertEqual(replayed.model_calls, result.model_calls)
            self.assertEqual(replayed.tool_calls, result.tool_calls)

    def test_model_budget_failure_is_explicit_and_replayable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            source.mkdir()
            (source / "value.txt").write_text("value\n", encoding="utf-8")
            backend = ScriptedBackend(
                [
                    {
                        "tool_calls": [
                            {
                                "id": "only-call",
                                "name": "read_file",
                                "arguments": {"path": "value.txt"},
                            }
                        ]
                    }
                ]
            )
            application = AgentApplication(root / "agent-home")

            result = application.run_task(
                source=source,
                task="Read the value, then answer.",
                backend=backend,
                policy=RunPolicy(max_model_calls=1),
            )

            self.assertIs(result.state, RuntimeState.FAILED)
            self.assertEqual(result.failure["kind"], "model_budget_exhausted")
            replayed = application.replay_session(result.session_id)
            self.assertIs(replayed.final_state, RuntimeState.FAILED)
            self.assertEqual(replayed.model_calls, 1)

    def test_invalid_script_response_becomes_classified_run_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            source.mkdir()
            (source / "value.txt").write_text("value\n", encoding="utf-8")
            backend = ScriptedBackend([{"tool_calls": "not-an-array"}])
            application = AgentApplication(root / "agent-home")

            result = application.run_task(
                source=source,
                task="Read the value.",
                backend=backend,
            )

            self.assertIs(result.state, RuntimeState.FAILED)
            self.assertEqual(result.failure["kind"], "invalid_script_response")


if __name__ == "__main__":
    unittest.main()
