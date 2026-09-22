from __future__ import annotations

import tempfile
import threading
import unittest
import os
from dataclasses import replace
from pathlib import Path

from coding_agent.application import AgentApplication
from coding_agent.context import BudgetedContextBuilder
from coding_agent.domain import EventType, RecoveryMode, RunPolicy, RuntimeSnapshot, RuntimeState, ToolCallState
from coding_agent.models.scripted import ScriptedBackend
from coding_agent.persistence import (
    DirectTreeReadOnlyViolation,
    LeaseConflict,
    ProductAdmissionConflict,
    ProductLifecycleConflict,
    SQLiteRunJournal,
    JournalMutation,
    ToolCallMutation,
)
from coding_agent.product_application import (
    AdmissionCommand,
    ConversationApplicationCoordinator,
    ResumeExecutionCommand,
    SteeringCommand,
)
from coding_agent.product_workspace import (
    ReadOnlyDirectProductComposition,
    capture_direct_tree_manifest,
    discover_direct_workspace,
    observe_direct_workspace,
)
from coding_agent.runtime import AgentRuntime
from coding_agent.test_profiles import default_test_profiles
from coding_agent.tools.builtin import build_builtin_registry
from coding_agent.tools.harness import ToolHarness
from coding_agent.trajectory import TrajectoryRecorder
from coding_agent.workspace import tree_fingerprint


