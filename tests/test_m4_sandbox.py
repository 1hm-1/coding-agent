from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import sys
import tempfile
import unittest

from coding_agent.application import AgentApplication
from coding_agent.domain import Permission, RuntimeState, ToolCall, ToolStatus
from coding_agent.models.scripted import ScriptedBackend
from coding_agent.sandbox import (
    ExecutionSpec,
    FakeSandboxExecutor,
    FailClosedSandboxExecutor,
    LinuxNamespaceExecutor,
    ResourceLimits,
)
from coding_agent.test_profiles import TestProfile, TestProfileRegistry, default_test_profiles
from coding_agent.tools.base import ToolContext
from coding_agent.tools.builtin import build_builtin_registry
from coding_agent.tools.harness import ToolHarness
from coding_agent.workspace import WorkspaceManager, tree_fingerprint


PYTHON_EXECUTABLE = "/usr/bin/python3" if Path("/usr/bin/python3").is_file() else sys.executable


class SandboxContractTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.source = root / "source"
        self.source.mkdir()
        (self.source / "sample.py").write_text("value = 1\n", encoding="utf-8")
        self.agent_home = root / "agent-home"
        self.manager = WorkspaceManager(self.agent_home)
        self.manager.create(self.source, "sandbox-session")
        self.guard = self.manager.get("sandbox-session")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def context(self, *permissions: Permission, execution_id: str | None = None) -> ToolContext:
        return ToolContext(
            workspace=self.guard,
            allowed_permissions=frozenset(permissions),
            execution_id=execution_id,
        )

    def test_policy_and_fail_closed_executor_never_runs_host_process(self) -> None:
        executor = FailClosedSandboxExecutor("test_backend_missing")
        harness = ToolHarness(
            build_builtin_registry(
                default_test_profiles(),
                sandbox_executor=executor,
            )
        )
        result = harness.execute(
            ToolCall(
                id="sandbox-unavailable",
                name="restricted_test",
                arguments={"profile": "python_unittest"},
            ),
            self.context(Permission.EXECUTE_TEST),
        )
        self.assertIs(result.status, ToolStatus.EXECUTION_ERROR)
        self.assertEqual(result.error["kind"], "sandbox_capability_unavailable")
        self.assertFalse(result.data["passed"])

    def test_fake_executor_receives_profile_only_and_records_safe_spec(self) -> None:
        executor = FakeSandboxExecutor()
        harness = ToolHarness(
            build_builtin_registry(
                default_test_profiles(),
                sandbox_executor=executor,
            )
        )
        result = harness.execute(
            ToolCall(
                id="sandbox-fake",
                name="restricted_test",
                arguments={"profile": "python_unittest"},
            ),
            self.context(Permission.EXECUTE_TEST, execution_id="exec-test-1"),
        )
        self.assertIs(result.status, ToolStatus.SUCCESS)
        self.assertTrue(result.data["passed"])
        self.assertEqual(len(executor.calls), 1)
        spec = executor.calls[0]
        self.assertEqual(spec.execution_id, "exec-test-1")
        self.assertEqual(spec.network, "none")
        self.assertEqual(spec.environment["CODING_AGENT_WORKSPACE"], "/workspace")
        self.assertNotIn("workspace", spec.to_dict())
        self.assertNotIn("environment", spec.to_dict())

    def test_native_namespace_hides_host_paths_secret_and_network(self) -> None:
        executor = LinuxNamespaceExecutor()
        capabilities = executor.capabilities()
        self.assertEqual(
            capabilities.metadata["identity_kind"],
            "native_runtime_sample_fingerprint",
        )
        if not capabilities.available:
            self.assertFalse(
                executor.execute.__name__ == "host_subprocess",
                "unavailable backend must not silently become host execution",
            )
            return
        source_before = tree_fingerprint(self.source)
        canary = self.agent_home / "host-canary.txt"
        canary.write_text("do-not-touch\n", encoding="utf-8")
        secret_name = "M4_TEST_SECRET"
        old_secret = os.environ.get(secret_name)
        os.environ[secret_name] = "host-secret-value"
        try:
            source_literal = repr(str(self.source))
            canary_literal = repr(str(canary))
            outside_link = self.guard.root / "outside-link"
            outside_link.symlink_to(canary)
            script = (
                "import os, pathlib, socket\n"
                f"hidden_source = not pathlib.Path({source_literal}).exists()\n"
                f"hidden_agent = not pathlib.Path({canary_literal}).exists()\n"
                f"hidden_secret = os.environ.get({secret_name!r}) is None\n"
                "outside_write_blocked = False\n"
                "try:\n"
                f"    pathlib.Path({canary_literal}).write_text('tampered')\n"
                "except OSError:\n"
                "    outside_write_blocked = True\n"
                "symlink_escape_blocked = False\n"
                "try:\n"
                "    pathlib.Path('/workspace/outside-link').read_text()\n"
                "except OSError:\n"
                "    symlink_escape_blocked = True\n"
                "interfaces = [line.split(':', 1)[0].strip() for line in "
                "pathlib.Path('/proc/net/dev').read_text().splitlines() if ':' in line]\n"
                "network_hidden = all(name == 'lo' for name in interfaces)\n"
                "for operation in (\n"
                "    lambda: socket.getaddrinfo('example.com', 80),\n"
                "    lambda: socket.create_connection(('1.1.1.1', 80), timeout=0.2),\n"
                "):\n"
                "    try:\n"
                "        operation()\n"
                "        network_hidden = False\n"
                "    except OSError:\n"
                "        pass\n"
                "root_read_only = False\n"
                "try:\n"
                "    pathlib.Path('/m4-root-write').write_text('blocked')\n"
                "except OSError:\n"
                "    root_read_only = True\n"
                "system_read_only = False\n"
                "try:\n"
                "    pathlib.Path('/usr/bin/m4-system-write').write_text('blocked')\n"
                "except OSError:\n"
                "    system_read_only = True\n"
                "pathlib.Path('/workspace/sandbox-marker.txt').write_text('workspace')\n"
                "network_hidden = network_hidden and not pathlib.Path('/dev/sda').exists()\n"
                "raise SystemExit(0 if all((hidden_source, hidden_agent, hidden_secret, "
                "outside_write_blocked, symlink_escape_blocked, network_hidden, "
                "root_read_only, system_read_only)) else 1)\n"
            )
            profiles = TestProfileRegistry()
            profiles.register(
                TestProfile(
                    name="namespace_attack",
                    argv=(PYTHON_EXECUTABLE, "-c", script),
                    timeout_seconds=5.0,
                    limits=ResourceLimits(
                        wall_seconds=5.0,
                        cpu_seconds=2.0,
                        memory_bytes=256 * 1024 * 1024,
                        writable_bytes=8 * 1024 * 1024,
                        pids=32,
                        stdout_bytes=4096,
                        stderr_bytes=4096,
                    ),
                )
            )
            harness = ToolHarness(
                build_builtin_registry(profiles, sandbox_executor=executor)
            )
            result = harness.execute(
                ToolCall(
                    id="sandbox-attack",
                    name="restricted_test",
                    arguments={"profile": "namespace_attack"},
                ),
                self.context(Permission.EXECUTE_TEST, execution_id="exec-attack"),
            )
        finally:
            if old_secret is None:
                os.environ.pop(secret_name, None)
            else:
                os.environ[secret_name] = old_secret
        self.assertIs(result.status, ToolStatus.SUCCESS, result.error)
        self.assertTrue(result.data["passed"], result.data)
        self.assertEqual((self.guard.root / "sandbox-marker.txt").read_text(), "workspace")
        self.assertEqual(canary.read_text(encoding="utf-8"), "do-not-touch\n")
        self.assertEqual(tree_fingerprint(self.source), source_before)
        sandbox = result.data["sandbox"]
        self.assertTrue(sandbox["rootfs_read_only"])
        self.assertEqual(sandbox["network"], "none")
        self.assertRegex(sandbox["image_digest"], r"^sha256:[0-9a-f]{64}$")
        self.assertTrue(sandbox["cleanup_verified"])

    def test_native_output_limit_is_bounded(self) -> None:
        executor = LinuxNamespaceExecutor()
        if not executor.capabilities().available:
            result = executor.execute(
                # The policy is intentionally not bypassed; this branch only
                # proves unavailable environments remain fail closed.
                self._spec_for_probe(execution_id="output-unavailable")
            )
            self.assertEqual(result.status, "sandbox_error")
            return
        profiles = TestProfileRegistry()
        profiles.register(
            TestProfile(
                name="large_output",
                argv=(PYTHON_EXECUTABLE, "-c", "print('x' * 100000)"),
                timeout_seconds=5.0,
                limits=ResourceLimits(
                    wall_seconds=5.0,
                    cpu_seconds=2.0,
                    memory_bytes=256 * 1024 * 1024,
                    writable_bytes=8 * 1024 * 1024,
                    pids=32,
                    stdout_bytes=1024,
                    stderr_bytes=1024,
                ),
            )
        )
        result = ToolHarness(
            build_builtin_registry(profiles, sandbox_executor=executor)
        ).execute(
            ToolCall(
                id="sandbox-output",
                name="restricted_test",
                arguments={"profile": "large_output"},
            ),
            self.context(Permission.EXECUTE_TEST),
        )
        self.assertIn(result.status, {ToolStatus.EXECUTION_ERROR, ToolStatus.SUCCESS})
        self.assertLessEqual(len(result.data["stdout"]), 1024)

    def test_native_resource_limits_and_process_cleanup(self) -> None:
        executor = LinuxNamespaceExecutor()
        if not executor.capabilities().available:
            self.assertFalse(executor.capabilities().available)
            return

        def run_profile(
            name: str,
            code: str,
            limits: ResourceLimits,
            *,
            timeout_seconds: float,
        ):
            profiles = TestProfileRegistry()
            profiles.register(
                TestProfile(
                    name=name,
                    argv=(PYTHON_EXECUTABLE, "-c", code),
                    timeout_seconds=timeout_seconds,
                    limits=limits,
                )
            )
            return ToolHarness(
                build_builtin_registry(profiles, sandbox_executor=executor)
            ).execute(
                ToolCall(
                    id=f"sandbox-{name}",
                    name="restricted_test",
                    arguments={"profile": name},
                ),
                self.context(Permission.EXECUTE_TEST, execution_id=f"exec-{name}"),
            )

        wall = run_profile(
            "wall_limit",
            "import time; time.sleep(10)",
            ResourceLimits(
                wall_seconds=0.15,
                cpu_seconds=2.0,
                memory_bytes=128 * 1024 * 1024,
                writable_bytes=4 * 1024 * 1024,
                pids=16,
                stdout_bytes=1024,
                stderr_bytes=1024,
            ),
            timeout_seconds=0.5,
        )
        self.assertIs(wall.status, ToolStatus.TIMEOUT)
        self.assertEqual(wall.data["limit_hit"], "wall_seconds")
        self.assertTrue(wall.data["sandbox"]["cleanup_verified"])

        cpu = run_profile(
            "cpu_limit",
            "while True: pass",
            ResourceLimits(
                wall_seconds=4.0,
                cpu_seconds=1.0,
                memory_bytes=128 * 1024 * 1024,
                writable_bytes=4 * 1024 * 1024,
                pids=16,
                stdout_bytes=1024,
                stderr_bytes=1024,
            ),
            timeout_seconds=5.0,
        )
        self.assertIs(cpu.status, ToolStatus.EXECUTION_ERROR)
        self.assertEqual(cpu.error["kind"], "sandbox_resource_exhausted")
        self.assertEqual(cpu.data["limit_hit"], "cpu_seconds")

        memory = run_profile(
            "memory_limit",
            "chunks = []\n"
            "while True:\n"
            "    chunks.append(bytearray(8 * 1024 * 1024))\n",
            ResourceLimits(
                wall_seconds=3.0,
                cpu_seconds=2.0,
                memory_bytes=64 * 1024 * 1024,
                writable_bytes=4 * 1024 * 1024,
                pids=16,
                stdout_bytes=1024,
                stderr_bytes=4096,
            ),
            timeout_seconds=4.0,
        )
        self.assertIs(memory.status, ToolStatus.EXECUTION_ERROR)
        self.assertEqual(memory.error["kind"], "sandbox_resource_exhausted")
        self.assertEqual(memory.data["limit_hit"], "memory_bytes")

        storage = run_profile(
            "storage_limit",
            "import pathlib; pathlib.Path('/workspace/blob').write_bytes(b'x' * 2000000)",
            ResourceLimits(
                wall_seconds=2.0,
                cpu_seconds=1.0,
                memory_bytes=128 * 1024 * 1024,
                writable_bytes=1024 * 1024,
                pids=16,
                stdout_bytes=1024,
                stderr_bytes=1024,
            ),
            timeout_seconds=3.0,
        )
        self.assertIs(storage.status, ToolStatus.EXECUTION_ERROR)
        self.assertEqual(storage.error["kind"], "sandbox_resource_exhausted")
        self.assertEqual(storage.data["limit_hit"], "writable_bytes")

        pids = run_profile(
            "pid_limit",
            "import os, time\n"
            "for _ in range(100):\n"
            "    try:\n"
            "        child = os.fork()\n"
            "    except OSError:\n"
            "        time.sleep(1)\n"
            "        break\n"
            "    if child == 0:\n"
            "        time.sleep(10)\n"
            "        os._exit(0)\n"
            "time.sleep(10)\n",
            ResourceLimits(
                wall_seconds=2.0,
                cpu_seconds=1.0,
                memory_bytes=128 * 1024 * 1024,
                writable_bytes=4 * 1024 * 1024,
                pids=8,
                stdout_bytes=1024,
                stderr_bytes=1024,
            ),
            timeout_seconds=3.0,
        )
        self.assertIs(pids.status, ToolStatus.EXECUTION_ERROR)
        self.assertEqual(pids.error["kind"], "sandbox_resource_exhausted")
        self.assertEqual(pids.data["limit_hit"], "pids")
        self.assertTrue(pids.data["sandbox"]["cleanup_verified"])

        child_ignores_term = run_profile(
            "child_cleanup",
            "import os, signal, time\n"
            "child = os.fork()\n"
            "if child == 0:\n"
            "    signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
            "    time.sleep(10)\n"
            "    os._exit(0)\n"
            "time.sleep(10)\n",
            ResourceLimits(
                wall_seconds=0.15,
                cpu_seconds=2.0,
                memory_bytes=128 * 1024 * 1024,
                writable_bytes=4 * 1024 * 1024,
                pids=16,
                stdout_bytes=1024,
                stderr_bytes=1024,
            ),
            timeout_seconds=0.5,
        )
        self.assertIs(child_ignores_term.status, ToolStatus.TIMEOUT)
        self.assertEqual(child_ignores_term.data["limit_hit"], "wall_seconds")
        self.assertTrue(child_ignores_term.data["sandbox"]["cleanup_verified"])

    def test_parallel_sessions_have_separate_mounts_and_output(self) -> None:
        executor = LinuxNamespaceExecutor()
        capabilities = executor.capabilities()
        if not capabilities.available:
            self.assertFalse(capabilities.available)
            return
        second_source = self.source.parent / "source-b"
        second_source.mkdir()
        (second_source / "sample.py").write_text("value = 2\n", encoding="utf-8")
        self.manager.create(second_source, "sandbox-session-b")
        second_guard = self.manager.get("sandbox-session-b")
        digest = str(capabilities.metadata["image_digest"])

        def execute(guard, token: str):
            spec = ExecutionSpec(
                argv=(
                    PYTHON_EXECUTABLE,
                    "-c",
                    "import pathlib; "
                    f"pathlib.Path('/workspace/{token}.txt').write_text('{token}'); "
                    f"print('{token}')",
                ),
                workspace=guard.root,
                working_directory=".",
                environment={
                    "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
                    "LANG": "C.UTF-8",
                    "LC_ALL": "C.UTF-8",
                    "PYTHONPATH": "",
                    "HOME": "/tmp/home",
                    "CODING_AGENT_WORKSPACE": "/workspace",
                    "PWD": "/workspace",
                },
                network="none",
                limits=ResourceLimits(
                    wall_seconds=2.0,
                    cpu_seconds=1.0,
                    memory_bytes=128 * 1024 * 1024,
                    writable_bytes=4 * 1024 * 1024,
                    pids=16,
                    stdout_bytes=1024,
                    stderr_bytes=1024,
                ),
                profile_name=f"parallel_{token}",
                image_digest=digest,
                execution_id=f"parallel-{token}",
            )
            return executor.execute(spec)

        with ThreadPoolExecutor(max_workers=2) as pool:
            first, second = pool.map(
                lambda item: execute(*item),
                ((self.guard, "first"), (second_guard, "second")),
            )
        self.assertEqual(first.status, "exited")
        self.assertEqual(second.status, "exited")
        self.assertEqual(first.stdout.strip(), "first")
        self.assertEqual(second.stdout.strip(), "second")
        self.assertEqual((self.guard.root / "first.txt").read_text(), "first")
        self.assertFalse((self.guard.root / "second.txt").exists())
        self.assertEqual((second_guard.root / "second.txt").read_text(), "second")
        self.assertFalse((second_guard.root / "first.txt").exists())
        self.assertTrue(first.backend_metadata["cleanup_verified"])
        self.assertTrue(second.backend_metadata["cleanup_verified"])

    def test_application_recovery_restarts_only_sandbox_observation(self) -> None:
        class SimulatedProcessCrash(BaseException):
            pass

        executor = FakeSandboxExecutor()
        root = Path(self.temporary.name)
        application = AgentApplication(root / "recovery-agent", sandbox_executor=executor)
        backend = ScriptedBackend(
            [
                {
                    "tool_calls": [
                        {
                            "id": "test-1",
                            "name": "restricted_test",
                            "arguments": {"profile": "python_unittest"},
                        }
                    ]
                },
                {"final": "tests observed"},
            ]
        )
        def crash(stage: str) -> None:
            if stage == "after_tool_running":
                raise SimulatedProcessCrash("crash")

        with self.assertRaises(SimulatedProcessCrash):
            application.run_task(
                source=self.source,
                task="Run the tests and answer.",
                backend=backend,
                fault_injector=crash,
            )
        session_id = application.list_sessions()[-1]["id"]
        resumed = application.resume_session(
            str(session_id),
            backend=ScriptedBackend(
                [
                    {
                        "tool_calls": [
                            {
                                "id": "test-1",
                                "name": "restricted_test",
                                "arguments": {"profile": "python_unittest"},
                            }
                        ]
                    },
                    {"final": "tests observed"},
                ]
            ),
        )
        self.assertIs(resumed.state, RuntimeState.COMPLETED)
        self.assertEqual(len(executor.calls), 1)
        self.assertEqual(
            [event.payload.get("attempt") for event in application.journal.list_events(str(session_id))
             if event.event_type.value == "tool_call_running"],
            [1, 2],
        )
        finished = [
            event
            for event in application.journal.list_events(str(session_id))
            if event.event_type.value == "tool_call_finished"
        ]
        self.assertEqual(
            finished[-1].payload["result"]["data"]["sandbox"]["image_digest"],
            "sha256:" + "0" * 64,
        )

    def _spec_for_probe(self, *, execution_id: str):
        capabilities = LinuxNamespaceExecutor().capabilities()
        return ExecutionSpec(
            argv=(PYTHON_EXECUTABLE, "-c", "pass"),
            workspace=self.guard.root,
            working_directory=".",
            environment={"PATH": "/usr/bin"},
            network="none",
            limits=ResourceLimits(
                wall_seconds=1.0,
                cpu_seconds=1.0,
                memory_bytes=64 * 1024 * 1024,
                writable_bytes=1024 * 1024,
                pids=8,
                stdout_bytes=1024,
                stderr_bytes=1024,
            ),
            profile_name="probe",
            image_digest=capabilities.metadata.get("image_digest"),
            execution_id=execution_id,
        )


if __name__ == "__main__":
    unittest.main()
