from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from coding_agent.application import AgentApplication
from coding_agent.domain import (
    EventType,
    Message,
    RecoveryMode,
    RunPolicy,
    RuntimeSnapshot,
    RuntimeState,
    Session,
    SummaryRecord,
)
from coding_agent.models.scripted import ScriptedBackend
from coding_agent.migrations import V1, V2, V3, V4
from coding_agent.persistence import PersistenceError, SQLiteRunJournal
from coding_agent.product_domain import ConversationSemanticEvent, RepositoryDescriptor
from coding_agent.product_persistence import ProductRepository


class M1ProductLayerTest(unittest.TestCase):
    def snapshot(self, session_id: str, source_path: str) -> RuntimeSnapshot:
        return Session(
            id=session_id,
            task="legacy task",
            source_path=source_path,
            state=RuntimeState.CREATED,
            policy=RunPolicy(),
            source_fingerprint="legacy-source",
            created_at="2026-09-22T00:00:00+00:00",
            updated_at="2026-09-22T00:00:00+00:00",
        ).to_snapshot()

    def test_repository_identity_is_generated_and_descriptors_do_not_merge_clones(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            journal = SQLiteRunJournal(Path(temporary) / "state.db")
            clone_a = journal.register_repository_identity(
                {
                    "canonical_path": "/work/clone-a",
                    "remote": "https://example.invalid/origin.git",
                    "history": "same-history",
                }
            )
            clone_b = journal.register_repository_identity(
                {
                    "canonical_path": "/work/clone-b",
                    "remote": "https://example.invalid/origin.git",
                    "history": "same-history",
                }
            )
            self.assertNotEqual(clone_a, clone_b)
            self.assertNotIn(clone_a, {"/work/clone-a", "/work/clone-b"})

            worktree_a = journal.register_repository_identity(
                {"canonical_path": "/work/tree-a", "git_common_dir": "/meta/repo.git"}
            )
            worktree_b = journal.register_repository_identity(
                {"canonical_path": "/work/tree-b", "git_common_dir": "/meta/repo.git"}
            )
            self.assertEqual(worktree_a, worktree_b)
            moved = journal.register_repository_identity(
                {"canonical_path": "/work/tree-moved"}, repository_id=worktree_a
            )
            self.assertEqual(moved, worktree_a)
            with self.assertRaises(PersistenceError):
                journal.register_repository_identity(
                    {"canonical_path": "/unregistered"}, repository_id="not-registered"
                )
            with self.assertRaises(PersistenceError):
                journal.register_repository_identity(
                    {"canonical_path": "/work/tree-a"}, repository_id=clone_a
                )
            # A non-Git directory is still a durable generated identity.
            non_git = journal.register_repository_identity({"canonical_path": "/plain"})
            journal.close()

            reopened = SQLiteRunJournal(Path(temporary) / "state.db")
            self.assertEqual(reopened.get_repository_identity(clone_a).repository_id, clone_a)
            self.assertEqual(reopened.get_repository_identity(clone_b).repository_id, clone_b)
            self.assertEqual(reopened.get_repository_identity(worktree_a).repository_id, worktree_a)
            self.assertEqual(reopened.get_repository_identity(non_git).repository_id, non_git)
            self.assertEqual(
                {
                    (descriptor.descriptor_kind, descriptor.descriptor_value)
                    for descriptor in reopened.list_repository_descriptors(clone_a)
                },
                {
                    ("canonical_path", "/work/clone-a"),
                    ("remote", "https://example.invalid/origin.git"),
                    ("history", "same-history"),
                },
            )
            self.assertEqual(
                {
                    descriptor.descriptor_value
                    for descriptor in reopened.list_repository_descriptors(worktree_a)
                    if descriptor.descriptor_kind == "canonical_path"
                },
                {"/work/tree-a", "/work/tree-b", "/work/tree-moved"},
            )
            self.assertEqual(
                {
                    descriptor.descriptor_value
                    for descriptor in reopened.list_repository_descriptors(worktree_a)
                    if descriptor.descriptor_kind == "git_common_dir"
                },
                {"/meta/repo.git"},
            )
            self.assertEqual(
                [
                    (descriptor.descriptor_kind, descriptor.descriptor_value)
                    for descriptor in reopened.list_repository_descriptors(non_git)
                ],
                [("canonical_path", "/plain")],
            )
            reopened.close()

    def test_sqlite_journal_satisfies_private_product_repository_port(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repository: ProductRepository = SQLiteRunJournal(Path(temporary) / "state.db")
            self.assertIsInstance(repository, ProductRepository)
            assert isinstance(repository, SQLiteRunJournal)
            repository.close()

    def test_legacy_mapping_is_atomic_idempotent_and_synthetic(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            journal = SQLiteRunJournal(Path(temporary) / "state.db")
            snapshot = self.snapshot("legacy-session", "/non-git/source")
            journal.create_session(snapshot, Message(role="user", content="legacy task"))

            with self.assertRaisesRegex(RuntimeError, "injected mapping failure"):
                journal.ensure_legacy_product_mapping(
                    snapshot.session_id,
                    failure_hook=lambda _: (_ for _ in ()).throw(
                        RuntimeError("injected mapping failure")
                    ),
                )
            self.assertIsNone(journal.get_legacy_product_mapping(snapshot.session_id))
            self.assertEqual(
                journal.connection.execute("SELECT COUNT(*) FROM conversations").fetchone()[0], 0
            )
            self.assertEqual(journal.load_snapshot(snapshot.session_id), snapshot)

            mapping = journal.ensure_legacy_product_mapping(snapshot.session_id)
            self.assertEqual(journal.ensure_legacy_product_mapping(snapshot.session_id), mapping)
            inspected = journal.inspect_legacy_product_mapping(snapshot.session_id)
            assert inspected is not None
            self.assertEqual(inspected["runtime_execution"], mapping)
            self.assertEqual(inspected["project_scope"].relative_path, ".")
            self.assertEqual(
                inspected["workspace_binding"].binding_kind,
                "legacy_unprepared_compatibility",
            )
            events = journal.list_conversation_semantic_events(snapshot.session_id)
            self.assertEqual(events[0]["event_type"], "legacy_session_imported")
            self.assertEqual(events[0]["provenance_kind"], "synthetic_legacy_mapping")
            self.assertIn("does not reconstruct", events[0]["provenance"]["semantic_limit"])
            self.assertNotIn("InitialRequest", events[0]["event_type"])
            journal.close()

            reopened = SQLiteRunJournal(Path(temporary) / "state.db")
            self.assertEqual(reopened.get_legacy_product_mapping(snapshot.session_id), mapping)
            self.assertIsNotNone(reopened.inspect_legacy_product_mapping(snapshot.session_id))
            self.assertEqual(
                reopened.list_conversation_semantic_events(snapshot.session_id)[0]["event_type"],
                "legacy_session_imported",
            )
            reopened.close()

    def test_corrupt_product_mapping_is_rejected_without_fabricating_an_aggregate(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            journal = SQLiteRunJournal(Path(temporary) / "state.db")
            snapshot = self.snapshot("corrupt-mapping", "/non-git/source")
            journal.create_session(snapshot, Message(role="user", content="legacy task"))
            journal.ensure_legacy_product_mapping(snapshot.session_id)
            journal.connection.execute("PRAGMA foreign_keys = OFF")
            journal.connection.execute(
                "DELETE FROM turns WHERE turn_id=(SELECT turn_id FROM runtime_executions WHERE legacy_session_id=?)",
                (snapshot.session_id,),
            )
            journal.connection.commit()
            journal.connection.execute("PRAGMA foreign_keys = ON")

            with self.assertRaisesRegex(PersistenceError, "corrupt product mapping"):
                journal.get_legacy_product_mapping(snapshot.session_id)
            with self.assertRaisesRegex(PersistenceError, "corrupt product mapping"):
                journal.inspect_legacy_product_mapping(snapshot.session_id)
            journal.close()

            with self.assertRaisesRegex(PersistenceError, "corrupt product mapping"):
                SQLiteRunJournal(Path(temporary) / "state.db")

    def test_cross_repository_product_mapping_is_rejected_without_projection(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            journal = SQLiteRunJournal(Path(temporary) / "state.db")
            snapshot = self.snapshot("cross-repository-mapping", "/non-git/source")
            journal.create_session(snapshot, Message(role="user", content="legacy task"))
            mapping = journal.ensure_legacy_product_mapping(snapshot.session_id)
            other_repository = journal.register_repository_identity({"canonical_path": "/other"})
            journal.connection.execute("PRAGMA foreign_keys = OFF")
            journal.connection.execute(
                "UPDATE workspace_bindings SET repository_id=? WHERE workspace_binding_id=?",
                (other_repository, mapping.workspace_binding_id),
            )
            journal.connection.commit()
            journal.connection.execute("PRAGMA foreign_keys = ON")

            with self.assertRaisesRegex(PersistenceError, "inconsistent aggregate relations"):
                journal.inspect_legacy_product_mapping(snapshot.session_id)
            with self.assertRaisesRegex(PersistenceError, "inconsistent aggregate relations"):
                journal.get_legacy_product_mapping(snapshot.session_id)
            journal.close()

    def test_new_legacy_one_shot_receives_mapping_without_runtime_admission_change(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            source.mkdir()
            (source / "kept.txt").write_text("source remains read-only", encoding="utf-8")
            application = AgentApplication(root / "agent")
            result = application.run_task(
                source=source,
                task="answer once",
                backend=ScriptedBackend([{"final": "done"}]),
                manage_signals=False,
            )
            self.assertEqual(result.state, RuntimeState.COMPLETED)
            self.assertIsInstance(application.journal, SQLiteRunJournal)
            journal = application.journal
            assert isinstance(journal, SQLiteRunJournal)
            mapping = journal.get_legacy_product_mapping(result.session_id)
            self.assertIsNotNone(mapping)
            inspected = journal.inspect_legacy_product_mapping(result.session_id)
            assert inspected is not None
            self.assertEqual(inspected["workspace_binding"].binding_kind, "legacy_copied_workspace")
            self.assertEqual(
                inspected["workspace_binding"].locator,
                str(Path(result.workspace_path).resolve()),
            )
            self.assertEqual((source / "kept.txt").read_text(encoding="utf-8"), "source remains read-only")
            # Runtime remains the old physical sessions record; M1 adds no FSM.
            self.assertEqual(
                journal.connection.execute(
                    "SELECT COUNT(*) FROM sessions WHERE id=?", (result.session_id,)
                ).fetchone()[0],
                1,
            )
            self.assertEqual(journal.list_conversation_semantic_events(result.session_id)[0]["event_type"], "legacy_session_imported")
            journal.close()

    def test_post_run_mapping_failure_preserves_result_and_reopens_for_backfill(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            source.mkdir()
            db_path = root / "agent" / "state.db"
            journal = SQLiteRunJournal(
                db_path,
                mapping_failure_hook=lambda _: (_ for _ in ()).throw(RuntimeError("mapping unavailable")),
            )
            backend = ScriptedBackend([{"final": "done"}])
            result = AgentApplication(root / "agent", journal=journal).run_task(
                source=source,
                task="finish despite mapping failure",
                backend=backend,
                manage_signals=False,
            )
            self.assertEqual(result.state, RuntimeState.COMPLETED)
            self.assertEqual(len(backend.requests), 1)
            self.assertIsNone(journal.get_legacy_product_mapping(result.session_id))
            self.assertEqual(
                journal.connection.execute("SELECT COUNT(*) FROM conversations").fetchone()[0], 0
            )
            failure = journal.get_legacy_product_mapping_failure(result.session_id)
            assert failure is not None
            self.assertEqual(failure["attempt_count"], 1)
            self.assertIsNone(failure["recovered_at"])
            journal.close()

            recovered = SQLiteRunJournal(db_path)
            self.assertIsNotNone(recovered.get_legacy_product_mapping(result.session_id))
            failure = recovered.get_legacy_product_mapping_failure(result.session_id)
            assert failure is not None
            self.assertIsNotNone(failure["recovered_at"])
            recovered.close()

    def test_constructor_backfill_failure_is_observable_and_recovers_on_next_open(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            db_path = Path(temporary) / "state.db"
            journal = SQLiteRunJournal(db_path)
            snapshot = self.snapshot("startup-backfill", "/legacy/source")
            journal.create_session(snapshot, Message(role="user", content=snapshot.task))
            journal.close()

            with self.assertRaisesRegex(RuntimeError, "mapping unavailable"):
                SQLiteRunJournal(
                    db_path,
                    mapping_failure_hook=lambda _: (_ for _ in ()).throw(
                        RuntimeError("mapping unavailable")
                    ),
                )
            import sqlite3

            raw = sqlite3.connect(db_path)
            self.assertEqual(raw.execute("SELECT COUNT(*) FROM runtime_executions").fetchone()[0], 0)
            self.assertEqual(
                raw.execute(
                    "SELECT attempt_count FROM product_mapping_failures WHERE legacy_session_id='startup-backfill'"
                ).fetchone()[0],
                1,
            )
            self.assertEqual(
                raw.execute("SELECT task FROM sessions WHERE id='startup-backfill'").fetchone()[0],
                "legacy task",
            )
            raw.close()

            recovered = SQLiteRunJournal(db_path)
            self.assertIsNotNone(recovered.get_legacy_product_mapping("startup-backfill"))
            failure = recovered.get_legacy_product_mapping_failure("startup-backfill")
            assert failure is not None
            self.assertIsNotNone(failure["recovered_at"])
            recovered.close()

    def test_mapping_failure_record_failure_still_preserves_legacy_result(self) -> None:
        class RecordFailureJournal(SQLiteRunJournal):
            def record_legacy_product_mapping_failure(self, session_id: str, error: Exception) -> None:
                raise RuntimeError("mapping failure record unavailable")

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            source.mkdir()
            journal = RecordFailureJournal(
                root / "agent" / "state.db",
                mapping_failure_hook=lambda _: (_ for _ in ()).throw(RuntimeError("mapping unavailable")),
            )
            backend = ScriptedBackend([{"final": "done"}])
            result = AgentApplication(root / "agent", journal=journal).run_task(
                source=source,
                task="preserve result when recovery audit is unavailable",
                backend=backend,
                manage_signals=False,
            )
            self.assertEqual(result.state, RuntimeState.COMPLETED)
            self.assertEqual(len(backend.requests), 1)
            self.assertIsNone(journal.get_legacy_product_mapping(result.session_id))
            journal.close()

    def test_interrupted_legacy_fixture_maps_recorded_copied_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            copied = root / "copied-workspace"
            copied.mkdir()
            journal = SQLiteRunJournal(root / "state.db")
            snapshot = self.snapshot("interrupted-session", str(root / "source"))
            snapshot = RuntimeSnapshot.from_dict(
                {**snapshot.to_dict(), "state": "interrupted", "workspace_path": str(copied)}
            )
            journal.create_session(snapshot, Message(role="user", content="legacy task"))
            journal.ensure_legacy_product_mapping(snapshot.session_id)
            inspected = journal.inspect_legacy_product_mapping(snapshot.session_id)
            assert inspected is not None
            self.assertEqual(inspected["workspace_binding"].binding_kind, "legacy_copied_workspace")
            self.assertEqual(inspected["workspace_binding"].locator, str(copied.resolve()))
            journal.close()

    def test_product_value_records_reject_invalid_identity_and_sequence(self) -> None:
        with self.assertRaises(ValueError):
            RepositoryDescriptor("", "repository", "canonical_path", "/work")
        with self.assertRaises(ValueError):
            ConversationSemanticEvent("event", "conversation", 0, "imported", "synthetic")

    def test_v4_database_upgrades_additively_and_backfills_existing_session(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            db_path = Path(temporary) / "state.db"
            import sqlite3

            legacy = sqlite3.connect(db_path)
            legacy.execute(
                "CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
            )
            for migration in (V1, V2, V3, V4):
                for statement in migration.statements:
                    legacy.execute(statement)
                legacy.execute(
                    "INSERT INTO schema_migrations(version, applied_at) VALUES (?, 'legacy')",
                    (migration.version,),
                )
            timestamp = "2026-09-21T00:00:00+00:00"
            uncertain_snapshot = RuntimeSnapshot(
                session_id="legacy-uncertain",
                task="preserve legacy-uncertain",
                source_path="/legacy/non-git",
                state=RuntimeState.WAITING_APPROVAL,
                policy=RunPolicy(),
                source_fingerprint="old-source",
                workspace_path="/copied/uncertain",
                resume_target_state=RuntimeState.CALLING_MODEL,
                interrupt_requested_at=timestamp,
                step_count=2,
                model_calls=1,
                tool_calls=1,
                created_at=timestamp,
                updated_at=timestamp,
                version=2,
            )
            uncertain_snapshot_json = uncertain_snapshot.to_json()
            summary = SummaryRecord(
                summary_id="legacy-summary",
                schema_version=1,
                session_id="legacy-retry",
                source_event_start=1,
                source_event_end=2,
                source_event_hash="event-hash",
                workspace_revision="revision",
                goals=("preserved",),
                created_at=timestamp,
            )
            summary_json = json.dumps(summary.to_dict(), ensure_ascii=False, sort_keys=True)
            sessions = (
                ("legacy-created", "created", None, None, None, None, None),
                ("legacy-completed", "completed", "/copied/completed", "answer", None, None, None),
                ("legacy-failed", "failed", "/copied/failed", None, '{"kind":"legacy"}', None, None),
                ("legacy-interrupted", "interrupted", "/copied/interrupted", None, None, "lease-a", timestamp),
                ("legacy-retry", "retry_wait", "/copied/retry", None, None, None, None),
                ("legacy-uncertain", "waiting_approval", "/copied/uncertain", None, None, "lease-b", timestamp),
            )
            legacy.executemany(
                """
                INSERT INTO sessions(
                    id, task, source_path, workspace_path, state, policy_json,
                    source_fingerprint, final_answer, failure_json, step_count,
                    model_calls, tool_calls, last_event_sequence, version, created_at,
                    updated_at, lease_owner, lease_expires_at, interrupt_requested_at,
                    resume_target_state, context_version
                ) VALUES (?, ?, '/legacy/non-git', ?, ?, '{"legacy":true}', 'old-source', ?, ?,
                          2, 1, 1, 3, 2, ?, ?, ?, ?, ?, 'calling_model', '1')
                """,
                [
                    (session_id, f"preserve {session_id}", workspace, state, answer, failure,
                     timestamp, timestamp, owner, expiry, timestamp)
                    for session_id, state, workspace, answer, failure, owner, expiry in sessions
                ],
            )
            legacy.execute(
                """
                INSERT INTO messages(session_id, message_index, role, content, tool_call_id, metadata_json, created_at)
                VALUES ('legacy-uncertain', 0, 'tool', 'preserve message', 'tool-uncertain', '{"legacy":true}', ?)
                """,
                (timestamp,),
            )
            legacy.execute(
                """
                INSERT INTO events(event_id, session_id, sequence, schema_version, event_type, state, payload_json, created_at)
                VALUES ('legacy-reconciliation-event', 'legacy-uncertain', 3, 1, ?,
                        'waiting_approval', '{"call_id":"tool-uncertain","permission":"write"}', ?)
                """,
                (EventType.APPROVAL_REQUESTED.value, timestamp),
            )
            legacy.execute(
                """
                INSERT INTO checkpoints(session_id, state, snapshot_json, updated_at)
                VALUES ('legacy-uncertain', 'waiting_approval', ?, ?)
                """,
                (uncertain_snapshot_json, timestamp),
            )
            legacy.executemany(
                """
                INSERT INTO model_calls(request_id, session_id, ordinal, attempt, backend, status,
                                        request_json, response_json, error_json, started_at, finished_at)
                VALUES (?, ?, 1, 1, 'legacy', ?, '{"request":"preserved"}', ?, ?, ?, ?)
                """,
                (
                    ("committed-response", "legacy-completed", "completed", '{"text":"committed"}', None, timestamp, timestamp),
                    ("uncertain-model", "legacy-uncertain", "uncertain", None, '{"kind":"uncertain"}', timestamp, None),
                ),
            )
            legacy.execute(
                """
                INSERT INTO tool_calls(call_id, session_id, ordinal, attempt, tool_name, arguments_json,
                                       recovery_mode, status, pre_revision, planned_post_revision,
                                       result_json, error_json, started_at, finished_at)
                VALUES ('tool-uncertain', 'legacy-uncertain', 1, 1, 'edit_file', '{"path":"x.py"}',
                        ?, 'uncertain', 'before', 'after', NULL, '{"kind":"uncertain"}', ?, NULL)
                """,
                (RecoveryMode.RECONCILABLE_WRITE.value, timestamp),
            )
            legacy.execute(
                """
                INSERT INTO summaries(summary_id, session_id, schema_version, source_event_start,
                                      source_event_end, source_event_hash, workspace_revision,
                                      summary_json, created_at, superseded_by, stale)
                VALUES ('legacy-summary', 'legacy-retry', 1, 1, 2, 'event-hash', 'revision',
                        ?, ?, NULL, 0)
                """,
                (summary_json, timestamp),
            )
            legacy.execute(
                """
                INSERT INTO memory_records(
                    memory_id, schema_version, scope, scope_id, kind, content,
                    source_run_id, source_agent_id, source_event_refs_json,
                    repository_revision, confidence, created_at, expires_at, status,
                    supersedes, content_hash, version, updated_at
                ) VALUES ('legacy-memory', 1, 'repository', 'legacy-repo', 'semantic', 'retained memory',
                          'legacy-run', 'legacy-agent', '[]', NULL, 0.9, ?, NULL, 'active',
                          NULL, 'memory-content-hash', 0, ?)
                """,
                (timestamp, timestamp),
            )
            legacy.commit()
            legacy.close()

            upgraded = SQLiteRunJournal(db_path)
            self.assertEqual(upgraded.schema_version, 5)
            self.assertEqual(
                upgraded.connection.execute(
                    "SELECT task FROM sessions WHERE id='legacy-created'"
                ).fetchone()[0],
                "preserve legacy-created",
            )
            self.assertEqual(
                upgraded.connection.execute("SELECT COUNT(*) FROM runtime_executions").fetchone()[0],
                len(sessions),
            )
            for session_id, *_ in sessions:
                self.assertIsNotNone(upgraded.get_legacy_product_mapping(session_id))
            self.assertEqual(
                upgraded.connection.execute(
                    "SELECT metadata_json FROM messages WHERE session_id='legacy-uncertain'"
                ).fetchone()[0],
                '{"legacy":true}',
            )
            self.assertEqual(
                upgraded.connection.execute(
                    "SELECT payload_json FROM events WHERE event_id='legacy-reconciliation-event'"
                ).fetchone()[0],
                '{"call_id":"tool-uncertain","permission":"write"}',
            )
            self.assertEqual(
                upgraded.connection.execute(
                    "SELECT response_json FROM model_calls WHERE request_id='committed-response'"
                ).fetchone()[0],
                '{"text":"committed"}',
            )
            self.assertEqual(
                tuple(
                    upgraded.connection.execute(
                        "SELECT recovery_mode, status, error_json FROM tool_calls WHERE call_id='tool-uncertain'"
                    ).fetchone()
                ),
                (RecoveryMode.RECONCILABLE_WRITE.value, "uncertain", '{"kind":"uncertain"}'),
            )
            self.assertEqual(
                upgraded.connection.execute(
                    "SELECT summary_json FROM summaries WHERE summary_id='legacy-summary'"
                ).fetchone()[0],
                summary_json,
            )
            self.assertEqual(
                tuple(
                    upgraded.connection.execute(
                        "SELECT lease_owner, interrupt_requested_at FROM sessions WHERE id='legacy-uncertain'"
                    ).fetchone()
                ),
                ("lease-b", timestamp),
            )
            self.assertEqual(
                upgraded.connection.execute("SELECT COUNT(*) FROM memory_records").fetchone()[0], 1
            )
            self.assertEqual(
                [row[0] for row in upgraded.connection.execute("SELECT version FROM schema_migrations")],
                [1, 2, 3, 4, 5],
            )
            self.assertEqual(upgraded.load_snapshot("legacy-uncertain"), uncertain_snapshot)
            self.assertEqual(
                upgraded.list_events("legacy-uncertain")[0].event_type,
                EventType.APPROVAL_REQUESTED,
            )
            self.assertEqual(
                upgraded.get_model_call("legacy-completed", "committed-response")["response"],
                {"text": "committed"},
            )
            self.assertEqual(
                upgraded.get_tool_call("legacy-uncertain", "tool-uncertain")["recovery_mode"],
                RecoveryMode.RECONCILABLE_WRITE.value,
            )
            self.assertEqual(upgraded.get_summary("legacy-retry", "legacy-summary"), summary)
            upgraded.close()

    def test_backfill_preserves_terminal_and_recovery_state_sessions(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            journal = SQLiteRunJournal(root / "state.db")
            states = (
                RuntimeState.COMPLETED,
                RuntimeState.FAILED,
                RuntimeState.INTERRUPTED,
                RuntimeState.RETRY_WAIT,
                RuntimeState.WAITING_APPROVAL,
            )
            for position, state in enumerate(states):
                snapshot = self.snapshot(f"state-{position}", str(root / f"source-{position}"))
                snapshot = RuntimeSnapshot.from_dict(
                    {**snapshot.to_dict(), "state": state.value}
                )
                journal.create_session(snapshot, Message(role="user", content=snapshot.task))
            mappings = journal.backfill_legacy_product_mappings()
            self.assertEqual(len(mappings), len(states))
            for position, state in enumerate(states):
                session_id = f"state-{position}"
                self.assertIsNotNone(journal.get_legacy_product_mapping(session_id))
                self.assertEqual(journal.load_snapshot(session_id).state, state)
                self.assertEqual([event.sequence for event in journal.list_events(session_id)], [1, 2])
            journal.close()
