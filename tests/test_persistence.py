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
from coding_agent.migrations import (
    FutureSchemaVersion,
    Migration,
    MigrationRunner,
    V1,
    V2,
    V3,
    V4,
    V5,
    V6,
    V7,
)
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
            self.assertEqual(journal.schema_version, 7)
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
                    "memory_records",
                    "memory_events",
                    "memory_retrievals",
                    "repository_identities",
                    "repository_descriptors",
                    "project_scopes",
                    "workspace_bindings",
                    "conversations",
                    "turns",
                    "runtime_executions",
                    "conversation_semantic_events",
                    "product_mapping_failures",
                    "product_admissions",
                    "product_inputs",
                    "turn_admission_artifacts",
                    "workspace_binding_observations",
                    "conversation_rebinds",
                    "workspace_writer_claims",
                    "workspace_writer_operation_receipts",
                    "workspace_recovery_barriers",
                    "turn_finalizations",
                    "instruction_trusts",
                    "instruction_sources",
                    "instruction_snapshots",
                    "instruction_manifests",
                    "instruction_manifest_entries",
                    "instruction_override_edges",
                    "instruction_manifest_status_events",
                    "instruction_manifest_conflicts",
                    "frozen_model_requests",
                    "context_manifests",
                    "model_attempts",
                    "model_attempt_outcomes",
                    "model_attempt_dispatches",
                    "context_operations",
                    "context_selection_items",
                    "conversation_summary_artifacts",
                    "conversation_summary_claims",
                    "tool_result_artifacts",
                    "normalized_observations",
                    "context_excerpts",
                    "file_context_items",
                    "product_operation_receipts",
                    "m3_metric_samples",
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

    def test_v6_inflight_model_request_is_imported_exactly_before_resume(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            db_path = Path(temporary) / "v6-inflight.db"
            connection = sqlite3.connect(db_path)
            MigrationRunner((V1, V2, V3, V4, V5, V6, Migration(7, ()))).migrate(connection)
            connection.execute("DELETE FROM schema_migrations WHERE version=7")
            snapshot = replace(
                self.snapshot("v6-inflight"), state=RuntimeState.CALLING_MODEL,
                active_call_id="v6-request", active_call_kind="model",
            )
            request_json = json.dumps({
                "request_id": "v6-request",
                "messages": [Message(role="user", content="frozen").to_dict()],
                "tools": [], "max_output_tokens": 128,
                "metadata": {"legacy": True},
            }, sort_keys=True, separators=(",", ":"))
            connection.execute(
                """INSERT INTO sessions(
                       id, task, source_path, workspace_path, state, policy_json,
                       source_fingerprint, final_answer, failure_json, step_count,
                       model_calls, tool_calls, last_event_sequence, version,
                       created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, NULL, NULL, 1, 1, 0, 0, 0, ?, ?)""",
                (snapshot.session_id, snapshot.task, snapshot.source_path,
                 snapshot.workspace_path, snapshot.state.value,
                 json.dumps(snapshot.policy.to_dict(), sort_keys=True, separators=(",", ":")),
                 snapshot.source_fingerprint, snapshot.created_at, snapshot.updated_at),
            )
            connection.execute(
                "INSERT INTO checkpoints(session_id, state, snapshot_json, updated_at) VALUES (?, ?, ?, ?)",
                (snapshot.session_id, snapshot.state.value, snapshot.to_json(), snapshot.updated_at),
            )
            connection.execute(
                """INSERT INTO model_calls(
                       request_id, session_id, ordinal, attempt, backend, status,
                       request_json, started_at)
                   VALUES ('v6-request', 'v6-inflight', 1, 1, 'legacy', 'running', ?, ?)""",
                (request_json, snapshot.updated_at),
            )
            connection.commit()
            connection.close()
            journal = SQLiteRunJournal(db_path)
            frozen = journal.connection.execute(
                "SELECT request_json, request_digest FROM frozen_model_requests WHERE request_id='v6-request'"
            ).fetchone()
            self.assertEqual(frozen["request_json"], request_json)
            self.assertEqual(journal.connection.execute(
                "SELECT COUNT(*) FROM model_attempts WHERE request_id='v6-request'"
            ).fetchone()[0], 1)
            self.assertEqual(journal.connection.execute(
                "SELECT COUNT(*) FROM model_attempt_dispatches"
            ).fetchone()[0], 0)
            journal.close()

    def test_product_schema_migration_rolls_back_partial_version(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            for boundary in range(0, len(V5.statements) + 1):
                with self.subTest(statement_boundary=boundary):
                    connection = sqlite3.connect(Path(temporary) / f"state-{boundary}.db")
                    broken_v5 = Migration(
                        version=5,
                        statements=(*V5.statements[:boundary], "THIS IS NOT VALID SQL"),
                    )
                    with self.assertRaises(sqlite3.OperationalError):
                        MigrationRunner((V1, V2, V3, V4, broken_v5, V6, V7)).migrate(connection)
                    self.assertIsNone(
                        connection.execute(
                            "SELECT name FROM sqlite_master WHERE name = 'repository_identities'"
                        ).fetchone()
                    )
                    self.assertEqual(
                        [row[0] for row in connection.execute("SELECT version FROM schema_migrations")],
                        [1, 2, 3, 4],
                    )
                    connection.close()

    def test_product_schema_provenance_and_commit_failures_rollback_legacy_session(self) -> None:
        """V5 failure after DDL must not lose the readable v4 Runtime record."""

        with tempfile.TemporaryDirectory() as temporary:
            for fault_point in ("before_provenance_insert", "before_commit"):
                with self.subTest(fault_point=fault_point):
                    db_path = Path(temporary) / f"state-{fault_point}.db"
                    connection = sqlite3.connect(db_path)
                    seed_v4 = Migration(
                        version=5,
                        statements=("THIS IS NOT VALID SQL",),
                    )
                    with self.assertRaises(sqlite3.OperationalError):
                        MigrationRunner((V1, V2, V3, V4, seed_v4, V6, V7)).migrate(connection)
                    snapshot = self.snapshot(f"legacy-{fault_point}")
                    connection.execute(
                        """
                        INSERT INTO sessions(
                            id, task, source_path, workspace_path, state, policy_json,
                            source_fingerprint, final_answer, failure_json, step_count,
                            model_calls, tool_calls, last_event_sequence, version, created_at,
                            updated_at, lease_owner, lease_expires_at, interrupt_requested_at,
                            resume_target_state, context_version
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, NULL, NULL, ?, ?, ?)
                        """,
                        (
                            snapshot.session_id,
                            snapshot.task,
                            snapshot.source_path,
                            snapshot.workspace_path,
                            snapshot.state.value,
                            json.dumps(snapshot.policy.to_dict(), sort_keys=True),
                            snapshot.source_fingerprint,
                            snapshot.final_answer,
                            json.dumps(snapshot.failure, sort_keys=True),
                            snapshot.step_count,
                            snapshot.model_calls,
                            snapshot.tool_calls,
                            snapshot.version,
                            snapshot.created_at,
                            snapshot.updated_at,
                            snapshot.interrupt_requested_at,
                            (
                                snapshot.resume_target_state.value
                                if snapshot.resume_target_state is not None
                                else None
                            ),
                            snapshot.context_version,
                        ),
                    )
                    connection.execute(
                        "INSERT INTO checkpoints(session_id, state, snapshot_json, updated_at) VALUES (?, ?, ?, ?)",
                        (snapshot.session_id, snapshot.state.value, snapshot.to_json(), snapshot.updated_at),
                    )
                    connection.commit()

                    def fail_at(version: int, point: str) -> None:
                        if version == 5 and point == fault_point:
                            raise RuntimeError(f"injected {point}")

                    with self.assertRaisesRegex(RuntimeError, fault_point):
                        MigrationRunner(migration_fault_hook=fail_at).migrate(connection)
                    self.assertIsNone(
                        connection.execute(
                            "SELECT name FROM sqlite_master WHERE name = 'repository_identities'"
                        ).fetchone()
                    )
                    self.assertEqual(
                        [row[0] for row in connection.execute("SELECT version FROM schema_migrations")],
                        [1, 2, 3, 4],
                    )
                    self.assertEqual(
                        connection.execute("SELECT task FROM sessions WHERE id=?", (snapshot.session_id,)).fetchone()[0],
                        snapshot.task,
                    )
                    self.assertEqual(
                        connection.execute("SELECT snapshot_json FROM checkpoints WHERE session_id=?", (snapshot.session_id,)).fetchone()[0],
                        snapshot.to_json(),
                    )
                    connection.close()

                    upgraded = SQLiteRunJournal(db_path)
                    self.assertEqual(upgraded.load_snapshot(snapshot.session_id), snapshot)
                    upgraded.close()

    def test_m2_v6_failure_at_each_boundary_preserves_v5_session(self) -> None:
        """Every v6 DDL/provenance/commit seam rolls back to a readable v5 DB."""
        with tempfile.TemporaryDirectory() as temporary:
            for boundary in range(len(V6.statements) + 1):
                with self.subTest(statement_boundary=boundary):
                    connection = sqlite3.connect(Path(temporary) / f"m2-{boundary}.db")
                    broken_v6 = Migration(version=6, statements=(*V6.statements[:boundary], "THIS IS NOT VALID SQL"))
                    seed_v5 = Migration(version=6, statements=("THIS IS NOT VALID SQL",))
                    with self.assertRaises(sqlite3.OperationalError):
                        MigrationRunner((V1, V2, V3, V4, V5, seed_v5, V7)).migrate(connection)
                    snapshot = self.snapshot(f"v5-{boundary}")
                    connection.execute(
                        """INSERT INTO sessions(id, task, source_path, workspace_path, state, policy_json,
                           source_fingerprint, final_answer, failure_json, step_count, model_calls, tool_calls,
                           last_event_sequence, version, created_at, updated_at, lease_owner, lease_expires_at,
                           interrupt_requested_at, resume_target_state, context_version)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, NULL, NULL, ?, ?, ?)""",
                        (snapshot.session_id, snapshot.task, snapshot.source_path, snapshot.workspace_path,
                         snapshot.state.value, json.dumps(snapshot.policy.to_dict(), sort_keys=True),
                         snapshot.source_fingerprint, snapshot.final_answer, json.dumps(snapshot.failure, sort_keys=True),
                         snapshot.step_count, snapshot.model_calls, snapshot.tool_calls, snapshot.version,
                         snapshot.created_at, snapshot.updated_at, snapshot.interrupt_requested_at,
                         snapshot.resume_target_state.value if snapshot.resume_target_state else None, snapshot.context_version),
                    )
                    connection.execute("INSERT INTO checkpoints(session_id, state, snapshot_json, updated_at) VALUES (?, ?, ?, ?)",
                                       (snapshot.session_id, snapshot.state.value, snapshot.to_json(), snapshot.updated_at))
                    connection.commit()
                    with self.assertRaises(sqlite3.OperationalError):
                        MigrationRunner((V1, V2, V3, V4, V5, broken_v6, V7)).migrate(connection)
                    self.assertEqual([row[0] for row in connection.execute("SELECT version FROM schema_migrations")], [1, 2, 3, 4, 5])
                    self.assertEqual(connection.execute("SELECT task FROM sessions WHERE id=?", (snapshot.session_id,)).fetchone()[0], snapshot.task)
                    self.assertIsNone(connection.execute("SELECT name FROM sqlite_master WHERE name='product_admissions'").fetchone())
                    connection.close()

    def test_m3_v7_failure_at_each_boundary_preserves_v6_authority(self) -> None:
        """Every v7 statement/provenance/commit seam leaves v6 usable."""
        with tempfile.TemporaryDirectory() as temporary:
            for boundary in range(len(V7.statements) + 1):
                with self.subTest(statement_boundary=boundary):
                    connection = sqlite3.connect(Path(temporary) / f"m3-{boundary}.db")
                    seed_v6 = Migration(version=7, statements=("THIS IS NOT VALID SQL",))
                    with self.assertRaises(sqlite3.OperationalError):
                        MigrationRunner((V1, V2, V3, V4, V5, V6, seed_v6)).migrate(connection)
                    snapshot = self.snapshot(f"v6-{boundary}")
                    connection.execute(
                        """INSERT INTO sessions(id, task, source_path, workspace_path, state, policy_json,
                           source_fingerprint, final_answer, failure_json, step_count, model_calls, tool_calls,
                           last_event_sequence, version, created_at, updated_at, lease_owner, lease_expires_at,
                           interrupt_requested_at, resume_target_state, context_version)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, NULL, NULL, ?, ?, ?)""",
                        (snapshot.session_id, snapshot.task, snapshot.source_path, snapshot.workspace_path,
                         snapshot.state.value, json.dumps(snapshot.policy.to_dict(), sort_keys=True),
                         snapshot.source_fingerprint, snapshot.final_answer, json.dumps(snapshot.failure, sort_keys=True),
                         snapshot.step_count, snapshot.model_calls, snapshot.tool_calls, snapshot.version,
                         snapshot.created_at, snapshot.updated_at, snapshot.interrupt_requested_at,
                         snapshot.resume_target_state.value if snapshot.resume_target_state else None,
                         snapshot.context_version),
                    )
                    connection.execute(
                        "INSERT INTO checkpoints(session_id, state, snapshot_json, updated_at) VALUES (?, ?, ?, ?)",
                        (snapshot.session_id, snapshot.state.value, snapshot.to_json(), snapshot.updated_at),
                    )
                    connection.commit()
                    broken = Migration(
                        version=7,
                        statements=(*V7.statements[:boundary], "THIS IS NOT VALID SQL"),
                    )
                    with self.assertRaises(sqlite3.OperationalError):
                        MigrationRunner((V1, V2, V3, V4, V5, V6, broken)).migrate(connection)
                    self.assertEqual(
                        [row[0] for row in connection.execute("SELECT version FROM schema_migrations")],
                        [1, 2, 3, 4, 5, 6],
                    )
                    self.assertEqual(
                        snapshot.task,
                        connection.execute(
                            "SELECT task FROM sessions WHERE id=?", (snapshot.session_id,),
                        ).fetchone()[0],
                    )
                    self.assertIsNone(connection.execute(
                        "SELECT name FROM sqlite_master WHERE name='frozen_model_requests'"
                    ).fetchone())
                    connection.close()
            for point in ("before_provenance_insert", "before_commit"):
                with self.subTest(fault_point=point):
                    connection = sqlite3.connect(Path(temporary) / f"m3-{point}.db")
                    seed_v6 = Migration(version=7, statements=("THIS IS NOT VALID SQL",))
                    with self.assertRaises(sqlite3.OperationalError):
                        MigrationRunner((V1, V2, V3, V4, V5, V6, seed_v6)).migrate(connection)

                    def fail(version: int, actual: str) -> None:
                        if version == 7 and actual == point:
                            raise RuntimeError(point)

                    with self.assertRaisesRegex(RuntimeError, point):
                        MigrationRunner(migration_fault_hook=fail).migrate(connection)
                    self.assertEqual(
                        [row[0] for row in connection.execute("SELECT version FROM schema_migrations")],
                        [1, 2, 3, 4, 5, 6],
                    )
                    self.assertIsNone(connection.execute(
                        "SELECT name FROM sqlite_master WHERE name='instruction_manifests'"
                    ).fetchone())
                    connection.close()
            for point in ("before_provenance_insert", "before_commit"):
                with self.subTest(fault_point=point):
                    connection = sqlite3.connect(Path(temporary) / f"m2-{point}.db")
                    seed_v5 = Migration(version=6, statements=("THIS IS NOT VALID SQL",))
                    with self.assertRaises(sqlite3.OperationalError):
                        MigrationRunner((V1, V2, V3, V4, V5, seed_v5, V7)).migrate(connection)
                    def fail(version: int, actual: str) -> None:
                        if version == 6 and actual == point:
                            raise RuntimeError(point)
                    with self.assertRaisesRegex(RuntimeError, point):
                        MigrationRunner(migration_fault_hook=fail).migrate(connection)
                    self.assertEqual([row[0] for row in connection.execute("SELECT version FROM schema_migrations")], [1, 2, 3, 4, 5])
                    self.assertIsNone(connection.execute("SELECT name FROM sqlite_master WHERE name='product_admissions'").fetchone())
                    connection.close()

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
