from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import sys
import tempfile
import unittest

from coding_agent.application import AgentApplication
from coding_agent.command_profiles import (
    DEFAULT_COMMAND_LIMITS,
    CommandProfile,
    CommandProfileRegistry,
    default_command_profiles,
)
from coding_agent.domain import Permission, RuntimeState, ToolCall, ToolStatus
from coding_agent.models.scripted import ScriptedBackend
from coding_agent.sandbox import ExecutionResult, FakeSandboxExecutor, LinuxNamespaceExecutor
from coding_agent.test_profiles import default_test_profiles
from coding_agent.tools.base import ToolContext
from coding_agent.tools.builtin import build_builtin_registry
from coding_agent.tools.harness import ToolHarness
from coding_agent.workspace import WorkspaceManager, tree_fingerprint
from tests.native_support import require_native_sandbox


PYTHON_EXECUTABLE = (
    "/usr/bin/python3" if Path("/usr/bin/python3").is_file() else sys.executable
)


class ProcessCrash(BaseException):
    pass


class StructuredExecutionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.source = root / "source"
        self.source.mkdir()
        (self.source / "sample.py").write_text("value = 1\n", encoding="utf-8")
        self.manager = WorkspaceManager(root / "agent-home")
        self.manager.create(self.source, "command-session")
        self.guard = self.manager.get("command-session")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def context(self, *permissions: Permission, execution_id: str | None = None) -> ToolContext:
        return ToolContext(
            workspace=self.guard,
            allowed_permissions=frozenset(permissions),
            execution_id=execution_id,
        )

    def harness(
        self,
        executor: FakeSandboxExecutor,
        profiles: CommandProfileRegistry | None = None,
    ) -> ToolHarness:
        return ToolHarness(
            build_builtin_registry(
                default_test_profiles(),
                command_profiles=profiles or default_command_profiles(),
                sandbox_executor=executor,
            )
        )

    def test_structured_argv_is_allowlisted_and_profile_controls_spec(self) -> None:
        executor = FakeSandboxExecutor()
        result = self.harness(executor).execute(
            ToolCall(
                id="command-valid",
                name="run_command",
                arguments={
                    "profile": "python_project",
                    "argv": ["python3", "-m", "unittest", "discover", "-v"],
                    "cwd": ".",
                },
            ),
            self.context(Permission.EXECUTE_COMMAND, execution_id="command-exec-1"),
        )

        self.assertIs(result.status, ToolStatus.SUCCESS)
        self.assertTrue(result.data["command_succeeded"])
        self.assertEqual(len(executor.calls), 1)
        spec = executor.calls[0]
        self.assertEqual(spec.argv, ("python3", "-m", "unittest", "discover", "-v"))
        self.assertEqual(spec.working_directory, ".")
        self.assertEqual(spec.network, "none")
        self.assertEqual(spec.execution_id, "command-exec-1")
        self.assertEqual(spec.environment["CODING_AGENT_WORKSPACE"], "/workspace")
        self.assertNotIn("workspace", spec.to_dict())
        self.assertNotIn("environment", spec.to_dict())

    def test_command_schema_allowlist_and_cwd_reject_model_escape(self) -> None:
        executor = FakeSandboxExecutor()
        harness = self.harness(executor)

        unknown_field = harness.execute(
            ToolCall(
                id="command-schema",
                name="run_command",
                arguments={
                    "profile": "python_project",
                    "argv": ["python3"],
                    "command": "python3",
                },
            ),
            self.context(Permission.EXECUTE_COMMAND),
        )
        self.assertIs(unknown_field.status, ToolStatus.INVALID_ARGUMENTS)
        self.assertEqual(unknown_field.error["kind"], "schema_validation")

        denied_executable = harness.execute(
            ToolCall(
                id="command-executable",
                name="run_command",
                arguments={
                    "profile": "python_project",
                    "argv": ["/usr/bin/pytest"],
                },
            ),
            self.context(Permission.EXECUTE_COMMAND),
        )
        self.assertIs(denied_executable.status, ToolStatus.INVALID_ARGUMENTS)
        self.assertEqual(denied_executable.error["kind"], "command_executable_denied")

        escaped_cwd = harness.execute(
            ToolCall(
                id="command-cwd",
                name="run_command",
                arguments={
                    "profile": "python_project",
                    "argv": ["python3"],
                    "cwd": "../outside",
                },
            ),
            self.context(Permission.EXECUTE_COMMAND),
        )
        self.assertIs(escaped_cwd.status, ToolStatus.INVALID_ARGUMENTS)
        self.assertEqual(escaped_cwd.error["kind"], "command_working_directory_invalid")
        self.assertEqual(executor.calls, [])

    def test_network_or_expanded_limits_are_admission_denied(self) -> None:
        profiles = CommandProfileRegistry()
        profiles.register(
            CommandProfile(
                name="network_command",
                executable_allowlist=("python3",),
                network="approved",
            )
        )
        profiles.register(
            CommandProfile(
                name="large_command",
                executable_allowlist=("python3",),
                limits=replace(
                    DEFAULT_COMMAND_LIMITS,
                    memory_bytes=DEFAULT_COMMAND_LIMITS.memory_bytes + 1,
                ),
            )
        )
        executor = FakeSandboxExecutor()
        harness = self.harness(executor, profiles)
        for profile in ("network_command", "large_command"):
            with self.subTest(profile=profile):
                result = harness.execute(
                    ToolCall(
                        id=f"approval-{profile}",
                        name="run_command",
                        arguments={"profile": profile, "argv": ["python3"]},
                    ),
                    self.context(Permission.EXECUTE_COMMAND),
                )
                self.assertIs(result.status, ToolStatus.PERMISSION_DENIED)
                self.assertEqual(result.error["kind"], "command_approval_required")
        self.assertEqual(executor.calls, [])

    def test_nonzero_exit_is_a_structured_observation(self) -> None:
        executor = FakeSandboxExecutor(
            result_factory=lambda spec: ExecutionResult(
                status="exited",
                exit_code=7,
                stdout="command output\n",
                stderr="command failure\n",
                duration_ms=1.0,
                backend_metadata={
                    "backend": "fake_sandbox",
                    "execution_id": spec.execution_id,
                    "cleanup_verified": True,
                },
            )
        )
        result = self.harness(executor).execute(
            ToolCall(
                id="command-failure",
                name="run_command",
                arguments={"profile": "python_project", "argv": ["python3"]},
            ),
            self.context(Permission.EXECUTE_COMMAND),
        )
        self.assertIs(result.status, ToolStatus.SUCCESS)
        self.assertFalse(result.data["command_succeeded"])
        self.assertEqual(result.data["exit_code"], 7)
        self.assertIsNone(result.error)

    @require_native_sandbox
    def test_native_command_uses_direct_argv_and_m4_1_boundary(self) -> None:
        executor = LinuxNamespaceExecutor()
        capabilities = executor.capabilities()
        self.assertTrue(capabilities.available, capabilities.to_dict())
        source_before = tree_fingerprint(self.source)
        marker = self.source.parent / "shell-must-not-create"
        argument = f"$(touch {marker})"
        result = self.harness(executor).execute(
            ToolCall(
                id="command-native",
                name="run_command",
                arguments={
                    "profile": "python_project",
                    "argv": [
                        "python3",
                        "-c",
                        "import sys; print(sys.argv[1])",
                        argument,
                    ],
                },
            ),
            self.context(Permission.EXECUTE_COMMAND, execution_id="command-native-1"),
        )
        self.assertIs(result.status, ToolStatus.SUCCESS, result.error)
        self.assertTrue(result.data["command_succeeded"], result.data)
        self.assertIn(argument, result.data["stdout"])
        self.assertFalse(marker.exists())
        self.assertEqual(tree_fingerprint(self.source), source_before)
        self.assertTrue(result.data["sandbox"]["cleanup_verified"])

    def test_running_command_is_uncertain_and_never_repeated_without_resolution(self) -> None:
        executor = FakeSandboxExecutor()
        application = AgentApplication(
            Path(self.temporary.name) / "persistent-agent-home",
            sandbox_executor=executor,
        )
        script = [
            {
                "tool_calls": [
                    {
                        "id": "command-1",
                        "name": "run_command",
                        "arguments": {
                            "profile": "python_project",
                            "argv": ["python3", "-m", "unittest"],
                        },
                    }
                ]
            },
            {"final": "done"},
        ]

        def crash(stage: str) -> None:
            if stage == "after_tool_running":
                raise ProcessCrash(stage)

        with self.assertRaises(ProcessCrash):
            application.run_task(
                source=self.source,
                task="Run the project command.",
                backend=ScriptedBackend(script),
                fault_injector=crash,
            )
        session_id = str(application.list_sessions()[0]["id"])
        self.assertEqual(executor.calls, [])

        resumed = application.resume_session(session_id, backend=ScriptedBackend(script))
        self.assertIs(resumed.state, RuntimeState.WAITING_APPROVAL)
        row = application.journal.get_tool_call(session_id, "command-1")
        self.assertIsNotNone(row)
        assert row is not None
        self.assertEqual(row["status"], "uncertain")
        self.assertEqual(executor.calls, [])
        self.assertTrue(
            any(
                event.event_type.value == "approval_requested"
                for event in application.journal.list_events(session_id)
            )
        )


if __name__ == "__main__":
    unittest.main()
