from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from coding_agent.application import AgentApplication
from coding_agent.domain import (
    EventType,
    Message,
    Permission,
    RunPolicy,
    RuntimeSnapshot,
    RuntimeState,
    Session,
    ToolCall,
    ToolResult,
    ToolStatus,
)
from coding_agent.export import export_trace
from coding_agent.migrations import FutureSchemaVersion
from coding_agent.persistence import (
    JournalConflict,
    JournalMutation,
    SQLiteRunJournal,
)
from coding_agent.models.scripted import ScriptedBackend
from coding_agent.trajectory import JsonlEventStore, replay


class PersistenceFoundationTest(unittest.TestCase):
    def snapshot(self, session_id: str = "persistence-session") -> RuntimeSnapshot:
        session = Session(
            id=session_id,
            task="persist this task",
            source_path="/tmp/source-repository",
            state=RuntimeState.CREATED,
            policy=RunPolicy(
                max_steps=4,
                max_model_calls=2,
                max_tool_calls=3,
                max_output_tokens=128,
                allowed_permissions=frozenset({Permission.READ, Permission.WRITE}),
            ),
            source_fingerprint="source-hash",
            created_at="2026-09-04T00:00:00+00:00",
            updated_at="2026-09-04T00:00:01+00:00",
        )
        return replace(
            session.to_snapshot(),
            workspace_path="/tmp/agent/workspaces/persistence-session/repo",
            pending_tool_calls=(
                ToolCall(
                    id="pending-1",
                    name="read_file",
                    arguments={"path": "value.txt"},
                ),
            ),
            active_tool_result=ToolResult(
                call_id="finished-1",
                tool_name="read_file",
                status=ToolStatus.SUCCESS,
                data={"content": "value"},
            ),
            final_answer="done",
            failure={"kind": "example", "message": "kept as structured JSON"},
            step_count=2,
            model_calls=1,
            tool_calls=1,
        )

    def create(self, journal: SQLiteRunJournal, session_id: str = "persistence-session"):
        snapshot = self.snapshot(session_id)
        initial = Message(
            role="user",
            content=snapshot.task,
            metadata={"unicode": "你好", "nested": {"ok": True}},
        )
        journal.create_session(snapshot, initial)
        return snapshot, initial

    def test_migration_is_fresh_idempotent_and_configured(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            db_path = Path(temporary) / "state.db"
            journal = SQLiteRunJournal(db_path)
            self.assertEqual(journal.schema_version, 3)
            self.assertEqual(journal.connection.execute("PRAGMA foreign_keys").fetchone()[0], 1)
            self.assertEqual(
                journal.connection.execute("PRAGMA journal_mode").fetchone()[0].lower(),
                "wal",
            )
            self.assertEqual(
                journal.connection.execute("PRAGMA busy_timeout").fetchone()[0],
                5000,
            )
            first_rows = journal.connection.execute(
                "SELECT version, applied_at FROM schema_migrations"
            ).fetchall()
            journal.close()

            reopened = SQLiteRunJournal(db_path)
            second_rows = reopened.connection.execute(
                "SELECT version, applied_at FROM schema_migrations"
            ).fetchall()
            self.assertEqual(first_rows, second_rows)
            self.assertEqual(
                {
                    row["name"]
                    for row in reopened.connection.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    )
                },
                {
                    "schema_migrations",
                    "sessions",
                    "messages",
                    "events",
                    "checkpoints",
                    "model_calls",
                    "tool_calls",
                    "summaries",
                    "sqlite_sequence",
                },
            )
            reopened.close()

    def test_unknown_future_schema_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            db_path = Path(temporary) / "state.db"
            connection = sqlite3.connect(db_path)
            connection.execute(
                "CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
            )
            connection.execute(
                "INSERT INTO schema_migrations(version, applied_at) VALUES (99, 'future')"
            )
            connection.commit()
            connection.close()
            with self.assertRaises(FutureSchemaVersion):
                SQLiteRunJournal(db_path)

    def test_snapshot_and_session_message_event_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            journal = SQLiteRunJournal(Path(temporary) / "state.db")
            snapshot, initial = self.create(journal)

            self.assertEqual(journal.load_snapshot(snapshot.session_id), snapshot)
            self.assertEqual(journal.list_messages(snapshot.session_id), [initial])
            self.assertEqual(
                [event.sequence for event in journal.list_events(snapshot.session_id)],
                [1, 2],
            )
            self.assertEqual(
                RuntimeSnapshot.from_json(snapshot.to_json()),
                snapshot,
            )
            unknown_version = json.loads(snapshot.to_json())
            unknown_version["snapshot_version"] = 999
            with self.assertRaises(ValueError):
                RuntimeSnapshot.from_dict(unknown_version)
            journal.close()

    def test_atomic_transition_and_message_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            journal = SQLiteRunJournal(Path(temporary) / "state.db")
            snapshot, initial = self.create(journal)
            transition_snapshot = replace(
                snapshot,
                state=RuntimeState.PREPARING_WORKSPACE,
                version=0,
            )
            transition = journal.commit(
                JournalMutation(
                    session_id=snapshot.session_id,
                    expected_version=0,
                    expected_state=RuntimeState.CREATED,
                    snapshot_after=transition_snapshot,
                    event_type=EventType.STATE_TRANSITION,
                    payload={
                        "from": RuntimeState.CREATED.value,
                        "to": RuntimeState.PREPARING_WORKSPACE.value,
                        "reason": "test",
                    },
                )
            )
            self.assertEqual(transition.event.sequence, 3)
            self.assertEqual(transition.committed_version, 1)
            loaded = journal.load_snapshot(snapshot.session_id)
            self.assertEqual(loaded.state, RuntimeState.PREPARING_WORKSPACE)
            self.assertEqual(loaded.version, 1)

            message = Message(role="assistant", content="persisted")
            message_snapshot = replace(loaded, version=1)
            message_result = journal.commit(
                JournalMutation(
                    session_id=snapshot.session_id,
                    expected_version=1,
                    expected_state=RuntimeState.PREPARING_WORKSPACE,
                    snapshot_after=message_snapshot,
                    event_type=EventType.MESSAGE_ADDED,
                    payload={"message": message.to_dict(), "message_index": 1},
                    message_to_append=message,
                )
            )
            self.assertEqual(message_result.event.sequence, 4)
            self.assertEqual(
                [message.content for message in journal.list_messages(snapshot.session_id)],
                [initial.content, "persisted"],
            )
            self.assertEqual(journal.load_snapshot(snapshot.session_id).version, 2)

    def test_expected_state_and_version_conflict_does_not_write(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            journal = SQLiteRunJournal(Path(temporary) / "state.db")
            snapshot, _ = self.create(journal)
            candidate = replace(snapshot, state=RuntimeState.PREPARING_WORKSPACE)
            journal.commit(
                JournalMutation(
                    session_id=snapshot.session_id,
                    expected_version=0,
                    expected_state=RuntimeState.CREATED,
                    snapshot_after=candidate,
                    event_type=EventType.STATE_TRANSITION,
                    payload={"from": "created", "to": "preparing_workspace"},
                )
            )
            with self.assertRaises(JournalConflict):
                journal.commit(
                    JournalMutation(
                        session_id=snapshot.session_id,
                        expected_version=0,
                        expected_state=RuntimeState.CREATED,
                        snapshot_after=candidate,
                        event_type=EventType.STATE_TRANSITION,
                        payload={"from": "created", "to": "preparing_workspace"},
                    )
                )
            self.assertEqual(
                [event.sequence for event in journal.list_events(snapshot.session_id)],
                [1, 2, 3],
            )
            self.assertEqual(
                journal.load_snapshot(snapshot.session_id).state,
                RuntimeState.PREPARING_WORKSPACE,
            )

    def test_crash_before_commit_rolls_back_everything(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            journal = SQLiteRunJournal(Path(temporary) / "state.db")
            snapshot, _ = self.create(journal)
            candidate = replace(snapshot, state=RuntimeState.PREPARING_WORKSPACE)

            def crash(stage: str) -> None:
                if stage == "before_commit":
                    raise RuntimeError("injected crash")

            journal.commit_hook = crash
            with self.assertRaises(RuntimeError):
                journal.commit(
                    JournalMutation(
                        session_id=snapshot.session_id,
                        expected_version=0,
                        expected_state=RuntimeState.CREATED,
                        snapshot_after=candidate,
                        event_type=EventType.STATE_TRANSITION,
                        payload={"from": "created", "to": "preparing_workspace"},
                    )
                )
            self.assertEqual(journal.load_snapshot(snapshot.session_id), snapshot)
            self.assertEqual(
                [event.sequence for event in journal.list_events(snapshot.session_id)],
                [1, 2],
            )
            self.assertEqual(journal.list_messages(snapshot.session_id)[0].role, "user")

            journal.commit_hook = None
            journal.commit(
                JournalMutation(
                    session_id=snapshot.session_id,
                    expected_version=0,
                    expected_state=RuntimeState.CREATED,
                    snapshot_after=candidate,
                    event_type=EventType.STATE_TRANSITION,
                    payload={"from": "created", "to": "preparing_workspace"},
                )
            )
            self.assertEqual(journal.load_snapshot(snapshot.session_id).version, 1)

    def test_db_events_export_and_rebuild_jsonl_projection(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            journal = SQLiteRunJournal(root / "state.db")
            snapshot, _ = self.create(journal)
            candidate = replace(snapshot, state=RuntimeState.PREPARING_WORKSPACE)
            journal.commit(
                JournalMutation(
                    session_id=snapshot.session_id,
                    expected_version=0,
                    expected_state=RuntimeState.CREATED,
                    snapshot_after=candidate,
                    event_type=EventType.STATE_TRANSITION,
                    payload={"from": "created", "to": "preparing_workspace"},
                )
            )
            destination = export_trace(journal, snapshot.session_id)
            jsonl_store = JsonlEventStore(destination.parent)
            db_projection = replay(journal.list_events(snapshot.session_id)).semantic_projection()
            self.assertEqual(
                replay(jsonl_store.load(snapshot.session_id)).semantic_projection(),
                db_projection,
            )
            destination.unlink()
            self.assertFalse(destination.exists())
            self.assertEqual(
                replay(journal.list_events(snapshot.session_id)).semantic_projection(),
                db_projection,
            )
            export_trace(journal, snapshot.session_id)
            self.assertTrue(destination.exists())

    def test_application_replays_from_sqlite_after_jsonl_is_deleted(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            source.mkdir()
            (source / "value.txt").write_text("value\n", encoding="utf-8")
            application = AgentApplication(root / "agent-home")
            result = application.run_task(
                source=source,
                task="Read the value and answer.",
                backend=ScriptedBackend([{"final": "value"}]),
            )
            projection = application.replay_session(result.session_id).semantic_projection()
            trace_path = Path(result.trace_path)
            self.assertTrue((root / "agent-home" / "state.db").exists())
            self.assertTrue(trace_path.exists())
            trace_path.unlink()

            self.assertEqual(
                application.replay_session(result.session_id).semantic_projection(),
                projection,
            )
            application.export_trace(result.session_id)
            self.assertTrue(trace_path.exists())


if __name__ == "__main__":
    unittest.main()
