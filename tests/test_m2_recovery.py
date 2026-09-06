from __future__ import annotations

import tempfile
import unittest
import os
import signal
from pathlib import Path

from coding_agent.application import AgentApplication
from coding_agent.domain import (
    BackendError,
    EventType,
    Message,
    RunPolicy,
    RuntimeSnapshot,
    RuntimeState,
    Session,
    ToolResult,
    ToolStatus,
)
from coding_agent.models.scripted import ScriptedBackend
from coding_agent.models.fallback import FallbackBackend
from coding_agent.persistence import (
    JournalMutation,
    LeaseConflict,
    ResumeRejected,
    SQLiteRunJournal,
)
from coding_agent.workspace import tree_fingerprint


class ProcessCrash(BaseException):
    pass


class RecoveryTest(unittest.TestCase):
    def _source(self, root: Path, content: str = "value\n") -> Path:
        source = root / "source"
        source.mkdir()
        (source / "value.txt").write_text(content, encoding="utf-8")
        return source

    def _read_script(self) -> list[dict[str, object]]:
        return [
            {
                "tool_calls": [
                    {
                        "id": "read-1",
                        "name": "read_file",
                        "arguments": {"path": "value.txt"},
                    }
                ]
            },
            {"final": "verified"},
        ]

    def test_saved_model_response_is_reused_after_interrupt(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = self._source(root)
            application = AgentApplication(root / "agent-home")

            class InterruptAfterResponse(ScriptedBackend):
                def complete(self, request):
                    response = super().complete(request)
                    application.interrupt_session(request.metadata["session_id"])
                    return response

            first = application.run_task(
                source=source,
                task="Read the value.",
                backend=InterruptAfterResponse(self._read_script()),
            )
            self.assertIs(first.state, RuntimeState.INTERRUPTED)
            self.assertIs(
                application.journal.load_snapshot(first.session_id).resume_target_state,
                RuntimeState.CALLING_MODEL,
            )

            resumed_backend = ScriptedBackend(self._read_script())
            resumed = application.resume_session(first.session_id, backend=resumed_backend)
            self.assertIs(resumed.state, RuntimeState.COMPLETED)
            self.assertEqual(resumed.final_answer, "verified")
            self.assertEqual(resumed_backend.requests.__len__(), 1)
            self.assertEqual(resumed.model_calls, 2)

    def test_model_message_crash_reuses_response_without_duplicate_message(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = self._source(root)
            application = AgentApplication(root / "agent-home")

            def crash(stage: str) -> None:
                if stage == "after_model_message":
                    raise ProcessCrash(stage)

            with self.assertRaises(ProcessCrash):
                application.run_task(
                    source=source,
                    task="Answer.",
                    backend=ScriptedBackend([{"final": "verified"}]),
                    fault_injector=crash,
                )
            session_id = str(application.list_sessions()[0]["id"])
            application.close()
            resumed_application = AgentApplication(root / "agent-home")
            resumed_backend = ScriptedBackend([{"final": "verified"}])
            resumed = resumed_application.resume_session(session_id, backend=resumed_backend)

            self.assertIs(resumed.state, RuntimeState.COMPLETED)
            self.assertEqual(resumed_backend.requests, [])
            messages = resumed_application.journal.list_messages(session_id)
            self.assertEqual(
                len([message for message in messages if message.role == "assistant"]),
                1,
            )

    def test_sigint_persists_interrupted_before_run_returns(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = self._source(root)
            application = AgentApplication(root / "agent-home")

            class SignalInterruptBackend(ScriptedBackend):
                def complete(self, request):
                    response = super().complete(request)
                    os.kill(os.getpid(), signal.SIGINT)
                    return response

            first = application.run_task(
                source=source,
                task="Answer.",
                backend=SignalInterruptBackend([{"final": "verified"}]),
            )
            self.assertIs(first.state, RuntimeState.INTERRUPTED)
            events = application.journal.list_events(first.session_id)
            interrupted_index = next(
                index
                for index, event in enumerate(events)
                if event.event_type is EventType.STATE_TRANSITION
                and event.payload.get("to") == RuntimeState.INTERRUPTED.value
            )
            self.assertEqual(events[interrupted_index].state, RuntimeState.INTERRUPTED)
            self.assertIsNone(
                application.journal.load_snapshot(first.session_id).interrupt_requested_at
            )

    def test_provider_tool_ids_can_repeat_across_sessions(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = self._source(root)
            application = AgentApplication(root / "agent-home")

            first = application.run_task(
                source=source,
                task="Read once.",
                backend=ScriptedBackend(self._read_script()),
            )
            second = application.run_task(
                source=source,
                task="Read again.",
                backend=ScriptedBackend(self._read_script()),
            )

            self.assertIs(first.state, RuntimeState.COMPLETED)
            self.assertIs(second.state, RuntimeState.COMPLETED)
            self.assertEqual(application.journal.list_tool_calls(first.session_id)[0]["call_id"], "read-1")
            self.assertEqual(application.journal.list_tool_calls(second.session_id)[0]["call_id"], "read-1")

    def test_tool_crash_windows_resume_without_duplicate_logical_call(self) -> None:
        stages = (
            "after_tool_prepare",
            "after_tool_running",
            "after_tool_result",
            "after_tool_observation",
        )
        for stage in stages:
            with self.subTest(stage=stage), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                source = self._source(root)
                application = AgentApplication(root / "agent-home")

                def crash(current: str, target: str = stage) -> None:
                    if current == target:
                        raise ProcessCrash(target)

                with self.assertRaises(ProcessCrash):
                    application.run_task(
                        source=source,
                        task="Read the value.",
                        backend=ScriptedBackend(self._read_script()),
                        fault_injector=crash,
                    )
                session_id = str(application.list_sessions()[0]["id"])
                resumed = application.resume_session(
                    session_id,
                    backend=ScriptedBackend(self._read_script()),
                )
                self.assertIs(resumed.state, RuntimeState.COMPLETED)
                self.assertEqual(resumed.tool_calls, 1)
                events = application.journal.list_events(session_id)
                self.assertEqual(
                    len([event for event in events if event.event_type is EventType.TOOL_CALL_FINISHED]),
                    1,
                )

    def test_search_read_only_result_is_reused_after_crash(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = self._source(root, 'RULE_CODE = "EAST"\n')
            application = AgentApplication(root / "agent-home")
            script = [
                {
                    "tool_calls": [
                        {
                            "id": "search-1",
                            "name": "search_files",
                            "arguments": {"query": "EAST"},
                        }
                    ]
                },
                {"final": "located"},
            ]

            def crash(stage: str) -> None:
                if stage == "after_tool_result":
                    raise ProcessCrash(stage)

            with self.assertRaises(ProcessCrash):
                application.run_task(
                    source=source,
                    task="Locate EAST.",
                    backend=ScriptedBackend(script),
                    fault_injector=crash,
                )
            session_id = str(application.list_sessions()[0]["id"])
            resumed = application.resume_session(
                session_id,
                backend=ScriptedBackend(script),
            )
            self.assertIs(resumed.state, RuntimeState.COMPLETED)
            calls = application.journal.list_tool_calls(session_id)
            self.assertEqual(len(calls), 1)
            self.assertEqual(calls[0]["tool_name"], "search_files")
            self.assertEqual(calls[0]["attempt"], 1)

    def test_model_running_crash_is_marked_uncertain_before_retry(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = self._source(root)
            application = AgentApplication(root / "agent-home")

            def crash(stage: str) -> None:
                if stage == "after_model_start":
                    raise ProcessCrash(stage)

            with self.assertRaises(ProcessCrash):
                application.run_task(
                    source=source,
                    task="Answer.",
                    backend=ScriptedBackend([{"final": "verified"}]),
                    fault_injector=crash,
                    policy=RunPolicy(retry_base_delay_seconds=0.0),
                )
            session_id = str(application.list_sessions()[0]["id"])
            resumed = application.resume_session(
                session_id,
                backend=ScriptedBackend([{"final": "verified"}]),
            )
            self.assertIs(resumed.state, RuntimeState.COMPLETED)
            self.assertEqual(resumed.model_calls, 2)
            self.assertIn(
                EventType.MODEL_CALL_UNCERTAIN,
                [event.event_type for event in application.journal.list_events(session_id)],
            )

    def test_unknown_edit_effect_requires_explicit_resolution(self) -> None:
        resolutions = ("effect-not-applied", "effect-applied", "abort")
        for resolution in resolutions:
            with self.subTest(resolution=resolution), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                source = self._source(root, "old\n")
                application = AgentApplication(root / "agent-home")
                base_harness = application.harness

                class CrashAfterEdit:
                    def __init__(self):
                        self.crashed = False

                    def prepare(self, *args, **kwargs):
                        return base_harness.prepare(*args, **kwargs)

                    def current_revision(self, *args, **kwargs):
                        return base_harness.current_revision(*args, **kwargs)

                    def execute(self, call, context):
                        result = base_harness.execute(call, context)
                        if call.name == "edit_file" and not self.crashed:
                            self.crashed = True
                            raise ProcessCrash("after-edit-effect")
                        return result

                application.harness = CrashAfterEdit()
                script = [
                    {
                        "tool_calls": [
                            {
                                "id": "edit-1",
                                "name": "edit_file",
                                "arguments": {
                                    "path": "value.txt",
                                    "old_text": "old",
                                    "new_text": "new",
                                },
                            }
                        ]
                    },
                    {"final": "done"},
                ]
                with self.assertRaises(ProcessCrash):
                    application.run_task(source=source, task="Edit.", backend=ScriptedBackend(script))
                session_id = str(application.list_sessions()[0]["id"])
                workspace_file = (
                    root / "agent-home" / "workspaces" / session_id / "repo" / "value.txt"
                )
                workspace_file.write_text("external\n", encoding="utf-8")
                waiting = application.resume_session(
                    session_id,
                    backend=ScriptedBackend(script),
                )
                self.assertIs(waiting.state, RuntimeState.WAITING_APPROVAL)
                self.assertEqual(
                    application.journal.get_tool_call(session_id, "edit-1")["status"],
                    "uncertain",
                )
                if resolution == "effect-applied":
                    resolved_result = ToolResult(
                        call_id="edit-1",
                        tool_name="edit_file",
                        status=ToolStatus.SUCCESS,
                        data={"operator_confirmed": True},
                    )
                    application.resolve_call(
                        session_id,
                        "edit-1",
                        resolution,
                        result=resolved_result,
                    )
                    final = application.resume_session(
                        session_id,
                        backend=ScriptedBackend(script),
                    )
                    self.assertIs(final.state, RuntimeState.COMPLETED)
                elif resolution == "effect-not-applied":
                    application.resolve_call(session_id, "edit-1", resolution)
                    final = application.resume_session(
                        session_id,
                        backend=ScriptedBackend(script),
                    )
                    self.assertIs(final.state, RuntimeState.COMPLETED)
                else:
                    final_snapshot = application.resolve_call(session_id, "edit-1", resolution)
                    self.assertIs(final_snapshot.state, RuntimeState.FAILED)
                    self.assertIs(
                        application.replay_session(session_id).final_state,
                        RuntimeState.FAILED,
                    )

    def test_lease_takeover_rejects_old_owner_writes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            now = [100.0]
            journal = SQLiteRunJournal(root / "state.db", lease_clock=lambda: now[0])
            session = Session(
                id="lease-session",
                task="task",
                source_path=str(root),
                state=RuntimeState.CREATED,
                policy=RunPolicy(),
                source_fingerprint=tree_fingerprint(root),
            )
            journal.create_session(session.to_snapshot(), Message(role="user", content="task"))
            journal.acquire_lease(session.id, "owner-a", lease_seconds=5)
            with self.assertRaises(LeaseConflict):
                journal.acquire_lease(session.id, "owner-b", lease_seconds=5)
            now[0] = 106.0
            journal.acquire_lease(session.id, "owner-b", lease_seconds=5)
            with self.assertRaises(LeaseConflict):
                journal.commit(
                    JournalMutation(
                        session_id=session.id,
                        expected_version=0,
                        expected_state=RuntimeState.CREATED,
                        snapshot_after=RuntimeSnapshot.from_session(
                            session, state=RuntimeState.PREPARING_WORKSPACE
                        ),
                        event_type=EventType.STATE_TRANSITION,
                        payload={"from": "created", "to": "preparing_workspace"},
                        lease_owner="owner-a",
                    )
                )

    def test_resume_rejects_changed_source_or_missing_workspace(self) -> None:
        for mutate in ("source", "workspace"):
            with self.subTest(mutate=mutate), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                source = self._source(root)
                application = AgentApplication(root / "agent-home")

                def crash(stage: str) -> None:
                    if stage == "after_model_response":
                        raise ProcessCrash(stage)

                with self.assertRaises(ProcessCrash):
                    application.run_task(
                        source=source,
                        task="Read the value.",
                        backend=ScriptedBackend(self._read_script()),
                        fault_injector=crash,
                    )
                session_id = str(application.list_sessions()[0]["id"])
                snapshot = application.journal.load_snapshot(session_id)
                if mutate == "source":
                    (source / "value.txt").write_text("changed\n", encoding="utf-8")
                else:
                    Path(snapshot.workspace_path).rename(Path(snapshot.workspace_path).parent / "removed")
                with self.assertRaises(ResumeRejected):
                    application.resume_session(
                        session_id,
                        backend=ScriptedBackend(self._read_script()),
                    )

    def test_retry_and_fallback_are_bounded_and_persisted(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = self._source(root)
            application = AgentApplication(root / "agent-home")

            class Unavailable:
                name = "primary"

                def complete(self, request):
                    raise BackendError("temporarily unavailable", kind="provider_unavailable")

            fallback = FallbackBackend([Unavailable(), ScriptedBackend([{"final": "done"}])])
            now = [0.0]

            def advance(seconds: float) -> None:
                now[0] += seconds

            result = application.run_task(
                source=source,
                task="Answer.",
                backend=fallback,
                policy=RunPolicy(
                    max_retries=1,
                    retry_base_delay_seconds=1.0,
                    retry_max_delay_seconds=2.0,
                ),
                clock=lambda: now[0],
                sleeper=advance,
            )
            self.assertIs(result.state, RuntimeState.COMPLETED)
            self.assertEqual(result.model_calls, 2)
            event_types = [
                event.event_type for event in application.journal.list_events(result.session_id)
            ]
            self.assertIn(EventType.RETRY_SCHEDULED, event_types)
            self.assertIn(EventType.FALLBACK_SELECTED, event_types)
            self.assertEqual(
                application.journal.list_model_calls(result.session_id)[0]["attempt"],
                2,
            )

    def test_non_retryable_backend_error_does_not_enter_retry_wait(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = self._source(root)
            application = AgentApplication(root / "agent-home")

            class Invalid:
                name = "invalid"

                def complete(self, request):
                    raise BackendError("bad request", kind="invalid_request")

            result = application.run_task(source=source, task="Answer.", backend=Invalid())
            self.assertIs(result.state, RuntimeState.FAILED)
            self.assertEqual(result.failure["kind"], "invalid_request")
            event_types = [
                event.event_type for event in application.journal.list_events(result.session_id)
            ]
            self.assertNotIn(EventType.RETRY_SCHEDULED, event_types)


if __name__ == "__main__":
    unittest.main()
