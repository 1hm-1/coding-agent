from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from coding_agent.domain import Permission, ToolCall, ToolStatus
from coding_agent.test_profiles import TestProfile, TestProfileRegistry, default_test_profiles
from coding_agent.tools.base import ToolContext, ToolDefinition, ToolOutcome, ToolRegistry
from coding_agent.tools.builtin import build_builtin_registry
from coding_agent.tools.harness import ToolHarness
from coding_agent.workspace import WorkspaceManager


class ToolHarnessTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        source = root / "source"
        source.mkdir()
        (source / "sample.txt").write_text("alpha\nbeta\n", encoding="utf-8")
        manager = WorkspaceManager(root / "agent-home")
        manager.create(source, "tool-session")
        self.guard = manager.get("tool-session")
        self.registry = build_builtin_registry(default_test_profiles())
        self.harness = ToolHarness(self.registry)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def context(self, *permissions: Permission) -> ToolContext:
        return ToolContext(
            workspace=self.guard,
            allowed_permissions=frozenset(permissions),
        )

    def test_registry_contains_m1_and_structured_m4_tools(self) -> None:
        self.assertEqual(
            self.registry.names,
            ("read_file", "edit_file", "restricted_test", "run_command"),
        )

    def test_registry_rejects_duplicate_and_open_schema(self) -> None:
        registry = ToolRegistry()
        definition = ToolDefinition(
            name="sample",
            description="sample",
            input_schema={
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            },
            permission=Permission.READ,
        )
        def handler(arguments, context):
            return ToolOutcome()

        registry.register(definition, handler)
        with self.assertRaises(ValueError):
            registry.register(definition, handler)
        with self.assertRaises(ValueError):
            registry.register(
                ToolDefinition(
                    name="open_schema",
                    description="invalid",
                    input_schema={"type": "object", "properties": {}},
                    permission=Permission.READ,
                ),
                handler,
            )

    def test_read_file_returns_structured_content(self) -> None:
        result = self.harness.execute(
            ToolCall(id="read-1", name="read_file", arguments={"path": "sample.txt"}),
            self.context(Permission.READ),
        )
        self.assertIs(result.status, ToolStatus.SUCCESS)
        self.assertIn("1: alpha", result.data["content"])

    def test_schema_rejects_unknown_fields(self) -> None:
        result = self.harness.execute(
            ToolCall(
                id="read-2",
                name="read_file",
                arguments={"path": "sample.txt", "unexpected": True},
            ),
            self.context(Permission.READ),
        )
        self.assertIs(result.status, ToolStatus.INVALID_ARGUMENTS)
        self.assertEqual(result.error["kind"], "schema_validation")

    def test_read_file_rejects_parent_traversal(self) -> None:
        result = self.harness.execute(
            ToolCall(
                id="read-escape",
                name="read_file",
                arguments={"path": "../source/sample.txt"},
            ),
            self.context(Permission.READ),
        )
        self.assertIs(result.status, ToolStatus.INVALID_ARGUMENTS)
        self.assertEqual(result.error["kind"], "workspace_path")

    def test_permission_denies_edit(self) -> None:
        result = self.harness.execute(
            ToolCall(
                id="edit-1",
                name="edit_file",
                arguments={"path": "sample.txt", "old_text": "alpha", "new_text": "gamma"},
            ),
            self.context(Permission.READ),
        )
        self.assertIs(result.status, ToolStatus.PERMISSION_DENIED)
        self.assertEqual(
            self.guard.resolve("sample.txt").read_text(encoding="utf-8"),
            "alpha\nbeta\n",
        )

    def test_edit_requires_one_exact_occurrence(self) -> None:
        result = self.harness.execute(
            ToolCall(
                id="edit-2",
                name="edit_file",
                arguments={"path": "sample.txt", "old_text": "missing", "new_text": "value"},
            ),
            self.context(Permission.WRITE),
        )
        self.assertIs(result.status, ToolStatus.EXECUTION_ERROR)
        self.assertEqual(result.error["kind"], "non_unique_edit")

    def test_restricted_test_rejects_model_supplied_command(self) -> None:
        result = self.harness.execute(
            ToolCall(
                id="test-1",
                name="restricted_test",
                arguments={"profile": "python_unittest", "command": "anything"},
            ),
            self.context(Permission.EXECUTE_TEST),
        )
        self.assertIs(result.status, ToolStatus.INVALID_ARGUMENTS)

    def test_unknown_tool_returns_structured_error(self) -> None:
        result = self.harness.execute(
            ToolCall(id="unknown-1", name="run_shell", arguments={}),
            self.context(Permission.READ),
        )
        self.assertIs(result.status, ToolStatus.NOT_FOUND)
        self.assertEqual(result.error["kind"], "unknown_tool")

    def test_restricted_test_enforces_profile_timeout(self) -> None:
        profiles = TestProfileRegistry()
        profiles.register(
            TestProfile(
                name="slow_test",
                argv=(
                    "/usr/bin/python3",
                    "-c",
                    "import time; time.sleep(5)",
                ),
                timeout_seconds=0.05,
            )
        )
        harness = ToolHarness(build_builtin_registry(profiles))
        result = harness.execute(
            ToolCall(
                id="test-timeout",
                name="restricted_test",
                arguments={"profile": "slow_test"},
            ),
            self.context(Permission.EXECUTE_TEST),
        )
        self.assertIs(result.status, ToolStatus.TIMEOUT)
        self.assertFalse(result.data["passed"])
        self.assertTrue(result.data["process_group_terminated"])

    def test_restricted_test_reports_test_failure_as_observation(self) -> None:
        (self.guard.root / "test_failure.py").write_text(
            "import unittest\n\n"
            "class FailureTest(unittest.TestCase):\n"
            "    def test_failure(self):\n"
            "        self.fail('expected failure')\n",
            encoding="utf-8",
        )
        result = self.harness.execute(
            ToolCall(
                id="test-failure",
                name="restricted_test",
                arguments={"profile": "python_unittest"},
            ),
            self.context(Permission.EXECUTE_TEST),
        )
        self.assertIs(result.status, ToolStatus.SUCCESS)
        self.assertFalse(result.data["passed"])
        self.assertNotEqual(result.data["exit_code"], 0)


if __name__ == "__main__":
    unittest.main()