class M2ProductLifecycleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "project"
        self.root.mkdir()
        (self.root / "sentinel.txt").write_text("unchanged", encoding="utf-8")
        self.journal = SQLiteRunJournal(Path(self.tmp.name) / "state.db")
        self.direct = discover_direct_workspace(self.journal, self.root)

    def tearDown(self) -> None:
        self.journal.close()
        self.tmp.cleanup()

    def _snapshot(self, session_id: str = "m2-session") -> RuntimeSnapshot:
        return RuntimeSnapshot(
            session_id=session_id,
            task="M2 admission task",
            source_path=str(self.root),
            state=RuntimeState.CREATED,
            policy=RunPolicy(),
            source_fingerprint=tree_fingerprint(self.root),
        )

    def _admit(self, operation: str = "admit-1", digest: str | None = None, session: str = "m2-session"):
        snapshot = self._snapshot(session)
        digest = digest or self.journal.canonical_admission_digest(
            initial_request="inspect only", repository_id=self.direct.repository_id,
            project_scope_id=self.direct.project_scope.project_scope_id,
            workspace_binding_id=self.direct.binding.workspace_binding_id, snapshot=snapshot,
            policy=RunPolicy(), conversation_id=None, expected_conversation_version=None,
        )
        return self.journal.admit_turn(
            operation_id=operation, payload_digest=digest, initial_request="inspect only",
            repository_id=self.direct.repository_id,
            project_scope_id=self.direct.project_scope.project_scope_id,
            workspace_binding_id=self.direct.binding.workspace_binding_id,
            snapshot=snapshot, policy=RunPolicy(),
        )

    def _conversation_version(self, conversation_id: str) -> int:
        return int(self.journal.connection.execute(
            "SELECT product_version FROM conversations WHERE conversation_id=?", (conversation_id,)
        ).fetchone()[0])

    def _input_digest(
        self, conversation_id: str, kind: str, payload: dict[str, object],
        correlation_id: str | None = None, version: int | None = None,
    ) -> str:
        expected_version = self._conversation_version(conversation_id) if version is None else version
        return self.journal.canonical_input_digest(
            conversation_id=conversation_id, input_kind=kind, payload=payload,
            correlation_id=correlation_id, expected_conversation_version=expected_version,
        )

    def _claim(
        self, binding_id: str, owner: str, *, operation: str, expected_epoch: int = 0,
        lease_seconds: float = 60, runtime_execution_id: str | None = None,
    ):
        observation_digest = self.journal.connection.execute(
            "SELECT observation_digest FROM workspace_binding_observations WHERE workspace_binding_id=? ORDER BY observed_at DESC LIMIT 1",
            (binding_id,),
        ).fetchone()[0]
        observation = {"claim": operation}
        return self.journal.claim_workspace_writer(
            operation_id=operation,
            payload_digest=self.journal.canonical_writer_claim_digest(
                workspace_binding_id=binding_id, owner_id=owner, expected_epoch=expected_epoch,
                expected_observation_digest=observation_digest, observation=observation, lease_seconds=lease_seconds,
                runtime_execution_id=runtime_execution_id,
            ), workspace_binding_id=binding_id, owner_id=owner, expected_epoch=expected_epoch,
            expected_observation_digest=observation_digest, lease_seconds=lease_seconds, observation=observation,
            runtime_execution_id=runtime_execution_id,
        )

    def _release(self, claim, *, operation: str) -> None:
        observation_digest = self.journal.connection.execute(
            "SELECT observation_digest FROM workspace_binding_observations WHERE workspace_binding_id=? ORDER BY observed_at DESC LIMIT 1",
            (claim.workspace_binding_id,),
        ).fetchone()[0]
        self.journal.release_workspace_writer(
            claim.claim_id, operation_id=operation,
            payload_digest=self.journal.canonical_product_payload_digest({
                "kind": "writer_release", "claim_id": claim.claim_id, "owner_id": claim.owner_id,
                "expected_epoch": claim.claim_epoch, "expected_observation_digest": observation_digest,
            }), owner_id=claim.owner_id, expected_epoch=claim.claim_epoch,
            expected_observation_digest=observation_digest,
        )

    def test_v6_atomic_admission_is_idempotent_and_rejects_reused_key(self) -> None:
        admission = self._admit()
        again = self._admit()
        self.assertTrue(again.idempotent)
        self.assertEqual(admission.turn_id, again.turn_id)
        self.assertEqual(self.journal.load_session(admission.legacy_session_id).task, "M2 admission task")
        with self.assertRaises(ProductAdmissionConflict):
            self._admit(digest="different")
        rows = self.journal.connection.execute("SELECT COUNT(*) FROM product_admissions").fetchone()
        self.assertEqual(rows[0], 1)

    def test_admission_faults_leave_no_session_or_product_half_aggregate(self) -> None:
        for point in (
            "before_conversation_publication", "after_conversation_publication",
            "before_session", "before_session_row", "after_session_row", "before_checkpoint",
            "after_checkpoint", "before_initial_message", "after_initial_message",
            "before_session_created_event", "after_session_created_event",
            "before_message_added_event", "after_message_added_event", "after_session",
            "before_turn", "after_turn", "before_runtime_execution", "after_runtime_execution",
            "before_admission_artifacts", "after_admission_artifacts",
            "before_initial_input", "after_initial_input",
            "before_admission_record", "after_admission_record",
            "before_conversation_update", "after_conversation_update",
            "before_initial_request_accepted_event", "after_initial_request_accepted_event",
            "before_turn_admitted_event", "after_turn_admitted_event",
            "after_semantic_events", "before_precommit", "before_commit", "after_precommit",
        ):
            with self.subTest(point=point):
                db = Path(self.tmp.name) / f"{point}.db"
                journal = SQLiteRunJournal(db)
                direct = discover_direct_workspace(journal, self.root)
                table_names = [str(row[0]) for row in journal.connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
                )]
                baseline = {
                    name: tuple(tuple(row) for row in journal.connection.execute(
                        f'SELECT * FROM "{name}" ORDER BY rowid'
                    ))
                    for name in table_names
                }
                def fail(actual: str) -> None:
                    if actual == point:
                        raise RuntimeError(point)
                with self.assertRaises(RuntimeError):
                    snapshot = self._snapshot(f"session-{point}")
                    journal.admit_turn(
                        operation_id="one", payload_digest=journal.canonical_admission_digest(
                            initial_request="x", repository_id=direct.repository_id,
                            project_scope_id=direct.project_scope.project_scope_id,
                            workspace_binding_id=direct.binding.workspace_binding_id, snapshot=snapshot,
                            policy=RunPolicy(), conversation_id=None, expected_conversation_version=None,
                        ), initial_request="x",
                        repository_id=direct.repository_id, project_scope_id=direct.project_scope.project_scope_id,
                        workspace_binding_id=direct.binding.workspace_binding_id,
                        snapshot=snapshot, policy=RunPolicy(), fault_hook=fail,
                    )
                for table in (
                    "conversations", "sessions", "checkpoints", "messages", "events", "turns",
                    "runtime_executions", "product_admissions", "product_inputs",
                    "turn_admission_artifacts", "conversation_semantic_events",
                ):
                    self.assertEqual(journal.connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0], 0)
                self.assertEqual(baseline, {
                    name: tuple(tuple(row) for row in journal.connection.execute(
                        f'SELECT * FROM "{name}" ORDER BY rowid'
                    ))
                    for name in table_names
                })
                journal.close()

    def test_outer_admission_fault_matrix_preserves_existing_idle_conversation(self) -> None:
        points = (
            "before_conversation_publication", "after_conversation_publication",
            "before_session", "after_session", "before_turn", "after_turn",
            "before_runtime_execution", "after_runtime_execution",
            "before_admission_artifacts", "after_admission_artifacts",
            "before_initial_input", "after_initial_input",
            "before_admission_record", "after_admission_record",
            "before_conversation_update", "after_conversation_update",
            "before_initial_request_accepted_event", "after_initial_request_accepted_event",
            "before_turn_admitted_event", "after_turn_admitted_event",
            "before_precommit", "after_precommit",
        )

        def database_state(journal: SQLiteRunJournal) -> dict[str, tuple[tuple[object, ...], ...]]:
            names = [str(row[0]) for row in journal.connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
            )]
            return {
                name: tuple(tuple(row) for row in journal.connection.execute(
                    f'SELECT * FROM "{name}" ORDER BY rowid'
                ))
                for name in names
            }

        for point in points:
            with self.subTest(point=point):
                db = Path(self.tmp.name) / f"existing-{point}.db"
                journal = SQLiteRunJournal(db)
                direct = discover_direct_workspace(journal, self.root)
                first = self._snapshot(f"seed-{point}")
                first_digest = journal.canonical_admission_digest(
                    initial_request="seed", repository_id=direct.repository_id,
                    project_scope_id=direct.project_scope.project_scope_id,
                    workspace_binding_id=direct.binding.workspace_binding_id,
                    snapshot=first, policy=RunPolicy(), conversation_id=None,
                    expected_conversation_version=None,
                )
                admitted = journal.admit_turn(
                    operation_id=f"seed-{point}", payload_digest=first_digest,
                    initial_request="seed", repository_id=direct.repository_id,
                    project_scope_id=direct.project_scope.project_scope_id,
                    workspace_binding_id=direct.binding.workspace_binding_id,
                    snapshot=first, policy=RunPolicy(),
                )
                journal.connection.execute(
                    "UPDATE sessions SET state='completed' WHERE id=?",
                    (admitted.legacy_session_id,),
                )
                journal.finalize_admitted_turn(admitted.operation_id)
                version = int(journal.inspect_conversation(admitted.conversation_id)["product_version"])  # type: ignore[index]
                baseline = database_state(journal)
                candidate = self._snapshot(f"candidate-{point}")
                digest = journal.canonical_admission_digest(
                    initial_request="next", repository_id=direct.repository_id,
                    project_scope_id=direct.project_scope.project_scope_id,
                    workspace_binding_id=direct.binding.workspace_binding_id,
                    snapshot=candidate, policy=RunPolicy(),
                    conversation_id=admitted.conversation_id,
                    expected_conversation_version=version,
                )

                def fail(actual: str) -> None:
                    if actual == point:
                        raise RuntimeError(point)

                with self.assertRaises(RuntimeError):
                    journal.admit_turn(
                        operation_id=f"candidate-{point}", payload_digest=digest,
                        initial_request="next", repository_id=direct.repository_id,
                        project_scope_id=direct.project_scope.project_scope_id,
                        workspace_binding_id=direct.binding.workspace_binding_id,
                        snapshot=candidate, policy=RunPolicy(),
                        conversation_id=admitted.conversation_id,
                        expected_conversation_version=version, fault_hook=fail,
                    )
                self.assertEqual(baseline, database_state(journal))
                journal.close()

    def test_one_open_turn_rebind_and_writer_barrier_are_fail_closed(self) -> None:
        admission = self._admit()
        with self.assertRaises(ProductLifecycleConflict):
            snapshot = self._snapshot("m2-session-2")
            self.journal.admit_turn(
                operation_id="admit-2", payload_digest=self.journal.canonical_admission_digest(
                    initial_request="next", repository_id=self.direct.repository_id,
                    project_scope_id=self.direct.project_scope.project_scope_id,
                    workspace_binding_id=self.direct.binding.workspace_binding_id, snapshot=snapshot,
                    policy=RunPolicy(), conversation_id=admission.conversation_id, expected_conversation_version=1,
                ), initial_request="next",
                repository_id=self.direct.repository_id,
                project_scope_id=self.direct.project_scope.project_scope_id,
                workspace_binding_id=self.direct.binding.workspace_binding_id,
                snapshot=snapshot, policy=RunPolicy(),
                conversation_id=admission.conversation_id, expected_conversation_version=1,
            )
        with self.assertRaises(ProductLifecycleConflict):
            self.journal.rebind_conversation(
                operation_id="rebind", payload_digest=self.journal.canonical_rebind_digest(
                    conversation_id=admission.conversation_id,
                    workspace_binding_id=self.direct.binding.workspace_binding_id,
                    expected_version=1, observation={},
                ), conversation_id=admission.conversation_id,
                workspace_binding_id=self.direct.binding.workspace_binding_id, expected_version=1, observation={},
            )
        # Runtime remains the authority for terminal state; this simulates its committed outcome.
        self.journal.connection.execute("UPDATE sessions SET state='completed' WHERE id=?", (admission.legacy_session_id,))
        self.journal.finalize_admitted_turn(admission.operation_id)
        rebound_root = Path(self.tmp.name) / "rebound"
        rebound_root.mkdir()
        rebound = self.journal.register_workspace_binding(
            repository_id=self.direct.repository_id,
            project_scope_id=self.direct.project_scope.project_scope_id,
            binding_kind="m2_direct_read_only", locator=str(rebound_root),
            observation=observe_direct_workspace(rebound_root).to_dict(),
        )
        rebind_observation = observe_direct_workspace(rebound_root).to_dict()
        self.assertEqual(3, self.journal.rebind_conversation(
            operation_id="rebind", payload_digest=self.journal.canonical_rebind_digest(
                conversation_id=admission.conversation_id, workspace_binding_id=rebound.workspace_binding_id,
                expected_version=2, observation=rebind_observation,
            ), conversation_id=admission.conversation_id,
            workspace_binding_id=rebound.workspace_binding_id, expected_version=2, observation=rebind_observation,
        ))
        claim = self._claim(rebound.workspace_binding_id, "owner-a", operation="claim-owner-a")
        with self.assertRaises(LeaseConflict):
            self._claim(rebound.workspace_binding_id, "owner-b", operation="claim-owner-b", expected_epoch=1)
        self._release(claim, operation="release-owner-a")

    def test_direct_tree_composition_is_read_only_and_coordinator_is_thin(self) -> None:
        before = self._tree_manifest()
        composition = ReadOnlyDirectProductComposition(self.direct)
        self.assertEqual("complete_bounded_filesystem_observation", self.direct.observation.observation_frontier)
        self.assertEqual("unavailable_no_git_process", self.direct.observation.git_facts["dirty"])
        self.assertEqual("unavailable_no_git_process", self.direct.observation.git_facts["staged"])
        self.assertEqual("unavailable_no_git_process", self.direct.observation.git_facts["untracked"])
        self.assertEqual(self.direct.observation.canonical_path, composition.read_observation().canonical_path)
        for action in (
            "edit", "create", "delete", "command", "test", "build", "cache", "git-index",
            "git-ref", "git-lock", "hook", "helper", "startup-artifact", "indirect-child", "undo",
        ):
            with self.assertRaises(DirectTreeReadOnlyViolation):
                composition.reject_side_effect(action)
        self.assertEqual(before, self._tree_manifest())
        readable = ReadOnlyDirectProductComposition(
            self.direct, harness=ToolHarness(build_builtin_registry(default_test_profiles())),
        )
        read = readable.read(call_id="direct-read", tool_name="read_file", arguments={"path": "sentinel.txt"})
        self.assertTrue(read.ok)
        self.assertIn("unchanged", str(read.data))
        with self.assertRaises(DirectTreeReadOnlyViolation):
            readable.read(call_id="direct-edit", tool_name="edit_file", arguments={})
        self.assertEqual(before, self._tree_manifest())
        # Restart retains the same direct read-only declaration and does not
        # create a marker, cache, Git index/ref/lock, or helper artifact.
        reopened = SQLiteRunJournal(Path(self.tmp.name) / "restart.db")
        restarted = discover_direct_workspace(reopened, self.root)
        self.assertEqual(before, self._tree_manifest())
        self.assertEqual("m2_direct_read_only", restarted.binding.binding_kind)
        reopened.close()
        coordinator = ConversationApplicationCoordinator(self.journal)
        coordinator_snapshot = self._snapshot("coordinator-session")
        admitted = coordinator.admit(AdmissionCommand(
            operation_id="coordinator", payload_digest=self.journal.canonical_admission_digest(
                initial_request="read", repository_id=self.direct.repository_id,
                project_scope_id=self.direct.project_scope.project_scope_id,
                workspace_binding_id=self.direct.binding.workspace_binding_id,
                snapshot=coordinator_snapshot, policy=RunPolicy(), conversation_id=None,
                expected_conversation_version=None,
            ), initial_request="read",
            repository_id=self.direct.repository_id, project_scope_id=self.direct.project_scope.project_scope_id,
            workspace_binding_id=self.direct.binding.workspace_binding_id,
            snapshot=coordinator_snapshot, policy=RunPolicy(),
        ))
        inspected = coordinator.resume_conversation(admitted.conversation_id)
        self.assertFalse(inspected["resume_started"])
        self.assertEqual(admitted.turn_id, inspected["turns"][0]["turn_id"])
        self.assertEqual(admitted.turn_id, coordinator.resume_execution(
            ResumeExecutionCommand("coordinator", admitted.conversation_id)
        ).turn_id)
        self.assertEqual("read_only", coordinator.inspect_direct_startup(
            self.direct.binding.workspace_binding_id
        )["discovery_mode"])

    def _tree_manifest(self) -> tuple[tuple[str, bytes], ...]:
        return tuple(sorted(
            (path.relative_to(self.root).as_posix(), path.read_bytes())
            for path in self.root.rglob("*") if path.is_file()
        ))

    def test_typed_steering_reaches_real_runtime_context_once_and_terminal_is_atomic(self) -> None:
        admission = self._admit(operation="runtime-admit", session="runtime-session")
        coordinator = ConversationApplicationCoordinator(self.journal)
        version = self._conversation_version(admission.conversation_id)
        payload = {"text": "typed steering"}
        coordinator.steer(SteeringCommand(
            operation_id="typed-steering",
            payload_digest=self._input_digest(
                admission.conversation_id, "steering", payload, version=version,
            ),
            conversation_id=admission.conversation_id,
            expected_conversation_version=version,
            text="typed steering",
        ))

        class RecordingContextBuilder:
            def __init__(self) -> None:
                self.delegate = BudgetedContextBuilder()
                self.seen: list[list[str]] = []

            def build(self, request):
                self.seen.append([message.content for message in request.messages])
                return self.delegate.build(request)

        context_builder = RecordingContextBuilder()
        host = AgentApplication(
            Path(self.tmp.name) / "product-host", journal=self.journal, event_store=self.journal,
            context_builder=context_builder,
        )
        session = self.journal.load_session(admission.legacy_session_id)
        recorder = TrajectoryRecorder(
            self.journal, session.id, journal=self.journal,
            snapshot_provider=session.to_snapshot,
        )
        runtime = AgentRuntime(
            session=session, backend=ScriptedBackend([{"final": "done"}]),
            context_builder=context_builder, registry=host.registry, harness=host.harness,
            workspace_manager=host.workspace_manager, recorder=recorder,
        )
        result = coordinator.run_execution(
            ResumeExecutionCommand(admission.operation_id, admission.conversation_id), runtime,
        )
        self.assertIs(result.state, RuntimeState.COMPLETED, result.failure)
        self.assertTrue(context_builder.seen)
        self.assertEqual(1, context_builder.seen[-1].count("typed steering"))
        self.assertEqual("consumed", self.journal.connection.execute(
            "SELECT status FROM product_inputs WHERE operation_id='typed-steering'"
        ).fetchone()[0])
        self.assertEqual(1, self.journal.connection.execute(
            "SELECT COUNT(*) FROM turn_finalizations WHERE turn_id=?", (admission.turn_id,)
        ).fetchone()[0])
        self.assertIsNone(self.journal.connection.execute(
            "SELECT open_turn_id FROM conversations WHERE conversation_id=?",
            (admission.conversation_id,),
        ).fetchone()[0])

    def test_concurrent_runtime_boundary_consumes_current_ordered_batch_without_failure(self) -> None:
        admission = self._admit(operation="concurrent-runtime", session="concurrent-runtime-session")
        coordinator = ConversationApplicationCoordinator(self.journal)
        consumer_entered = threading.Event()
        producer_done = threading.Event()
        producer_errors: list[BaseException] = []
        original_consume = self.journal.consume_current_steering_inputs

        def synchronized_consume(*, conversation_id: str, legacy_session_id: str) -> list[str]:
            consumer_entered.set()
            if not producer_done.wait(timeout=5):
                raise RuntimeError("producer did not reach the safe-boundary race")
            return original_consume(
                conversation_id=conversation_id, legacy_session_id=legacy_session_id,
            )

        self.journal.consume_current_steering_inputs = synchronized_consume  # type: ignore[method-assign]

        def produce() -> None:
            try:
                if not consumer_entered.wait(timeout=5):
                    raise RuntimeError("Runtime did not reach the safe boundary")
                for ordinal in (1, 2):
                    version = self._conversation_version(admission.conversation_id)
                    payload = {"text": f"concurrent steering {ordinal}"}
                    self.journal.append_product_input(
                        operation_id=f"concurrent-steering-{ordinal}",
                        payload_digest=self._input_digest(
                            admission.conversation_id, "steering", payload, version=version,
                        ),
                        conversation_id=admission.conversation_id,
                        input_kind="steering", payload=payload,
                        expected_conversation_version=version,
                    )
            except BaseException as exc:
                producer_errors.append(exc)
            finally:
                producer_done.set()

        producer = threading.Thread(target=produce)
        producer.start()

        class RecordingContextBuilder:
            def __init__(self) -> None:
                self.delegate = BudgetedContextBuilder()
                self.seen: list[list[str]] = []

            def build(self, request):
                self.seen.append([message.content for message in request.messages])
                return self.delegate.build(request)

        context_builder = RecordingContextBuilder()
        host = AgentApplication(
            Path(self.tmp.name) / "concurrent-product-host", journal=self.journal,
            event_store=self.journal, context_builder=context_builder,
        )
        session = self.journal.load_session(admission.legacy_session_id)
        runtime = AgentRuntime(
            session=session, backend=ScriptedBackend([{"final": "done"}]),
            context_builder=context_builder, registry=host.registry, harness=host.harness,
            workspace_manager=host.workspace_manager,
            recorder=TrajectoryRecorder(
                self.journal, session.id, journal=self.journal,
                snapshot_provider=session.to_snapshot,
            ),
        )
        result = coordinator.run_execution(
            ResumeExecutionCommand(admission.operation_id, admission.conversation_id), runtime,
        )
        producer.join(timeout=5)
        self.assertFalse(producer.is_alive())
        self.assertEqual([], producer_errors)
        self.assertIs(result.state, RuntimeState.COMPLETED, result.failure)
        self.assertNotEqual(RuntimeState.FAILED, self.journal.load_session(admission.legacy_session_id).state)
        visible = context_builder.seen[-1]
        for ordinal in (1, 2):
            text = f"concurrent steering {ordinal}"
            self.assertEqual(1, visible.count(text))
        statuses = self.journal.connection.execute(
            "SELECT operation_id, status FROM product_inputs WHERE operation_id LIKE 'concurrent-steering-%' ORDER BY sequence"
        ).fetchall()
        self.assertEqual(
            [("concurrent-steering-1", "consumed"), ("concurrent-steering-2", "consumed")],
            [(row["operation_id"], row["status"]) for row in statuses],
        )

    def test_input_terminal_race_has_one_durable_order_and_no_dangling_acceptance(self) -> None:
        admission = self._admit(operation="race-admit", session="race-session")
        version = self._conversation_version(admission.conversation_id)
        gate = threading.Barrier(2)
        outcomes: list[str] = []

        def accept_input() -> None:
            gate.wait()
            try:
                self.journal.append_product_input(
                    operation_id="race-steering",
                    payload_digest=self._input_digest(
                        admission.conversation_id, "steering", {"text": "race"}, version=version,
                    ),
                    conversation_id=admission.conversation_id, input_kind="steering",
                    payload={"text": "race"}, expected_conversation_version=version,
                )
                outcomes.append("input-first")
            except ProductLifecycleConflict:
                outcomes.append("terminal-first")

        def finish_turn() -> None:
            gate.wait()
            with self.journal._lock, self.journal._write_transaction():  # type: ignore[attr-defined]
                self.journal._transition_admitted_session_in_transaction(  # type: ignore[attr-defined]
                    admission.legacy_session_id, RuntimeState.CANCELLED, "race-terminal", {},
                )

        threads = [threading.Thread(target=accept_input), threading.Thread(target=finish_turn)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(1, len(outcomes))
        row = self.journal.connection.execute(
            "SELECT status FROM product_inputs WHERE operation_id='race-steering'"
        ).fetchone()
        if outcomes == ["input-first"]:
            self.assertEqual("closed_terminal_unconsumed", row["status"])
        else:
            self.assertIsNone(row)
        self.assertEqual(1, self.journal.connection.execute(
            "SELECT COUNT(*) FROM turn_finalizations WHERE turn_id=?", (admission.turn_id,)
        ).fetchone()[0])

    def test_concurrent_admission_and_post_admission_failure_are_recoverable(self) -> None:
        """Separate SQLite connections serialize an operation and retain failure evidence."""
        second = SQLiteRunJournal(Path(self.tmp.name) / "state.db")
        results: list[object] = []
        errors: list[Exception] = []
        start = threading.Barrier(2)
        def submit(journal: SQLiteRunJournal) -> None:
            try:
                start.wait()
                results.append(journal.admit_turn(
                    operation_id="concurrent", payload_digest=journal.canonical_admission_digest(
                        initial_request="read", repository_id=self.direct.repository_id,
                        project_scope_id=self.direct.project_scope.project_scope_id,
                        workspace_binding_id=self.direct.binding.workspace_binding_id,
                        snapshot=self._snapshot("concurrent-session"), policy=RunPolicy(),
                        conversation_id=None, expected_conversation_version=None,
                    ), initial_request="read",
                    repository_id=self.direct.repository_id,
                    project_scope_id=self.direct.project_scope.project_scope_id,
                    workspace_binding_id=self.direct.binding.workspace_binding_id,
                    snapshot=self._snapshot("concurrent-session"), policy=RunPolicy(),
                ))
            except Exception as error:  # pragma: no cover - assertion below reports it
                errors.append(error)
        left = threading.Thread(target=submit, args=(self.journal,))
        right = threading.Thread(target=submit, args=(second,))
        left.start()
        right.start()
        left.join()
        right.join()
        self.assertFalse(errors)
        self.assertEqual(2, len(results))
        self.assertEqual(results[0].turn_id, results[1].turn_id)  # type: ignore[attr-defined]
        self.journal.mark_admitted_initialization_failed("concurrent", RuntimeError("no provider init"))
        session = self.journal.load_session("concurrent-session")
        self.assertIs(session.state, RuntimeState.FAILED)
        self.assertEqual(1, self.journal.connection.execute("SELECT COUNT(*) FROM turn_finalizations").fetchone()[0])
        second.close()

    def test_distinct_operations_contend_for_one_existing_conversation_turn(self) -> None:
        """Different operation IDs race; exactly one may publish the open Turn."""
        seed = self._admit("seed", session="seed-session")
        self.journal.connection.execute("UPDATE sessions SET state='completed' WHERE id=?", (seed.legacy_session_id,))
        self.journal.finalize_admitted_turn(seed.operation_id)
        second = SQLiteRunJournal(Path(self.tmp.name) / "state.db")
        start = threading.Barrier(2)
        results: list[object] = []
        errors: list[Exception] = []

        def submit(journal: SQLiteRunJournal, operation: str) -> None:
            snapshot = self._snapshot(f"{operation}-session")
            try:
                start.wait()
                results.append(journal.admit_turn(
                    operation_id=operation, payload_digest=journal.canonical_admission_digest(
                        initial_request=operation, repository_id=self.direct.repository_id,
                        project_scope_id=self.direct.project_scope.project_scope_id,
                        workspace_binding_id=self.direct.binding.workspace_binding_id, snapshot=snapshot,
                        policy=RunPolicy(), conversation_id=seed.conversation_id, expected_conversation_version=2,
                    ), initial_request=operation, repository_id=self.direct.repository_id,
                    project_scope_id=self.direct.project_scope.project_scope_id,
                    workspace_binding_id=self.direct.binding.workspace_binding_id, snapshot=snapshot,
                    policy=RunPolicy(), conversation_id=seed.conversation_id, expected_conversation_version=2,
                ))
            except Exception as error:
                errors.append(error)

        left = threading.Thread(target=submit, args=(self.journal, "distinct-left"))
        right = threading.Thread(target=submit, args=(second, "distinct-right"))
        left.start()
        right.start()
        left.join()
        right.join()
        self.assertEqual(1, len(results))
        self.assertEqual(1, len(errors))
        self.assertIsInstance(errors[0], ProductLifecycleConflict)
        self.assertEqual(1, self.journal.connection.execute(
            """SELECT COUNT(*) FROM turns
               LEFT JOIN turn_finalizations USING(turn_id)
               WHERE conversation_id=? AND turn_finalizations.turn_id IS NULL""", (seed.conversation_id,)
        ).fetchone()[0])
        second.close()

    def test_post_admission_initialization_failure_and_finalization_share_one_transaction(self) -> None:
        """A crash at the enclosing commit leaves neither terminal projection visible."""
        admission = self._admit()

        def fail_before_commit(point: str) -> None:
            if point == "before_commit":
                raise RuntimeError("crash before atomic finalization commit")

        self.journal.commit_hook = fail_before_commit
        with self.assertRaisesRegex(RuntimeError, "atomic finalization"):
            self.journal.mark_admitted_initialization_failed(admission.operation_id, RuntimeError("init"))
        self.assertIs(self.journal.load_session(admission.legacy_session_id).state, RuntimeState.CREATED)
        self.assertEqual(0, self.journal.connection.execute("SELECT COUNT(*) FROM turn_finalizations").fetchone()[0])
        self.assertEqual(admission.turn_id, self.journal.connection.execute(
            "SELECT open_turn_id FROM conversations WHERE conversation_id=?", (admission.conversation_id,)
        ).fetchone()[0])

        self.journal.commit_hook = None
        self.journal.mark_admitted_initialization_failed(admission.operation_id, RuntimeError("init"))
        self.assertIs(self.journal.load_session(admission.legacy_session_id).state, RuntimeState.FAILED)
        self.assertEqual(1, self.journal.connection.execute("SELECT COUNT(*) FROM turn_finalizations").fetchone()[0])

    def test_ordered_controls_expiry_barrier_and_linked_worktree_observation(self) -> None:
        admission = self._admit()
        self.journal.append_product_input(
            operation_id="steering-operation", payload_digest=self._input_digest(
                admission.conversation_id, "steering", {"ordinal": 1},
            ), conversation_id=admission.conversation_id, input_kind="steering", payload={"ordinal": 1},
            expected_conversation_version=self._conversation_version(admission.conversation_id),
        )
        self.journal.request_ordinary_input(
            operation_id="ordinary-request-operation", payload_digest=self._input_digest(
                admission.conversation_id, "ordinary_input_request", {"question": "continue?"}, "ordinary-request-1",
            ),
            conversation_id=admission.conversation_id, request_id="ordinary-request-1", payload={"question": "continue?"},
            expected_conversation_version=self._conversation_version(admission.conversation_id),
        )
        for ordinal, kind in enumerate(("reply", "cancel"), start=2):
            self.journal.append_product_input(
                operation_id=f"{kind}-operation", payload_digest=self._input_digest(
                    admission.conversation_id, kind, {"ordinal": ordinal},
                    "ordinary-request-1" if kind == "reply" else None,
                ),
                conversation_id=admission.conversation_id, input_kind=kind,
                payload={"ordinal": ordinal}, correlation_id="ordinary-request-1" if kind == "reply" else None,
                expected_conversation_version=self._conversation_version(admission.conversation_id),
            )
        self.assertEqual(
            ["initial_request", "steering", "ordinary_input_request", "reply", "cancel"],
            [row[0] for row in self.journal.connection.execute(
                "SELECT input_kind FROM product_inputs WHERE conversation_id=? ORDER BY sequence", (admission.conversation_id,)
            )],
        )
        self.assertEqual("resolved", self.journal.connection.execute(
            "SELECT status FROM product_inputs WHERE input_id='ordinary-request-1'"
        ).fetchone()[0])
        # A terminal/finalized Turn rejects a late control rather than racing a
        # plausible pending input into the next Turn.
        self.journal.apply_cancel_at_safe_boundary(
            conversation_id=admission.conversation_id, legacy_session_id=admission.legacy_session_id,
            operation_id="cancel-operation", expected_conversation_version=self._conversation_version(admission.conversation_id),
        )
        self.assertIs(self.journal.load_session(admission.legacy_session_id).state, RuntimeState.CANCELLED)
        with self.assertRaises(ProductLifecycleConflict):
            self.journal.append_product_input(
                operation_id="late", payload_digest=self._input_digest(admission.conversation_id, "reply", {}, None), conversation_id=admission.conversation_id,
                input_kind="reply", payload={}, expected_conversation_version=self._conversation_version(admission.conversation_id),
            )
        another_root = Path(self.tmp.name) / "another"
        another_root.mkdir()
        rebound = self.journal.register_workspace_binding(
            repository_id=self.direct.repository_id, project_scope_id=self.direct.project_scope.project_scope_id,
            binding_kind="m2_direct_read_only", locator=str(another_root),
            observation=observe_direct_workspace(another_root).to_dict(),
        )
        now = [10.0]
        self.journal.lease_clock = lambda: now[0]
        claim = self._claim(rebound.workspace_binding_id, "writer", operation="expiry-writer", lease_seconds=1)
        now[0] = 12.0
        with self.assertRaises(LeaseConflict):
            self._claim(rebound.workspace_binding_id, "new", operation="expiry-new", expected_epoch=1, lease_seconds=1)
        barrier_id = self.journal.connection.execute(
            "SELECT barrier_id FROM workspace_recovery_barriers WHERE status='active'"
        ).fetchone()[0]
        self._release(claim, operation="release-expired")
        with self.assertRaises(LeaseConflict):
            self._claim(rebound.workspace_binding_id, "new", operation="expiry-new-2", expected_epoch=1, lease_seconds=1)
        with self.assertRaises(LeaseConflict):
            self.journal.resolve_expired_writer_barrier(
                barrier_id=barrier_id, claim_id=claim.claim_id, reconciliation_digest="arbitrary",
            )
        self.journal.resolve_expired_writer_barrier(
            barrier_id=barrier_id, claim_id=claim.claim_id,
            reconciliation_digest=self.journal.writer_expiry_reconciliation_digest(claim.claim_id),
        )
        self._claim(rebound.workspace_binding_id, "new", operation="expiry-new-3", expected_epoch=1, lease_seconds=1)

        common = Path(self.tmp.name) / "common.git"
        (common / "worktrees" / "linked").mkdir(parents=True)
        (common / "refs" / "heads").mkdir(parents=True)
        (common / "refs" / "heads" / "main").write_text("a" * 40 + "\n", encoding="ascii")
        (common / "worktrees" / "linked" / "commondir").write_text("../..", encoding="utf-8")
        (common / "worktrees" / "linked" / "HEAD").write_text(
            "ref: refs/heads/main\n", encoding="utf-8"
        )
        linked = Path(self.tmp.name) / "linked"
        linked.mkdir()
        (linked / ".git").write_text(f"gitdir: {common / 'worktrees' / 'linked'}\n", encoding="utf-8")
        main = Path(self.tmp.name) / "main"
        main.mkdir()
        (main / ".git").mkdir()
        # Make main advertise the same common directory in purely local layout facts.
        (main / ".git" / "commondir").write_text(str(common), encoding="utf-8")
        left = discover_direct_workspace(self.journal, linked)
        self.assertNotEqual("unavailable_missing", left.observation.git_facts["branch_tip"])
        first_tip = left.observation.git_facts["branch_tip"]
        (common / "refs" / "heads" / "main").write_text("b" * 40 + "\n", encoding="ascii")
        self.assertNotEqual(first_tip, observe_direct_workspace(linked).git_facts["branch_tip"])
        right = discover_direct_workspace(self.journal, main)
        self.assertEqual(left.repository_id, right.repository_id)

    def test_user_reply_requires_a_matching_outstanding_ordinary_request(self) -> None:
        admission = self._admit()
        with self.assertRaises(ProductLifecycleConflict):
            self.journal.append_product_input(
                operation_id="unsolicited-reply", payload_digest=self._input_digest(
                    admission.conversation_id, "reply", {"text": "not a resolver"}, "missing",
                ), conversation_id=admission.conversation_id,
                input_kind="reply", payload={"text": "not a resolver"}, correlation_id="missing",
                expected_conversation_version=self._conversation_version(admission.conversation_id),
            )

    def test_ordered_input_delivery_is_visible_once_after_reopen(self) -> None:
        admission = self._admit("input-delivery")
        for operation, text in (("steer-1", "first steering"), ("steer-2", "second steering")):
            version = self._conversation_version(admission.conversation_id)
            self.journal.append_product_input(
                operation_id=operation,
                payload_digest=self._input_digest(admission.conversation_id, "steering", {"text": text}, version=version),
                conversation_id=admission.conversation_id, input_kind="steering", payload={"text": text},
                expected_conversation_version=version,
            )
        version = self._conversation_version(admission.conversation_id)
        self.assertEqual(2, len(self.journal.consume_steering_inputs(
            conversation_id=admission.conversation_id, legacy_session_id=admission.legacy_session_id,
            expected_conversation_version=version,
        )))
        self.assertEqual(["inspect only", "first steering", "second steering"], [
            message.content for message in self.journal.list_messages(admission.legacy_session_id)
        ])
        self.assertEqual([], self.journal.consume_steering_inputs(
            conversation_id=admission.conversation_id, legacy_session_id=admission.legacy_session_id,
            expected_conversation_version=version,
        ))
        self.journal.close()
        self.journal = SQLiteRunJournal(Path(self.tmp.name) / "state.db")
        self.assertEqual(["inspect only", "first steering", "second steering"], [
            message.content for message in self.journal.list_messages(admission.legacy_session_id)
        ])

        version = self._conversation_version(admission.conversation_id)
        self.journal.request_ordinary_input(
            operation_id="ask", payload_digest=self._input_digest(
                admission.conversation_id, "ordinary_input_request", {"question": "continue"}, "question-1", version,
            ), conversation_id=admission.conversation_id, request_id="question-1", payload={"question": "continue"},
            expected_conversation_version=version,
        )
        version = self._conversation_version(admission.conversation_id)
        self.journal.append_product_input(
            operation_id="reply", payload_digest=self._input_digest(
                admission.conversation_id, "reply", {"text": "yes"}, "question-1", version,
            ), conversation_id=admission.conversation_id, input_kind="reply", payload={"text": "yes"},
            correlation_id="question-1", expected_conversation_version=version,
        )
        self.assertEqual("yes", self.journal.list_messages(admission.legacy_session_id)[-1].content)

    def test_execution_resume_revalidates_binding_and_reopen_repairs_terminal_projection(self) -> None:
        admission = self._admit("resume-validation")
        self.assertEqual(admission.turn_id, self.journal.resume_execution(admission.operation_id).turn_id)
        (self.root / "sentinel.txt").write_text("same-size", encoding="utf-8")
        # Ordinary content drift is observed and invalidates the prior
        # frontier; it does not silently change the immutable binding.
        self.assertEqual(admission.turn_id, self.journal.resume_execution(admission.operation_id).turn_id)
        self.assertGreaterEqual(2, self.journal.connection.execute(
            "SELECT COUNT(*) FROM workspace_binding_observations WHERE workspace_binding_id=?",
            (self.direct.binding.workspace_binding_id,),
        ).fetchone()[0])

        # A terminal Runtime row is authority; a crash before Product
        # finalization is repaired automatically on open, not by a caller.
        self.journal.connection.execute(
            "UPDATE sessions SET state='completed' WHERE id=?", (admission.legacy_session_id,)
        )
        database = Path(self.tmp.name) / "state.db"
        self.journal.close()
        self.journal = SQLiteRunJournal(database)
        self.assertEqual(1, self.journal.connection.execute(
            "SELECT COUNT(*) FROM turn_finalizations WHERE turn_id=?", (admission.turn_id,)
        ).fetchone()[0])

    def test_uncertain_runtime_call_barrier_resolves_or_repairs_after_seam_crash(self) -> None:
        """A real tool row, not a caller token, controls the Product barrier."""
        app_home = Path(self.tmp.name) / "app-home"
        application = AgentApplication(app_home)
        journal = application.journal
        assert isinstance(journal, SQLiteRunJournal)
        binding = discover_direct_workspace(journal, self.root)
        snapshot = RuntimeSnapshot(
            session_id="barrier-runtime", task="uncertain call", source_path=str(self.root),
            state=RuntimeState.CREATED, policy=RunPolicy(), source_fingerprint="m2-source",
        )
        digest = journal.canonical_admission_digest(
            initial_request="uncertain call", repository_id=binding.repository_id,
            project_scope_id=binding.project_scope.project_scope_id,
            workspace_binding_id=binding.binding.workspace_binding_id, snapshot=snapshot,
            policy=RunPolicy(), conversation_id=None, expected_conversation_version=None,
        )
        admission = journal.admit_turn(
            operation_id="barrier-admit", payload_digest=digest, initial_request="uncertain call",
            repository_id=binding.repository_id, project_scope_id=binding.project_scope.project_scope_id,
            workspace_binding_id=binding.binding.workspace_binding_id, snapshot=snapshot, policy=RunPolicy(),
        )
        for target in (RuntimeState.PREPARING_WORKSPACE, RuntimeState.BUILDING_CONTEXT,
                       RuntimeState.CALLING_MODEL, RuntimeState.DISPATCHING_TOOL):
            journal._transition_admitted_session_in_transaction  # type: ignore[attr-defined]
            with journal._lock, journal._write_transaction():  # type: ignore[attr-defined]
                journal._transition_admitted_session_in_transaction(  # type: ignore[attr-defined]
                    admission.legacy_session_id, target, "test_setup", {},
                )
        before = journal.load_snapshot(admission.legacy_session_id)
        waiting = replace(before, state=RuntimeState.WAITING_APPROVAL, active_call_id="uncertain-call", active_call_kind="tool")
        journal.commit(JournalMutation(
            session_id=admission.legacy_session_id, expected_version=before.version,
            expected_state=RuntimeState.DISPATCHING_TOOL, snapshot_after=waiting,
            event_type=EventType.TOOL_CALL_UNCERTAIN, payload={"call_id": "uncertain-call"},
            tool_call=ToolCallMutation(
                call_id="uncertain-call", ordinal=1, tool_name="edit_file", arguments={"path": "sentinel.txt"},
                recovery_mode=RecoveryMode.RECONCILABLE_WRITE, status=ToolCallState.UNCERTAIN,
            ),
        ))
        evidence = journal._runtime_uncertain_invocation_evidence_in_transaction(  # type: ignore[attr-defined]
            binding.binding.workspace_binding_id, "uncertain-call",
        )
        self.assertEqual("active", journal.connection.execute(
            "SELECT status FROM workspace_recovery_barriers WHERE workspace_binding_id=? AND uncertain_invocation_id=?",
            (binding.binding.workspace_binding_id, "uncertain-call"),
        ).fetchone()[0])
        barrier_digest = journal.canonical_unknown_effect_barrier_digest(
            workspace_binding_id=binding.binding.workspace_binding_id, uncertain_invocation_id="uncertain-call",
            resolver_kind="call_resolved", evidence_digest=evidence,
        )
        barrier_id = journal.record_unknown_effect_recovery_barrier(
            operation_id="barrier-record", payload_digest=barrier_digest,
            workspace_binding_id=binding.binding.workspace_binding_id, uncertain_invocation_id="uncertain-call",
            resolver_kind="call_resolved", evidence_digest=evidence,
        )
        # Databases produced by the first v6 implementation used this exact
        # legacy tuple; it must resolve through the retained call resolver.
        journal.connection.execute(
            """UPDATE workspace_recovery_barriers
               SET reason='legacy_waiting_approval_reconciliation',
                   resolver_kind='legacy_uncertain_call_resolver'
               WHERE barrier_id=?""",
            (barrier_id,),
        )
        original_repair = journal.repair_resolved_recovery_barriers_after_restart
        journal.repair_resolved_recovery_barriers_after_restart = lambda: (_ for _ in ()).throw(RuntimeError("seam crash"))  # type: ignore[method-assign]
        with self.assertRaisesRegex(RuntimeError, "seam crash"):
            application.resolve_call(admission.legacy_session_id, "uncertain-call", "effect-not-applied")
        self.assertEqual("active", journal.connection.execute(
            "SELECT status FROM workspace_recovery_barriers WHERE barrier_id=?", (barrier_id,)
        ).fetchone()[0])
        journal.repair_resolved_recovery_barriers_after_restart = original_repair  # type: ignore[method-assign]
        journal.close()
        reopened = SQLiteRunJournal(app_home / "state.db")
        self.assertEqual("cleared", reopened.connection.execute(
            "SELECT status FROM workspace_recovery_barriers WHERE barrier_id=?", (barrier_id,)
        ).fetchone()[0])
        reopened.close()
    def test_unknown_effect_barrier_requires_exact_invocation_resolver_and_evidence(self) -> None:
        binding_id = self.direct.binding.workspace_binding_id
        digest = self.journal.canonical_unknown_effect_barrier_digest(
            workspace_binding_id=binding_id, uncertain_invocation_id="tool-call-1",
            resolver_kind="call_resolved", evidence_digest="evidence-1",
        )
        with self.assertRaisesRegex(LeaseConflict, "real Runtime"):
            self.journal.record_unknown_effect_recovery_barrier(
                operation_id="barrier-op", payload_digest=digest, workspace_binding_id=binding_id,
                uncertain_invocation_id="tool-call-1", resolver_kind="call_resolved", evidence_digest="evidence-1",
            )

    def test_two_terminal_turns_preserve_conversation_and_immutable_bindings(self) -> None:
        first = self._admit()
        self.journal.connection.execute("UPDATE sessions SET state='completed' WHERE id=?", (first.legacy_session_id,))
        self.journal.finalize_admitted_turn(first.operation_id)
        snapshot = self._snapshot("second-session")
        second_digest = self.journal.canonical_admission_digest(
            initial_request="second request", repository_id=self.direct.repository_id,
            project_scope_id=self.direct.project_scope.project_scope_id,
            workspace_binding_id=self.direct.binding.workspace_binding_id, snapshot=snapshot,
            policy=RunPolicy(), conversation_id=first.conversation_id, expected_conversation_version=2,
        )
        second = self.journal.admit_turn(
            operation_id="second-admission", payload_digest=second_digest, initial_request="second request",
            repository_id=self.direct.repository_id, project_scope_id=self.direct.project_scope.project_scope_id,
            workspace_binding_id=self.direct.binding.workspace_binding_id, snapshot=snapshot, policy=RunPolicy(),
            conversation_id=first.conversation_id, expected_conversation_version=2,
        )
        self.assertEqual(first.conversation_id, second.conversation_id)
        self.assertNotEqual(first.runtime_execution_id, second.runtime_execution_id)
        inspection = self.journal.inspect_conversation(first.conversation_id)
        self.assertIsNotNone(inspection)
        assert inspection is not None
        turns = inspection["turns"]
        self.assertIsInstance(turns, list)
        assert isinstance(turns, list)
        self.assertEqual(2, turns[1]["ordinal"])

    def test_writer_renewal_rejects_stale_epoch_and_observation(self) -> None:
        with self.assertRaisesRegex(ValueError, "\(0, 300\]"):
            self._claim(self.direct.binding.workspace_binding_id, "writer", operation="too-long", lease_seconds=301)
        claim = self._claim(self.direct.binding.workspace_binding_id, "writer", operation="acquire")
        observed = self.journal.connection.execute(
            "SELECT observation_digest FROM workspace_binding_observations WHERE workspace_binding_id=? ORDER BY observed_at DESC LIMIT 1",
            (claim.workspace_binding_id,),
        ).fetchone()[0]
        def digest(epoch: int, observation: str) -> str:
            return self.journal.canonical_product_payload_digest({
                "kind": "writer_renew", "claim_id": claim.claim_id, "owner_id": claim.owner_id,
                "expected_epoch": epoch, "expected_observation_digest": observation, "lease_seconds": 60,
            })
        with self.assertRaises(LeaseConflict):
            self.journal.renew_workspace_writer(
                operation_id="stale", payload_digest=digest(0, observed), claim_id=claim.claim_id,
                owner_id=claim.owner_id, expected_epoch=0, expected_observation_digest=observed, lease_seconds=60,
            )
        renewed = self.journal.renew_workspace_writer(
            operation_id="renew", payload_digest=digest(1, observed), claim_id=claim.claim_id,
            owner_id=claim.owner_id, expected_epoch=1, expected_observation_digest=observed, lease_seconds=60,
        )
        self.assertEqual(claim.claim_epoch, renewed.claim_epoch)
        self.assertEqual(renewed.claim_id, self.journal.renew_workspace_writer(
            operation_id="renew", payload_digest=digest(1, observed), claim_id=claim.claim_id,
            owner_id=claim.owner_id, expected_epoch=1, expected_observation_digest=observed, lease_seconds=60,
        ).claim_id)
        self._release(claim, operation="release-once")
        self._release(claim, operation="release-once")
        (self.root / "sentinel.txt").write_text("equal-size", encoding="utf-8")
        with self.assertRaisesRegex(LeaseConflict, "live drift"):
            self._claim(self.direct.binding.workspace_binding_id, "next", operation="drifted", expected_epoch=1)

    def test_execution_owned_writer_is_released_at_terminal_and_stable_restart(self) -> None:
        admission = self._admit()
        claim = self._claim(
            self.direct.binding.workspace_binding_id, "writer", operation="owned-terminal",
            runtime_execution_id=admission.runtime_execution_id,
        )
        with self.journal._lock, self.journal._write_transaction():  # type: ignore[attr-defined]
            self.journal._transition_admitted_session_in_transaction(  # type: ignore[attr-defined]
                admission.legacy_session_id, RuntimeState.CANCELLED, "terminal-test", {},
            )
        self.assertEqual("released", self.journal.connection.execute(
            "SELECT status FROM workspace_writer_claims WHERE claim_id=?", (claim.claim_id,)
        ).fetchone()[0])
        self.assertEqual(1, self.journal.connection.execute(
            "SELECT COUNT(*) FROM turn_finalizations WHERE turn_id=?", (admission.turn_id,)
        ).fetchone()[0])

        second = self._admit(operation="admit-stable", session="m2-stable")
        stable_claim = self._claim(
            self.direct.binding.workspace_binding_id, "writer-stable", operation="owned-stable",
            expected_epoch=1, runtime_execution_id=second.runtime_execution_id,
        )
        self.journal.connection.execute(
            """INSERT INTO workspace_recovery_barriers(
               barrier_id, workspace_binding_id, claim_id, reason, uncertain_invocation_id,
               resolver_kind, evidence_digest, resolved_by_operation_id, status,
               reconciliation_token, created_at, cleared_at)
               VALUES ('stable-independent-barrier', ?, NULL, 'test_independent',
                       'stable-independent', 'test', 'test', NULL, 'active', NULL, ?, NULL)""",
            (self.direct.binding.workspace_binding_id, self.journal.clock()),
        )
        with self.journal._lock, self.journal._write_transaction():  # type: ignore[attr-defined]
            self.journal._transition_admitted_session_in_transaction(  # type: ignore[attr-defined]
                second.legacy_session_id, RuntimeState.WAITING_USER_INPUT, "stable-wait-test", {},
            )
        self.assertEqual("released", self.journal.connection.execute(
            "SELECT status FROM workspace_writer_claims WHERE claim_id=?", (stable_claim.claim_id,)
        ).fetchone()[0])
        self.assertEqual("active", self.journal.connection.execute(
            "SELECT status FROM workspace_recovery_barriers WHERE barrier_id='stable-independent-barrier'"
        ).fetchone()[0])
        database = Path(self.tmp.name) / "state.db"
        self.journal.close()
        reopened = SQLiteRunJournal(database)
        self.assertEqual("released", reopened.connection.execute(
            "SELECT status FROM workspace_writer_claims WHERE claim_id=?", (stable_claim.claim_id,)
        ).fetchone()[0])
        reopened.close()
        self.journal = SQLiteRunJournal(database)
        self.journal.connection.execute(
            "UPDATE workspace_recovery_barriers SET status='cleared' WHERE barrier_id='stable-independent-barrier'"
        )

        interrupted = self._admit(operation="admit-interrupted", session="m2-interrupted")
        interrupted_claim = self._claim(
            self.direct.binding.workspace_binding_id, "writer-interrupted", operation="owned-interrupted",
            expected_epoch=2, runtime_execution_id=interrupted.runtime_execution_id,
        )
        with self.journal._lock, self.journal._write_transaction():  # type: ignore[attr-defined]
            self.journal._transition_admitted_session_in_transaction(  # type: ignore[attr-defined]
                interrupted.legacy_session_id, RuntimeState.INTERRUPTED, "interrupt-test", {},
            )
        self.assertEqual("released", self.journal.connection.execute(
            "SELECT status FROM workspace_writer_claims WHERE claim_id=?", (interrupted_claim.claim_id,)
        ).fetchone()[0])
        self.journal.close()
        reopened = SQLiteRunJournal(database)
        self.assertEqual("released", reopened.connection.execute(
            "SELECT status FROM workspace_writer_claims WHERE claim_id=?", (interrupted_claim.claim_id,)
        ).fetchone()[0])
        reopened.close()
        self.journal = SQLiteRunJournal(database)

    def test_direct_observation_captures_directory_symlink_git_and_monorepo_scope(self) -> None:
        repository = Path(self.tmp.name) / "repository"
        project = repository / "packages" / "widget"
        project.mkdir(parents=True)
        (repository / ".git" / "refs" / "heads").mkdir(parents=True)
        (repository / ".git" / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
        (repository / ".git" / "refs" / "heads" / "main").write_text("a" * 40 + "\n", encoding="ascii")
        (repository / ".git" / "index").write_bytes(b"index-one")
        (project / "empty").mkdir()
        (project / "payload").write_text("same-size", encoding="utf-8")
        os.symlink("payload", project / "linked-payload")
        first = observe_direct_workspace(project)
        self.assertEqual(str(repository.resolve()), first.repository_root)
        self.assertEqual("complete_bounded_filesystem_observation", first.observation_frontier)
        self.assertNotEqual("unavailable_no_git_process", first.git_facts["branch_head"])
        self.assertNotEqual("unavailable_no_git_process", first.git_facts["branch_tip"])
        self.assertNotEqual("unavailable_no_git_process", first.git_facts["index"])
        self.assertEqual("unavailable_no_git_process", first.git_facts["dirty"])
        (project / "payload").write_text("drift-size", encoding="utf-8")
        self.assertNotEqual(first.directory_digest, observe_direct_workspace(project).directory_digest)
        (repository / ".git" / "refs" / "heads" / "main").write_text("b" * 40 + "\n", encoding="ascii")
        branch_changed = observe_direct_workspace(project)
        self.assertNotEqual(first.git_facts["branch_tip"], branch_changed.git_facts["branch_tip"])
        (repository / ".git" / "index").write_bytes(b"index-two")
        self.assertNotEqual(branch_changed.git_facts["index"], observe_direct_workspace(project).git_facts["index"])
        os.unlink(project / "linked-payload")
        os.symlink("empty", project / "linked-payload")
        self.assertNotEqual(first.directory_digest, observe_direct_workspace(project).directory_digest)
        (project / "too-large.bin").write_bytes(b"x" * (8 * 1024 * 1024 + 1))
        incomplete = observe_direct_workspace(project)
        self.assertEqual("incomplete_bound_exhausted", incomplete.observation_frontier)
        self.assertIn("too-large.bin", incomplete.exclusions)
        (project / "too-large.bin").unlink()
        discovered = discover_direct_workspace(self.journal, project)
        self.assertEqual("packages/widget", discovered.project_scope.relative_path)
        root_binding = discover_direct_workspace(self.journal, repository)
        self.assertEqual(discovered.repository_id, root_binding.repository_id)
        with self.assertRaises(ProductLifecycleConflict):
            observe_direct_workspace(project / "missing")

    def test_checked_in_manifest_surface_is_unchanged_across_success_failure_crash_restart(self) -> None:
        repository = Path(self.tmp.name) / "manifest-repository"
        (repository / ".git" / "refs" / "heads").mkdir(parents=True)
        (repository / ".git" / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
        (repository / ".git" / "refs" / "heads" / "main").write_text("a" * 40 + "\n", encoding="ascii")
        (repository / ".git" / "index").write_bytes(b"index")
        (repository / "empty").mkdir()
        (repository / "payload.txt").write_text("payload", encoding="utf-8")
        os.symlink("payload.txt", repository / "payload-link")
        before = capture_direct_tree_manifest(repository)
        database = Path(self.tmp.name) / "manifest-state.db"
        journal = SQLiteRunJournal(database)
        direct = discover_direct_workspace(journal, repository)

        def snapshot(name: str) -> RuntimeSnapshot:
            return RuntimeSnapshot(
                session_id=name, task="manifest", source_path=str(repository),
                state=RuntimeState.CREATED, policy=RunPolicy(), source_fingerprint="manifest",
            )

        for operation, failure in (("failure", RuntimeError("failure")), ("crash", KeyboardInterrupt())):
            candidate = snapshot(operation)
            digest = journal.canonical_admission_digest(
                initial_request="read", repository_id=direct.repository_id,
                project_scope_id=direct.project_scope.project_scope_id,
                workspace_binding_id=direct.binding.workspace_binding_id,
                snapshot=candidate, policy=RunPolicy(), conversation_id=None,
                expected_conversation_version=None,
            )
            with self.assertRaises(type(failure)):
                journal.admit_turn(
                    operation_id=operation, payload_digest=digest, initial_request="read",
                    repository_id=direct.repository_id,
                    project_scope_id=direct.project_scope.project_scope_id,
                    workspace_binding_id=direct.binding.workspace_binding_id,
                    snapshot=candidate, policy=RunPolicy(),
                    fault_hook=lambda point, error=failure: (_ for _ in ()).throw(error)
                    if point == "after_checkpoint" else None,
                )
            self.assertEqual(before, capture_direct_tree_manifest(repository))
        journal.close()
        reopened = SQLiteRunJournal(database)
        rediscovered = discover_direct_workspace(reopened, repository)
        successful = snapshot("success")
        successful_digest = reopened.canonical_admission_digest(
            initial_request="read", repository_id=rediscovered.repository_id,
            project_scope_id=rediscovered.project_scope.project_scope_id,
            workspace_binding_id=rediscovered.binding.workspace_binding_id,
            snapshot=successful, policy=RunPolicy(), conversation_id=None,
            expected_conversation_version=None,
        )
        reopened.admit_turn(
            operation_id="success", payload_digest=successful_digest, initial_request="read",
            repository_id=rediscovered.repository_id,
            project_scope_id=rediscovered.project_scope.project_scope_id,
            workspace_binding_id=rediscovered.binding.workspace_binding_id,
            snapshot=successful, policy=RunPolicy(),
        )
        reopened.close()
        self.assertEqual(before, capture_direct_tree_manifest(repository))

    def test_observation_entry_bound_counts_empty_directories_and_symlink_directories(self) -> None:
        empty_root = Path(self.tmp.name) / "many-empty-directories"
        empty_root.mkdir()
        for ordinal in range(10_001):
            (empty_root / f"entry-{ordinal:05d}").mkdir()
        empty_observation = observe_direct_workspace(empty_root)
        self.assertEqual("incomplete_bound_exhausted", empty_observation.observation_frontier)
        self.assertEqual(10_001, empty_observation.observed_entries)
        self.assertEqual("entry_bound_exceeded", empty_observation.exclusions["entry-10000"])

        symlink_root = Path(self.tmp.name) / "symlink-directory-bound"
        symlink_root.mkdir()
        target = Path(self.tmp.name) / "external-directory-target"
        target.mkdir()
        for ordinal in range(10_000):
            (symlink_root / f"entry-{ordinal:05d}").mkdir()
        os.symlink(target, symlink_root / "zzzzz-symlink-directory")
        symlink_observation = observe_direct_workspace(symlink_root)
        self.assertEqual("incomplete_bound_exhausted", symlink_observation.observation_frontier)
        self.assertEqual(10_001, symlink_observation.observed_entries)
        self.assertEqual(
            "entry_bound_exceeded",
            symlink_observation.exclusions["zzzzz-symlink-directory"],
        )

    def test_manifest_detects_directory_metadata_change(self) -> None:
        repository = Path(self.tmp.name) / "directory-metadata-manifest"
        child = repository / "child"
        child.mkdir(parents=True)
        before = capture_direct_tree_manifest(repository)
        child.chmod(0o700)
        after = capture_direct_tree_manifest(repository)
        self.assertNotEqual(before, after)
        before_child = next(entry for entry in before["entries"] if entry["path"] == "child")  # type: ignore[index]
        after_child = next(entry for entry in after["entries"] if entry["path"] == "child")  # type: ignore[index]
        self.assertEqual("directory", before_child["kind"])
        self.assertIn("type_bits", before_child)
        self.assertNotEqual(before_child["mode"], after_child["mode"])

    def test_legacy_waiting_approval_without_call_evidence_fails_closed(self) -> None:
        admission = self._admit()
        self.journal.connection.execute(
            "UPDATE sessions SET state='waiting_approval' WHERE id=?", (admission.legacy_session_id,)
        )
        database = Path(self.tmp.name) / "state.db"
        self.journal.close()
        reopened = SQLiteRunJournal(database)
        self.assertEqual(0, reopened.repair_recovery_barriers_after_restart())
        barrier = reopened.connection.execute(
            """SELECT reason, uncertain_invocation_id, resolver_kind, status
               FROM workspace_recovery_barriers"""
        ).fetchone()
        self.assertEqual("legacy_waiting_approval_incomplete_evidence", barrier["reason"])
        self.assertEqual("unresolvable_fail_closed", barrier["resolver_kind"])
        self.assertEqual("active", barrier["status"])
        with self.assertRaisesRegex(ProductLifecycleConflict, "barrier"):
            reopened.resume_execution(admission.operation_id)
        observation_digest = reopened.connection.execute(
            """SELECT observation_digest FROM workspace_binding_observations
               WHERE workspace_binding_id=? ORDER BY observed_at DESC, rowid DESC LIMIT 1""",
            (self.direct.binding.workspace_binding_id,),
        ).fetchone()[0]
        claim_observation = {"claim": "blocked-by-incomplete-evidence"}
        with self.assertRaisesRegex(LeaseConflict, "barrier"):
            reopened.claim_workspace_writer(
                operation_id="blocked-writer",
                payload_digest=reopened.canonical_writer_claim_digest(
                    workspace_binding_id=self.direct.binding.workspace_binding_id,
                    owner_id="blocked", expected_epoch=0,
                    expected_observation_digest=observation_digest,
                    observation=claim_observation, lease_seconds=60,
                ),
                workspace_binding_id=self.direct.binding.workspace_binding_id,
                owner_id="blocked", expected_epoch=0,
                expected_observation_digest=observation_digest,
                lease_seconds=60, observation=claim_observation,
            )
        reopened.close()
        repaired_again = SQLiteRunJournal(database)
        self.assertEqual(0, repaired_again.repair_recovery_barriers_after_restart())
        self.assertEqual(1, repaired_again.connection.execute(
            "SELECT COUNT(*) FROM workspace_recovery_barriers"
        ).fetchone()[0])
        repaired_again.close()
        self.journal = SQLiteRunJournal(database)


if __name__ == "__main__":
    unittest.main()
