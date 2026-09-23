from __future__ import annotations

import json
import base64
from dataclasses import replace
import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch

from coding_agent.application import AgentApplication
from coding_agent.compression import CompressionEngine
from coding_agent.context import (
    BudgetedContextBuilder,
    ContextBudgetConfig,
    ExactTokenCounter,
    ModelCapability,
    ModelCapabilityRegistry,
)
from coding_agent.domain import BackendError, InvariantViolation, ModelResponse, RunPolicy, RuntimeSnapshot, RuntimeState, Usage
from coding_agent.models.fallback import FallbackBackend
from coding_agent.models.scripted import ScriptedBackend
from coding_agent.persistence import ProductLifecycleConflict, SQLiteRunJournal
from coding_agent.product_application import (
    ConversationApplicationCoordinator,
    InstructionRefreshCommand,
    ResumeExecutionCommand,
    UserReplyCommand,
)
from coding_agent.product_instructions import DiscoveredInstruction, InstructionDiscovery, sha256_bytes
from coding_agent.product_context import (
    build_summary_artifact,
    capture_file_context_item,
    capture_tool_result_artifact,
    CompositionBudget,
    ContextComposer,
    ContextRequiredContentExceedsBudget,
    ContextSource,
    ProductContextSnapshot,
    RuntimeContextSnapshot,
)
from coding_agent.product_domain import SummaryClaim
from coding_agent.product_workspace import capture_direct_tree_manifest, discover_direct_workspace
from coding_agent.runtime import AgentRuntime
from coding_agent.trajectory import TrajectoryRecorder
from coding_agent.workspace import tree_fingerprint


class HostLoss(BaseException):
    pass


class CrashBeforeProvider:
    name = "m3-fake"

    def __init__(self) -> None:
        self.calls = 0
        self.requests = []

    def complete(self, request):
        self.calls += 1
        self.requests.append(request.to_dict())
        return ModelResponse(text="done", usage=Usage(input_tokens=1, output_tokens=1))


class M3ProductContextTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name) / "project"
        self.root.mkdir()
        (self.root / "AGENTS.md").write_text("rule.style: precise\n", encoding="utf-8")
        (self.root / "value.txt").write_text("stable\n", encoding="utf-8")
        self.journal = SQLiteRunJournal(Path(self.temporary.name) / "state.db")
        self.direct = discover_direct_workspace(self.journal, self.root)
        trust_digest = self.journal.canonical_instruction_trust_digest(
            repository_id=self.direct.repository_id,
            project_scope_id=self.direct.project_scope.project_scope_id,
            workspace_binding_id=self.direct.binding.workspace_binding_id,
            source_kind="agents_md", trusted=True,
        )
        self.journal.configure_instruction_trust(
            operation_id="trust", payload_digest=trust_digest,
            repository_id=self.direct.repository_id,
            project_scope_id=self.direct.project_scope.project_scope_id,
            workspace_binding_id=self.direct.binding.workspace_binding_id,
            source_kind="agents_md", trusted=True,
        )

    def tearDown(self) -> None:
        self.journal.close()
        self.temporary.cleanup()

    def _admit(
        self, operation: str = "admit", session_id: str = "m3-session",
        policy: RunPolicy | None = None,
    ):
        selected_policy = policy or RunPolicy()
        snapshot = RuntimeSnapshot(
            session_id=session_id, task="M3 task", source_path=str(self.root),
            state=RuntimeState.CREATED, policy=selected_policy,
            source_fingerprint=tree_fingerprint(self.root),
        )
        digest = self.journal.canonical_admission_digest(
            initial_request="inspect precisely", repository_id=self.direct.repository_id,
            project_scope_id=self.direct.project_scope.project_scope_id,
            workspace_binding_id=self.direct.binding.workspace_binding_id,
            snapshot=snapshot, policy=selected_policy, conversation_id=None,
            expected_conversation_version=None,
        )
        return self.journal.admit_turn(
            operation_id=operation, payload_digest=digest, initial_request="inspect precisely",
            repository_id=self.direct.repository_id,
            project_scope_id=self.direct.project_scope.project_scope_id,
            workspace_binding_id=self.direct.binding.workspace_binding_id,
            snapshot=snapshot, policy=selected_policy,
        )

    def test_composer_is_deterministic_memory_absent_and_required_overflow_is_explicit(self) -> None:
        composer = ContextComposer()
        product = ProductContextSnapshot(
            "system", "runtime", ("intent",), ("instruction",),
            optional_sources=(ContextSource("memory", "memory:1", "must not load"),),
        )
        runtime = RuntimeContextSnapshot(7, tool_schemas=({"name": "read"},))
        budget = CompositionBudget(2048, 128, 32)
        first = composer.compose(
            product, runtime, budget, request_id="request",
            context_manifest_id="manifest", max_output_tokens=128,
        )
        second = composer.compose(
            product, runtime, budget, request_id="request",
            context_manifest_id="manifest", max_output_tokens=128,
        )
        self.assertEqual(first, second)
        self.assertEqual(first.package.memory_status, "absent")
        self.assertEqual(
            [item.reason for item in first.package.selections if item.source_class == "memory"],
            ["memory_absent"],
        )
        with self.assertRaises(ContextRequiredContentExceedsBudget):
            composer.compose(
                product, runtime, CompositionBudget(8, 4, 2), request_id="small",
                context_manifest_id="small-manifest", max_output_tokens=4,
            )

    def test_minimum_reservations_protect_later_classes_and_record_lending(self) -> None:
        product = ProductContextSnapshot(
            "s", "r", ("i",), (), optional_sources=(
                ContextSource("recent_transcript", "recent", "r" * 1600),
                ContextSource("observation", "obs", "o" * 512),
                ContextSource("file", "file", "f" * 512),
                ContextSource("summary", "summary", "u" * 256),
            ),
        )
        composed = ContextComposer().compose(
            product, RuntimeContextSnapshot(1, tool_schemas=()),
            CompositionBudget(512, 0, 0), request_id="reserved-request",
            context_manifest_id="reserved-manifest", max_output_tokens=0,
        )
        dispositions = {item.source_id: item.disposition for item in composed.package.selections}
        self.assertEqual(dispositions["recent"], "omitted")
        self.assertEqual([dispositions[key] for key in ("obs", "file", "summary")],
                         ["included", "included", "included"])
        ledger = composed.manifest_payload["class_ledger"]
        self.assertGreaterEqual(ledger["observation"]["reserved"], 128)
        self.assertGreaterEqual(ledger["file"]["reserved"], 128)
        self.assertGreaterEqual(ledger["summary"]["reserved"], 64)

    def test_exact_overflow_multiple_class_evictions_reconcile_final_ledger(self) -> None:
        class OverheadCounter:
            name = "overhead-v1"

            def count_request(self, provider, model, messages, tools):
                return sum(len(message.content) + 10 for message in messages)

        sources = tuple(
            ContextSource("recent_transcript", f"recent:{index}", "r" * 80,
                          recency=8 - index)
            for index in range(8)
        ) + tuple(
            ContextSource("observation", f"observation:{index}", "o" * 80)
            for index in range(2)
        ) + tuple(
            ContextSource("file", f"file:{index}", "f" * 80)
            for index in range(2)
        ) + (ContextSource("summary", "summary:0", "s" * 80),)
        product = ProductContextSnapshot("s", "r", ("i",), (), optional_sources=sources)
        composer = ContextComposer(token_counter=OverheadCounter())
        composed = composer.compose(
            product, RuntimeContextSnapshot(1), CompositionBudget(700, 0, 0),
            request_id="eviction-request", context_manifest_id="eviction-manifest",
            max_output_tokens=0,
        )
        manifest = composed.manifest_payload
        selections = composed.package.selections
        evicted = [item for item in selections if item.reason == "exact_request_overflow"]
        self.assertGreaterEqual(len(evicted), 3)
        self.assertGreaterEqual(len({item.source_class for item in evicted}), 3)
        included = [item for item in selections if item.source_class != "required"
                    and item.disposition == "included"]
        self.assertEqual(sum(item.token_count for item in included), manifest["optional_tokens"])
        ledger = manifest["class_ledger"]
        remaining = (manifest["context_window"] - manifest["output_reserve"]
                     - manifest["framing_margin"] - manifest["tool_schema_tokens"]
                     - manifest["required_tokens"])
        self.assertEqual(sum(row["spent"] for row in ledger.values())
                         + manifest["unallocated_optional_tokens"], remaining)
        for source_class, row in ledger.items():
            self.assertEqual(row["spent"], row["allocated"])
            self.assertEqual(row["reserved"] + row["lent"] - row["released"], row["spent"])
            self.assertEqual(row["spent"], sum(
                item.token_count for item in included if item.source_class == source_class
            ))
        self.assertGreater(ledger["recent_transcript"]["lent"], 0)
        self.assertGreater(ledger["summary"]["released"], 0)
        self.assertLessEqual(manifest["total_request_tokens"], manifest["context_window"])
        self.assertEqual(composed, composer.compose(
            product, RuntimeContextSnapshot(1), CompositionBudget(700, 0, 0),
            request_id="eviction-request", context_manifest_id="eviction-manifest",
            max_output_tokens=0,
        ))

    def test_exact_overflow_can_release_every_class_reservation(self) -> None:
        class OverheadCounter:
            name = "high-overhead-v1"

            def count_request(self, provider, model, messages, tools):
                return sum(len(message.content) + 20 for message in messages)

        classes = ("recent_transcript", "observation", "file", "summary")
        product = ProductContextSnapshot("s", "r", ("i",), (), optional_sources=tuple(
            ContextSource(source_class, source_class, source_class[0] * 80)
            for source_class in classes
        ))
        composed = ContextComposer(token_counter=OverheadCounter()).compose(
            product, RuntimeContextSnapshot(1), CompositionBudget(100, 0, 0),
            request_id="all-evicted", context_manifest_id="all-evicted-manifest",
            max_output_tokens=0,
        )
        manifest = composed.manifest_payload
        self.assertEqual(manifest["optional_tokens"], 0)
        self.assertEqual({item.source_class for item in composed.package.selections
                          if item.reason == "exact_request_overflow"}, set(classes))
        for row in manifest["class_ledger"].values():
            self.assertEqual(row["spent"], 0)
            self.assertEqual(row["allocated"], 0)
            self.assertEqual(row["lent"], 0)
            self.assertEqual(row["released"], row["reserved"])
        self.assertEqual(manifest["unallocated_optional_tokens"],
                         manifest["context_window"] - manifest["required_tokens"])

    def test_exact_final_message_counter_sees_roles_and_whole_protocol_group_once(self) -> None:
        calls = []

        def count(_provider, _model, messages):
            calls.append(tuple((message.role, message.content, message.tool_call_id)
                               for message in messages))
            return sum(len(message.content) + (20 if message.role == "system" else 7)
                       for message in messages)

        product = ProductContextSnapshot("system", "runtime", ("intent",), ())
        group = (
            {"role": "assistant", "content": "call", "tool_call_id": None,
             "metadata": {"tool_calls": ["a", "b"]}},
            {"role": "tool", "content": "one", "tool_call_id": "a", "metadata": {}},
            {"role": "tool", "content": "two", "tool_call_id": "b", "metadata": {}},
        )
        composer = ContextComposer(
            token_counter=ExactTokenCounter(count), provider="provider", model="model",
            capability_source="test-registry", capability_version="v42",
        )
        composed = composer.compose(
            product, RuntimeContextSnapshot(1, protocol_group=group),
            CompositionBudget(300, 10, 3), request_id="exact-request",
            context_manifest_id="exact-manifest", max_output_tokens=10,
        )
        self.assertEqual(len(calls), 1)
        self.assertEqual([role for role, _, _ in calls[0]],
                         ["system", "system", "user", "assistant", "tool", "tool"])
        self.assertEqual(composed.manifest_payload["total_request_tokens"],
                         sum(len(content) + (20 if role == "system" else 7)
                             for role, content, _ in calls[0]))
        self.assertEqual(composed.manifest_payload["capability_version"], "v42")
        self.assertEqual(composed.manifest_payload["tool_accounting"], "provider_messages_no_tools")
        with self.assertRaises(ContextRequiredContentExceedsBudget):
            composer.compose(
                product, RuntimeContextSnapshot(1, protocol_group=group),
                CompositionBudget(int(composed.manifest_payload["total_request_tokens"]) + 12, 10, 3),
                request_id="exact-overflow", context_manifest_id="exact-overflow-manifest",
                max_output_tokens=10,
            )

    def test_tool_schema_accounting_uses_request_counter_or_named_conservative_fallback(self) -> None:
        product = ProductContextSnapshot("s", "r", ("i",), ())
        runtime = RuntimeContextSnapshot(1, tool_schemas=({"name": "tool", "schema": "x" * 2000},))
        with self.assertRaises(ContextRequiredContentExceedsBudget):
            ContextComposer(token_counter=ExactTokenCounter(lambda *_: 1)).compose(
                product, runtime, CompositionBudget(128, 0, 0),
                request_id="tool-overflow", context_manifest_id="tool-overflow-manifest",
                max_output_tokens=0,
            )

        class RequestCounter:
            name = "request-exact-v1"

            def count_messages(self, provider, model, messages):
                raise AssertionError("whole-request counter must be used")

            def count_request(self, provider, model, messages, tools):
                self.assertion = (provider, model, len(messages), len(tools))
                return 50

        counter = RequestCounter()
        composed = ContextComposer(
            token_counter=counter, provider="provider", model="model",
        ).compose(
            product, runtime, CompositionBudget(128, 0, 0),
            request_id="tool-exact", context_manifest_id="tool-exact-manifest",
            max_output_tokens=0,
        )
        self.assertEqual(counter.assertion, ("provider", "model", 3, 1))
        self.assertEqual(composed.manifest_payload["total_request_tokens"], 50)
        self.assertEqual(composed.manifest_payload["tool_accounting"], "provider_request_counter")

    def test_product_required_overflow_records_failure_without_frozen_request(self) -> None:
        admission = self._admit(operation="overflow-admit", session_id="overflow-session")
        before = capture_direct_tree_manifest(self.root)
        host = AgentApplication(
            Path(self.temporary.name) / "overflow-host", journal=self.journal,
            event_store=self.journal, context_builder=BudgetedContextBuilder(),
        )
        session = self.journal.load_session(admission.legacy_session_id)
        tiny_builder = BudgetedContextBuilder(capability_registry=ModelCapabilityRegistry((
            ModelCapability("scripted", "scripted", 64),
        )))
        runtime = AgentRuntime(
            session=session, backend=ScriptedBackend([{"final": "unreachable"}]),
            context_builder=tiny_builder, registry=host.registry,
            harness=host.harness, workspace_manager=host.workspace_manager,
            recorder=TrajectoryRecorder(
                self.journal, session.id, journal=self.journal,
                snapshot_provider=session.to_snapshot,
            ),
        )
        provide = ConversationApplicationCoordinator(self.journal)._runtime_model_request_provider(
            admission, runtime,
        )
        with self.assertRaises(ContextRequiredContentExceedsBudget):
            provide("overflow-request", session, (), ())
        self.assertEqual(self.journal.connection.execute(
            "SELECT status FROM context_operations"
        ).fetchone()[0], "overflow")
        self.assertEqual(self.journal.connection.execute(
            "SELECT COUNT(*) FROM frozen_model_requests"
        ).fetchone()[0], 0)
        overflow_count = self.journal.connection.execute(
            "SELECT population_kind, value, request_id FROM m3_metric_samples "
            "WHERE metric_name='context_operation_count'"
        ).fetchone()
        self.assertEqual(tuple(overflow_count), ("overflow", 1.0, None))
        self.assertEqual(capture_direct_tree_manifest(self.root), before)

    def test_trusted_snapshot_refresh_is_versioned_and_exact_resume_uses_durable_bytes(self) -> None:
        admission = self._admit()
        manifests = self.journal.connection.execute(
            "SELECT revision, effective_digest FROM instruction_manifests ORDER BY revision"
        ).fetchall()
        self.assertEqual(len(manifests), 1)
        (self.root / "AGENTS.md").write_text("rule.style: terse\n", encoding="utf-8")
        stale_snapshot = self.journal.load_m3_context_snapshot(admission.operation_id)
        self.assertEqual(stale_snapshot["instruction_status"], "stale")
        self.assertEqual(stale_snapshot["instruction_content"], ("rule.style: precise\n",))
        version = int(self.journal.inspect_conversation(admission.conversation_id)["product_version"])
        digest = self.journal.canonical_instruction_refresh_digest(
            conversation_id=admission.conversation_id, expected_version=version,
            reason="explicit_refresh", target_paths=(),
        )
        coordinator = ConversationApplicationCoordinator(self.journal)
        refreshed = coordinator.refresh_instructions(InstructionRefreshCommand(
            "refresh", digest, admission.conversation_id, version,
        ))
        again = coordinator.refresh_instructions(InstructionRefreshCommand(
            "refresh", digest, admission.conversation_id, version,
        ))
        self.assertEqual(refreshed, again)
        rows = self.journal.connection.execute(
            "SELECT revision, effective_digest FROM instruction_manifests ORDER BY revision"
        ).fetchall()
        self.assertEqual([int(row["revision"]) for row in rows], [1, 2])
        self.assertNotEqual(rows[0]["effective_digest"], rows[1]["effective_digest"])
        current_snapshot = self.journal.load_m3_context_snapshot(admission.operation_id)
        self.assertEqual(current_snapshot["instruction_status"], "active")
        self.assertEqual(current_snapshot["instruction_content"], ("rule.style: terse\n",))
        self.assertEqual(
            coordinator.resume_execution(ResumeExecutionCommand(
                admission.operation_id, admission.conversation_id,
            )).turn_id,
            admission.turn_id,
        )

    def test_unrelated_provider_trust_does_not_activate_agents_and_stale_is_sticky(self) -> None:
        self.journal.configure_instruction_trust(
            operation_id="revoke-agents",
            payload_digest=self.journal.canonical_instruction_trust_digest(
                repository_id=self.direct.repository_id,
                project_scope_id=self.direct.project_scope.project_scope_id,
                workspace_binding_id=self.direct.binding.workspace_binding_id,
                source_kind="agents_md", trusted=False,
            ),
            repository_id=self.direct.repository_id,
            project_scope_id=self.direct.project_scope.project_scope_id,
            workspace_binding_id=self.direct.binding.workspace_binding_id,
            source_kind="agents_md", trusted=False,
        )
        other_digest = self.journal.canonical_instruction_trust_digest(
            repository_id=self.direct.repository_id,
            project_scope_id=self.direct.project_scope.project_scope_id,
            workspace_binding_id=self.direct.binding.workspace_binding_id,
            source_kind="other_provider", trusted=True,
        )
        self.journal.configure_instruction_trust(
            operation_id="trust-other", payload_digest=other_digest,
            repository_id=self.direct.repository_id,
            project_scope_id=self.direct.project_scope.project_scope_id,
            workspace_binding_id=self.direct.binding.workspace_binding_id,
            source_kind="other_provider", trusted=True,
        )
        inactive = self._admit(operation="wrong-provider", session_id="wrong-provider-session")
        self.assertEqual(self.journal.load_m3_context_snapshot(inactive.operation_id)["instruction_content"], ())
        trust_digest = self.journal.canonical_instruction_trust_digest(
            repository_id=self.direct.repository_id,
            project_scope_id=self.direct.project_scope.project_scope_id,
            workspace_binding_id=self.direct.binding.workspace_binding_id,
            source_kind="agents_md", trusted=True,
        )
        self.journal.configure_instruction_trust(
            operation_id="retrust-agents", payload_digest=trust_digest,
            repository_id=self.direct.repository_id,
            project_scope_id=self.direct.project_scope.project_scope_id,
            workspace_binding_id=self.direct.binding.workspace_binding_id,
            source_kind="agents_md", trusted=True,
        )
        active = self._admit(operation="sticky-admit", session_id="sticky-session")
        instruction = self.root / "AGENTS.md"
        instruction.write_text("rule.style: changed\n", encoding="utf-8")
        self.assertEqual(self.journal.load_m3_context_snapshot(active.operation_id)["instruction_status"], "stale")
        instruction.write_text("rule.style: precise\n", encoding="utf-8")
        self.assertEqual(self.journal.load_m3_context_snapshot(active.operation_id)["instruction_status"], "stale")

    def test_product_runtime_freezes_composed_request_manifest_attempt_and_dispatch(self) -> None:
        admission = self._admit(operation="runtime-admit", session_id="runtime-session")
        coordinator = ConversationApplicationCoordinator(self.journal)
        host = AgentApplication(
            Path(self.temporary.name) / "host", journal=self.journal,
            event_store=self.journal, context_builder=BudgetedContextBuilder(),
        )
        session = self.journal.load_session(admission.legacy_session_id)
        runtime = AgentRuntime(
            session=session, backend=ScriptedBackend([{"final": "done"}]),
            context_builder=BudgetedContextBuilder(), registry=host.registry,
            harness=host.harness, workspace_manager=host.workspace_manager,
            recorder=TrajectoryRecorder(
                self.journal, session.id, journal=self.journal,
                snapshot_provider=session.to_snapshot,
            ),
        )
        result = coordinator.run_execution(
            ResumeExecutionCommand(admission.operation_id, admission.conversation_id), runtime,
        )
        self.assertIs(result.state, RuntimeState.COMPLETED, result.failure)
        request = self.journal.connection.execute(
            "SELECT request_json FROM frozen_model_requests"
        ).fetchone()
        manifest = self.journal.connection.execute(
            "SELECT policy_version, memory_status FROM context_manifests"
        ).fetchone()
        self.assertIn("rule.style: precise", str(request["request_json"]))
        self.assertEqual((manifest["policy_version"], manifest["memory_status"]), ("m3-hybrid-v1", "absent"))
        self.assertEqual(self.journal.connection.execute(
            "SELECT COUNT(*) FROM model_attempt_dispatches"
        ).fetchone()[0], 1)
        self.assertEqual(self.journal.connection.execute(
            "SELECT outcome_kind FROM model_attempt_outcomes"
        ).fetchone()[0], "succeeded")
        token_metric = self.journal.connection.execute(
            """SELECT conversation_id, turn_id, runtime_execution_id, request_id,
                      attempt_id, value, classification, coverage_status
               FROM m3_metric_samples WHERE metric_name='token_usage'"""
        ).fetchone()
        self.assertEqual((token_metric["conversation_id"], token_metric["turn_id"],
                          token_metric["runtime_execution_id"]),
                         (admission.conversation_id, admission.turn_id,
                          admission.runtime_execution_id))
        self.assertIsNotNone(token_metric["request_id"])
        self.assertIsNotNone(token_metric["attempt_id"])
        self.assertIsNone(token_metric["value"])
        self.assertEqual((token_metric["classification"], token_metric["coverage_status"]),
                         ("unknown", "incomplete"))
        self.assertGreater(self.journal.connection.execute(
            "SELECT COUNT(*) FROM m3_metric_samples WHERE metric_name='context_section_tokens'"
        ).fetchone()[0], 0)

    def test_second_connection_steering_between_compose_and_freeze_rejects_provider(self) -> None:
        admission = self._admit(operation="race-admit", session_id="race-session")
        host = AgentApplication(
            Path(self.temporary.name) / "race-host", journal=self.journal,
            event_store=self.journal, context_builder=BudgetedContextBuilder(),
        )
        session = self.journal.load_session(admission.legacy_session_id)
        backend = CrashBeforeProvider()
        runtime = AgentRuntime(
            session=session, backend=backend, context_builder=BudgetedContextBuilder(),
            registry=host.registry, harness=host.harness,
            workspace_manager=host.workspace_manager,
            recorder=TrajectoryRecorder(
                self.journal, session.id, journal=self.journal,
                snapshot_provider=session.to_snapshot,
            ),
        )
        coordinator = ConversationApplicationCoordinator(self.journal)
        original = coordinator._runtime_model_request_provider
        raced = False

        def interleaved(admitted, current_runtime):
            provide = original(admitted, current_runtime)

            def stale(request_id, current_session, messages, tools):
                nonlocal raced
                frozen = provide(request_id, current_session, messages, tools)
                if not raced:
                    raced = True
                    with SQLiteRunJournal(self.journal.db_path) as competitor:
                        version = int(competitor.inspect_conversation(admission.conversation_id)["product_version"])
                        competitor.append_product_input(
                            operation_id="racing-steer",
                            payload_digest=competitor.canonical_input_digest(
                                conversation_id=admission.conversation_id,
                                input_kind="steering", payload={"text": "new constraint"},
                                correlation_id=None, expected_conversation_version=version,
                            ),
                            conversation_id=admission.conversation_id, input_kind="steering",
                            payload={"text": "new constraint"},
                            expected_conversation_version=version,
                        )
                return frozen

            return stale

        with patch.object(coordinator, "_runtime_model_request_provider", side_effect=interleaved):
            result = coordinator.run_execution(
                ResumeExecutionCommand(admission.operation_id, admission.conversation_id), runtime,
            )
        self.assertIs(result.state, RuntimeState.COMPLETED, result.failure)
        self.assertEqual(backend.calls, 1)
        self.assertEqual(self.journal.connection.execute(
            "SELECT COUNT(*) FROM frozen_model_requests"
        ).fetchone()[0], 1)
        self.assertIn("new constraint", self.journal.connection.execute(
            "SELECT request_json FROM frozen_model_requests"
        ).fetchone()["request_json"])
        self.assertEqual(self.journal.connection.execute(
            "SELECT COUNT(*) FROM context_operations WHERE status='stale_rebuild'"
        ).fetchone()[0], 1)

    def test_second_connection_refresh_during_model_call_is_rejected_at_safe_boundary(self) -> None:
        admission = self._admit(operation="refresh-race-admit", session_id="refresh-race-session")
        host = AgentApplication(
            Path(self.temporary.name) / "refresh-race-host", journal=self.journal,
            event_store=self.journal, context_builder=BudgetedContextBuilder(),
        )
        session = self.journal.load_session(admission.legacy_session_id)
        backend = CrashBeforeProvider()
        runtime = AgentRuntime(
            session=session, backend=backend, context_builder=BudgetedContextBuilder(),
            registry=host.registry, harness=host.harness,
            workspace_manager=host.workspace_manager,
            recorder=TrajectoryRecorder(
                self.journal, session.id, journal=self.journal,
                snapshot_provider=session.to_snapshot,
            ),
        )
        coordinator = ConversationApplicationCoordinator(self.journal)
        original = coordinator._runtime_model_request_provider
        attempted = False

        def interleaved(admitted, current_runtime):
            provide = original(admitted, current_runtime)

            def refresh_during_call(request_id, current_session, messages, tools):
                nonlocal attempted
                frozen = provide(request_id, current_session, messages, tools)
                if not attempted:
                    attempted = True
                    with SQLiteRunJournal(self.journal.db_path) as competitor:
                        version = int(competitor.inspect_conversation(
                            admission.conversation_id,
                        )["product_version"])
                        digest = competitor.canonical_instruction_refresh_digest(
                            conversation_id=admission.conversation_id,
                            expected_version=version, reason="explicit_refresh", target_paths=(),
                        )
                        with self.assertRaises(ProductLifecycleConflict):
                            competitor.refresh_instruction_manifest(
                                operation_id="racing-refresh", payload_digest=digest,
                                conversation_id=admission.conversation_id,
                                expected_version=version,
                            )
                return frozen

            return refresh_during_call

        with patch.object(coordinator, "_runtime_model_request_provider", side_effect=interleaved):
            result = coordinator.run_execution(
                ResumeExecutionCommand(admission.operation_id, admission.conversation_id), runtime,
            )
        self.assertTrue(attempted)
        self.assertIs(result.state, RuntimeState.COMPLETED, result.failure)
        self.assertEqual(backend.calls, 1)
        self.assertEqual(self.journal.connection.execute(
            "SELECT COUNT(*) FROM instruction_manifests"
        ).fetchone()[0], 1)

    def test_failed_attempt_fallback_success_has_separate_count_and_unknown_usage(self) -> None:
        class Primary:
            name = "primary"

            def complete(self, request):
                raise BackendError("temporarily unavailable", kind="provider_unavailable")

        class Secondary:
            name = "secondary"

            def complete(self, request):
                return ModelResponse(text="done", usage=Usage())

        admission = self._admit(
            operation="fallback-admit", session_id="fallback-session",
            policy=RunPolicy(retry_base_delay_seconds=0, retry_max_delay_seconds=0),
        )
        host = AgentApplication(
            Path(self.temporary.name) / "fallback-host", journal=self.journal,
            event_store=self.journal, context_builder=BudgetedContextBuilder(),
        )
        session = self.journal.load_session(admission.legacy_session_id)
        runtime = AgentRuntime(
            session=session, backend=FallbackBackend((Primary(), Secondary())),
            context_builder=BudgetedContextBuilder(), registry=host.registry,
            harness=host.harness, workspace_manager=host.workspace_manager,
            recorder=TrajectoryRecorder(
                self.journal, session.id, journal=self.journal,
                snapshot_provider=session.to_snapshot,
            ),
        )
        result = ConversationApplicationCoordinator(self.journal).run_execution(
            ResumeExecutionCommand(admission.operation_id, admission.conversation_id), runtime,
        )
        self.assertIs(result.state, RuntimeState.COMPLETED, result.failure)
        outcomes = self.journal.connection.execute(
            """SELECT attempt.request_id, attempt.attempt_id, outcome.outcome_kind
               FROM model_attempts AS attempt JOIN model_attempt_outcomes AS outcome
                 USING(attempt_id) ORDER BY attempt.ordinal"""
        ).fetchall()
        self.assertEqual([row["outcome_kind"] for row in outcomes], ["failed", "succeeded"])
        self.assertEqual(outcomes[0]["request_id"], outcomes[1]["request_id"])
        fallback = self.journal.connection.execute(
            """SELECT metric_name, population_kind, conversation_id, turn_id,
                      runtime_execution_id, request_id, attempt_id, value,
                      classification, coverage_status, dimensions_json
               FROM m3_metric_samples WHERE metric_name='model_fallback_count'"""
        ).fetchall()
        self.assertEqual(len(fallback), 1)
        self.assertEqual((fallback[0]["population_kind"], fallback[0]["conversation_id"],
                          fallback[0]["turn_id"], fallback[0]["runtime_execution_id"],
                          fallback[0]["request_id"], fallback[0]["attempt_id"],
                          fallback[0]["value"], fallback[0]["classification"],
                          fallback[0]["coverage_status"]),
                         ("fallback_selected", admission.conversation_id,
                          admission.turn_id, admission.runtime_execution_id,
                          outcomes[0]["request_id"], outcomes[0]["attempt_id"],
                          1.0, "measured", "complete"))
        self.assertEqual(json.loads(fallback[0]["dimensions_json"]),
                         {"from": "primary", "to": "secondary"})
        usage = self.journal.connection.execute(
            """SELECT population_kind, value, classification, coverage_status
               FROM m3_metric_samples WHERE metric_name='token_usage'"""
        ).fetchall()
        self.assertEqual({(row["population_kind"], row["value"], row["classification"],
                           row["coverage_status"]) for row in usage},
                         {("failed", None, "unknown", "incomplete"),
                          ("succeeded", None, "unknown", "incomplete")})
        metric_counts = self.journal.connection.execute(
            """SELECT metric_name, population_kind, request_id, attempt_id, value
               FROM m3_metric_samples WHERE unit='count'"""
        ).fetchall()
        self.assertEqual(sum(row["metric_name"] == "frozen_request_count"
                             for row in metric_counts), 1)
        self.assertEqual(sum(row["metric_name"] == "context_manifest_count"
                             for row in metric_counts), 1)
        self.assertEqual({row["population_kind"] for row in metric_counts
                          if row["metric_name"] == "model_attempt_outcome_count"},
                         {"failed", "succeeded"})
        self.assertEqual([row["attempt_id"] for row in metric_counts
                          if row["metric_name"] == "model_retry_count"],
                         [outcomes[1]["attempt_id"]])
        self.assertTrue(all(row["value"] == 1 and row["request_id"]
                            for row in metric_counts if row["metric_name"] in
                            {"frozen_request_count", "model_attempt_outcome_count",
                             "model_retry_count", "model_fallback_count"}))
        overhead = self.journal.connection.execute(
            """SELECT value, classification, coverage_status, attempt_id
               FROM m3_metric_samples WHERE metric_name='retry_recovery_overhead_ms'"""
        ).fetchone()
        self.assertEqual(tuple(overhead), (None, "unknown", "unknown",
                                           outcomes[1]["attempt_id"]))

    def test_runtime_projects_post_harness_tool_artifact_and_legacy_reopen_is_unknown(self) -> None:
        (self.root / "value.txt").write_text("x" * 30_000, encoding="utf-8")
        admission = self._admit(operation="tool-admit", session_id="tool-session")
        host = AgentApplication(
            Path(self.temporary.name) / "tool-host", journal=self.journal,
            event_store=self.journal, context_builder=BudgetedContextBuilder(),
        )
        session = self.journal.load_session(admission.legacy_session_id)
        runtime = AgentRuntime(
            session=session,
            backend=ScriptedBackend([
                {"tool_calls": [{
                    "id": "read-large", "name": "read_file",
                    "arguments": {"path": "value.txt"},
                }]},
                {"final": "done"},
            ]),
            context_builder=BudgetedContextBuilder(), registry=host.registry,
            harness=host.harness, workspace_manager=host.workspace_manager,
            recorder=TrajectoryRecorder(
                self.journal, session.id, journal=self.journal,
                snapshot_provider=session.to_snapshot,
            ),
        )
        result = ConversationApplicationCoordinator(self.journal).run_execution(
            ResumeExecutionCommand(admission.operation_id, admission.conversation_id), runtime,
        )
        self.assertIs(result.state, RuntimeState.COMPLETED, result.failure)
        projection = self.journal.connection.execute(
            """SELECT artifact.capture_completeness, artifact.captured_size,
                      observation.projection_completeness
               FROM tool_result_artifacts AS artifact
               JOIN normalized_observations AS observation USING(artifact_id)
               WHERE artifact.legacy_session_id=? AND artifact.tool_call_id='read-large'""",
            (admission.legacy_session_id,),
        ).fetchone()
        self.assertEqual(
            (projection["capture_completeness"], projection["projection_completeness"]),
            ("incomplete", "incomplete"),
        )
        self.assertLessEqual(int(projection["captured_size"]), 256 * 1024)

        # Recreate the shape of a pre-v7 result row: exact bytes exist, but its
        # original process-capture completeness was not durably classified.
        self.journal.connection.execute("DELETE FROM normalized_observations")
        self.journal.connection.execute("DELETE FROM tool_result_artifacts")
        database = self.journal.db_path
        self.journal.close()
        self.journal = SQLiteRunJournal(database)
        self.assertEqual(self.journal.connection.execute(
            "SELECT capture_completeness FROM tool_result_artifacts"
        ).fetchone()[0], "unknown")

    def test_product_summary_success_has_auxiliary_lineage_claims_and_reopens(self) -> None:
        (self.root / "value.txt").write_text("value\n" + "x" * 10_000, encoding="utf-8")
        admission = self._admit(
            operation="summary-admit", session_id="summary-session",
            policy=RunPolicy(max_output_tokens=0),
        )
        counter = ExactTokenCounter(
            lambda _provider, _model, messages: sum(len(message.content) for message in messages)
        )
        registry = ModelCapabilityRegistry([
            ModelCapability(
                provider="scripted", model="scripted", context_limit=14_000,
                token_counter=counter,
            )
        ])
        builder = BudgetedContextBuilder(
            capability_registry=registry,
            config=ContextBudgetConfig(
                protocol_margin_tokens=0, high_watermark_ratio=0.5,
                target_after_compression_ratio=0.4,
            ),
        )
        summary_payload = {
            "schema_version": 1,
            "goals": ["M3 task", "inspect precisely"],
            "constraints": [], "decisions": ["Use the read observation"],
            "files_read": [], "edits": [], "tests": [], "errors": [],
            "unresolved": [],
        }
        summary_backend = ScriptedBackend([{"final": json.dumps(summary_payload)}])
        compression_engine = CompressionEngine(summary_backend)
        host = AgentApplication(
            Path(self.temporary.name) / "summary-host", journal=self.journal,
            event_store=self.journal, context_builder=builder,
            compression_engine=compression_engine,
        )
        session = self.journal.load_session(admission.legacy_session_id)
        first_runtime = AgentRuntime(
            session=session,
            backend=ScriptedBackend([
                {"tool_calls": [{
                    "id": "read-summary", "name": "read_file",
                    "arguments": {"path": "value.txt"},
                }]},
                {"final": "done"},
            ]),
            context_builder=builder, registry=host.registry, harness=host.harness,
            workspace_manager=host.workspace_manager,
            recorder=TrajectoryRecorder(
                self.journal, session.id, journal=self.journal,
                snapshot_provider=session.to_snapshot,
            ),
            compression_engine=compression_engine,
            fault_injector=(
                lambda stage: (_ for _ in ()).throw(HostLoss(stage))
                if stage == "after_summary_start" else None
            ),
        )
        coordinator = ConversationApplicationCoordinator(self.journal)
        with self.assertRaises(HostLoss):
            coordinator.run_execution(
                ResumeExecutionCommand(admission.operation_id, admission.conversation_id),
                first_runtime,
            )
        self.assertEqual(summary_backend.requests, [])
        self.assertEqual(self.journal.connection.execute(
            """SELECT COUNT(*) FROM frozen_model_requests
               WHERE request_kind='summary_auxiliary'"""
        ).fetchone()[0], 1)
        self.assertEqual(self.journal.connection.execute(
            """SELECT status FROM context_operations AS operation
               JOIN frozen_model_requests AS request USING(request_id)
               WHERE request.request_kind='summary_auxiliary'"""
        ).fetchone()[0], "started")
        self.assertEqual(self.journal.connection.execute(
            "SELECT COUNT(*) FROM model_attempt_dispatches AS dispatch JOIN model_attempts AS attempt USING(attempt_id) JOIN frozen_model_requests AS request USING(request_id) WHERE request.request_kind='summary_auxiliary'"
        ).fetchone()[0], 0)

        resumed_session = self.journal.load_session(admission.legacy_session_id)
        resumed_runtime = AgentRuntime(
            session=resumed_session, backend=ScriptedBackend([{"final": "done"}]),
            context_builder=builder, registry=host.registry, harness=host.harness,
            workspace_manager=host.workspace_manager,
            recorder=TrajectoryRecorder(
                self.journal, resumed_session.id, journal=self.journal,
                snapshot_provider=resumed_session.to_snapshot,
            ),
            compression_engine=compression_engine,
        )
        result = coordinator.run_execution(
            ResumeExecutionCommand(admission.operation_id, admission.conversation_id),
            resumed_runtime,
        )
        self.assertIs(result.state, RuntimeState.COMPLETED, result.failure)
        self.assertEqual(len(summary_backend.requests), 1, [
            (event.event_type.value, event.payload)
            for event in self.journal.list_events(admission.legacy_session_id)
            if event.event_type.value.startswith("compression")
            or event.event_type.value in {"model_call_started", "model_call_finished", "tool_executed"}
        ])
        row = self.journal.connection.execute(
            """SELECT request.request_kind, outcome.outcome_kind,
                      summary.status, COUNT(claim.claim_id) AS claim_count
               FROM frozen_model_requests AS request
               JOIN model_attempts AS attempt USING(request_id)
               JOIN model_attempt_outcomes AS outcome USING(attempt_id)
               JOIN conversation_summary_artifacts AS summary
                 ON summary.conversation_id=?
               LEFT JOIN conversation_summary_claims AS claim USING(summary_artifact_id)
               WHERE request.legacy_session_id=?
                 AND request.request_kind='summary_auxiliary'
               GROUP BY request.request_id, summary.summary_artifact_id""",
            (admission.conversation_id, admission.legacy_session_id),
        ).fetchone()
        self.assertIsNotNone(row, [
            event.event_type.value
            for event in self.journal.list_events(admission.legacy_session_id)
        ])
        self.assertEqual(
            (row["request_kind"], row["outcome_kind"], row["status"]),
            ("summary_auxiliary", "succeeded", "valid"),
        )
        self.assertEqual(self.journal.connection.execute(
            """SELECT status FROM context_operations
               WHERE operation_kind='compaction' AND request_id IS NOT NULL"""
        ).fetchone()[0], "succeeded")
        self.assertEqual(self.journal.connection.execute(
            """SELECT population_kind FROM m3_metric_samples
               WHERE metric_name='context_compaction_latency_ms'
                 AND population_kind='compaction_succeeded'"""
        ).fetchone()[0], "compaction_succeeded")
        self.assertGreaterEqual(int(row["claim_count"]), 2)
        association = self.journal.connection.execute(
            """SELECT summary.auxiliary_request_id, summary.auxiliary_attempt_id,
                      summary.source_end_sequence, summary.source_digest, summary.content,
                      request.semantic_prefix_end_sequence, request.semantic_prefix_digest,
                      outcome.outcome_kind
               FROM conversation_summary_artifacts AS summary
               JOIN frozen_model_requests AS request
                 ON request.request_id=summary.auxiliary_request_id
               JOIN model_attempts AS attempt
                 ON attempt.attempt_id=summary.auxiliary_attempt_id
               JOIN model_attempt_outcomes AS outcome
                 ON outcome.attempt_id=attempt.attempt_id
               WHERE summary.conversation_id=?""",
            (admission.conversation_id,),
        ).fetchone()
        self.assertIsNotNone(association["auxiliary_request_id"])
        self.assertIsNotNone(association["auxiliary_attempt_id"])
        self.assertEqual((association["source_end_sequence"], association["source_digest"]),
                         (association["semantic_prefix_end_sequence"],
                          association["semantic_prefix_digest"]))
        self.assertEqual(association["outcome_kind"], "succeeded")
        self.assertIn("Use the read observation", association["content"])
        auxiliary_samples = self.journal.connection.execute(
            """SELECT metric_name, population_kind, conversation_id, turn_id,
                      runtime_execution_id, request_id, attempt_id, classification,
                      coverage_status, value
               FROM m3_metric_samples WHERE population_kind='auxiliary_succeeded'"""
        ).fetchall()
        self.assertEqual({sample["metric_name"] for sample in auxiliary_samples},
                         {"model_attempt_latency_ms", "token_usage", "model_attempt_outcome_count"})
        self.assertTrue(all(sample["conversation_id"] == admission.conversation_id
                            and sample["turn_id"] == admission.turn_id
                            and sample["runtime_execution_id"] == admission.runtime_execution_id
                            and sample["request_id"] and sample["attempt_id"]
                            for sample in auxiliary_samples))
        unknown_usage = next(sample for sample in auxiliary_samples
                             if sample["metric_name"] == "token_usage")
        self.assertEqual((unknown_usage["classification"], unknown_usage["coverage_status"],
                          unknown_usage["value"]), ("unknown", "incomplete", None))
        database = self.journal.db_path
        self.journal.close()
        self.journal = SQLiteRunJournal(database)
        self.assertEqual(self.journal.connection.execute(
            """SELECT COUNT(*) FROM conversation_summary_artifacts
               WHERE conversation_id=? AND status='valid'""",
            (admission.conversation_id,),
        ).fetchone()[0], 1)

    def test_failed_auxiliary_compaction_preserves_last_valid_conversation_summary(self) -> None:
        (self.root / "value.txt").write_text("value\n" + "x" * 10_000, encoding="utf-8")
        admission = self._admit(
            operation="failed-summary-admit", session_id="failed-summary-session",
            policy=RunPolicy(max_output_tokens=0),
        )
        events = tuple(
            {"sequence": event["sequence"], "event_type": event["event_type"],
             "provenance_kind": event["provenance_kind"], "payload": event["provenance"]}
            for event in self.journal.list_conversation_semantic_events(admission.legacy_session_id)
        )
        previous = build_summary_artifact(
            conversation_id=admission.conversation_id, events=events,
            content="inspect precisely", claims=(SummaryClaim(
                "previous-intent", "conversational_intent", "inspect precisely", 1, 1,
            ),),
        )
        self.journal.record_summary_artifact(
            previous,
            instruction_manifest_id=admission.instruction_manifest.instruction_manifest_id,
            workspace_binding_id=self.direct.binding.workspace_binding_id,
        )
        counter = ExactTokenCounter(
            lambda _provider, _model, messages: sum(len(message.content) for message in messages)
        )
        builder = BudgetedContextBuilder(
            capability_registry=ModelCapabilityRegistry((ModelCapability(
                "scripted", "scripted", 14_000, token_counter=counter,
            ),)),
            config=ContextBudgetConfig(
                protocol_margin_tokens=0, high_watermark_ratio=0.5,
                target_after_compression_ratio=0.4,
            ),
        )
        host = AgentApplication(
            Path(self.temporary.name) / "failed-summary-host", journal=self.journal,
            event_store=self.journal, context_builder=builder,
        )
        session = self.journal.load_session(admission.legacy_session_id)
        runtime = AgentRuntime(
            session=session, backend=ScriptedBackend([
                {"tool_calls": [{"id": "read-failure", "name": "read_file",
                                 "arguments": {"path": "value.txt"}}]},
                {"final": "done"},
            ]),
            context_builder=builder, registry=host.registry,
            harness=host.harness, workspace_manager=host.workspace_manager,
            recorder=TrajectoryRecorder(
                self.journal, session.id, journal=self.journal,
                snapshot_provider=session.to_snapshot,
            ),
            compression_engine=CompressionEngine(ScriptedBackend([{"final": "not JSON"}])),
        )
        result = ConversationApplicationCoordinator(self.journal).run_execution(
            ResumeExecutionCommand(admission.operation_id, admission.conversation_id), runtime,
        )
        self.assertIs(result.state, RuntimeState.COMPLETED, result.failure)
        self.assertEqual(self.journal.connection.execute(
            """SELECT summary_artifact_id FROM conversation_summary_artifacts
               WHERE conversation_id=? AND status='valid'""",
            (admission.conversation_id,),
        ).fetchone()[0], previous.summary_artifact_id)
        self.assertEqual(self.journal.connection.execute(
            """SELECT COUNT(*) FROM model_attempt_outcomes AS outcome
               JOIN model_attempts AS attempt USING(attempt_id)
               JOIN frozen_model_requests AS request USING(request_id)
               WHERE request.request_kind='summary_auxiliary'
                 AND outcome.outcome_kind='failed'"""
        ).fetchone()[0], 1)
        self.assertEqual(self.journal.connection.execute(
            """SELECT status FROM context_operations
               WHERE operation_kind='compaction' AND request_id IS NOT NULL"""
        ).fetchone()[0], "failed")
        self.assertEqual(tuple(self.journal.connection.execute(
            """SELECT population_kind, value, classification, coverage_status
               FROM m3_metric_samples
               WHERE metric_name='context_compaction_latency_ms'
                 AND population_kind='compaction_failed'"""
        ).fetchone()), ("compaction_failed", None, "unknown", "incomplete"))
        failed_samples = self.journal.connection.execute(
            """SELECT metric_name, classification, coverage_status, value,
                      conversation_id, turn_id, runtime_execution_id, request_id, attempt_id
               FROM m3_metric_samples WHERE population_kind='auxiliary_failed'"""
        ).fetchall()
        self.assertEqual({sample["metric_name"] for sample in failed_samples},
                         {"model_attempt_latency_ms", "token_usage", "model_attempt_outcome_count"})
        self.assertTrue(all(sample["conversation_id"] == admission.conversation_id
                            and sample["turn_id"] == admission.turn_id
                            and sample["runtime_execution_id"] == admission.runtime_execution_id
                            and sample["request_id"] and sample["attempt_id"]
                            for sample in failed_samples))
        failed_usage = next(sample for sample in failed_samples
                            if sample["metric_name"] == "token_usage")
        self.assertEqual((failed_usage["classification"], failed_usage["coverage_status"],
                          failed_usage["value"]), ("unknown", "incomplete", None))

    def test_concurrent_conversation_append_rejects_frozen_summary_prefix(self) -> None:
        (self.root / "value.txt").write_text("value\n" + "x" * 10_000, encoding="utf-8")
        admission = self._admit(
            operation="summary-drift-admit", session_id="summary-drift-session",
            policy=RunPolicy(max_output_tokens=0),
        )
        counter = ExactTokenCounter(
            lambda _provider, _model, messages: sum(len(message.content) for message in messages)
        )
        builder = BudgetedContextBuilder(
            capability_registry=ModelCapabilityRegistry((ModelCapability(
                "scripted", "scripted", 14_000, token_counter=counter,
            ),)),
            config=ContextBudgetConfig(
                protocol_margin_tokens=0, high_watermark_ratio=0.5,
                target_after_compression_ratio=0.4,
            ),
        )

        class ConcurrentSummary:
            name = "scripted"

            def __init__(self, journal):
                self.journal = journal
                self.calls = 0

            def complete(self, request):
                self.calls += 1
                with SQLiteRunJournal(self.journal.db_path) as competitor:
                    version = int(competitor.inspect_conversation(
                        admission.conversation_id,
                    )["product_version"])
                    competitor.append_product_input(
                        operation_id="summary-concurrent-steering",
                        payload_digest=competitor.canonical_input_digest(
                            conversation_id=admission.conversation_id,
                            input_kind="steering", payload={"text": "new intent during summary"},
                            correlation_id=None, expected_conversation_version=version,
                        ),
                        conversation_id=admission.conversation_id, input_kind="steering",
                        payload={"text": "new intent during summary"},
                        expected_conversation_version=version,
                    )
                return ModelResponse(text=json.dumps({
                    "schema_version": 1, "goals": ["M3 task", "inspect precisely"],
                    "constraints": [], "decisions": ["Use the read observation"],
                    "files_read": [], "edits": [], "tests": [], "errors": [], "unresolved": [],
                }), usage=Usage())

        summary_backend = ConcurrentSummary(self.journal)
        host = AgentApplication(
            Path(self.temporary.name) / "summary-drift-host", journal=self.journal,
            event_store=self.journal, context_builder=builder,
            compression_engine=CompressionEngine(summary_backend),
        )
        session = self.journal.load_session(admission.legacy_session_id)
        runtime = AgentRuntime(
            session=session, backend=ScriptedBackend([
                {"tool_calls": [{"id": "drift-read", "name": "read_file",
                                 "arguments": {"path": "value.txt"}}]},
                {"final": "done"},
            ]),
            context_builder=builder, registry=host.registry, harness=host.harness,
            workspace_manager=host.workspace_manager,
            recorder=TrajectoryRecorder(
                self.journal, session.id, journal=self.journal,
                snapshot_provider=session.to_snapshot,
            ),
            compression_engine=CompressionEngine(summary_backend),
        )
        result = ConversationApplicationCoordinator(self.journal).run_execution(
            ResumeExecutionCommand(admission.operation_id, admission.conversation_id), runtime,
        )
        self.assertIs(result.state, RuntimeState.COMPLETED, result.failure)
        self.assertEqual(summary_backend.calls, 1)
        self.assertEqual(self.journal.connection.execute(
            "SELECT COUNT(*) FROM conversation_summary_artifacts"
        ).fetchone()[0], 0)
        self.assertEqual(self.journal.connection.execute(
            """SELECT outcome.outcome_kind FROM model_attempt_outcomes AS outcome
               JOIN model_attempts AS attempt USING(attempt_id)
               JOIN frozen_model_requests AS request USING(request_id)
               WHERE request.request_kind='summary_auxiliary'"""
        ).fetchone()[0], "failed")

    def test_unconfigured_compaction_rejection_has_population_without_auxiliary_request(self) -> None:
        (self.root / "value.txt").write_text("value\n" + "x" * 10_000, encoding="utf-8")
        admission = self._admit(
            operation="rejected-compaction-admit", session_id="rejected-compaction-session",
            policy=RunPolicy(max_output_tokens=0),
        )
        counter = ExactTokenCounter(
            lambda _provider, _model, messages: sum(len(message.content) for message in messages)
        )
        builder = BudgetedContextBuilder(
            capability_registry=ModelCapabilityRegistry((ModelCapability(
                "scripted", "scripted", 14_000, token_counter=counter,
            ),)),
            config=ContextBudgetConfig(
                protocol_margin_tokens=0, high_watermark_ratio=0.5,
                target_after_compression_ratio=0.4,
            ),
        )
        host = AgentApplication(
            Path(self.temporary.name) / "rejected-compaction-host", journal=self.journal,
            event_store=self.journal, context_builder=builder,
        )
        session = self.journal.load_session(admission.legacy_session_id)
        runtime = AgentRuntime(
            session=session, backend=ScriptedBackend([
                {"tool_calls": [{"id": "rejected-read", "name": "read_file",
                                 "arguments": {"path": "value.txt"}}]},
                {"final": "done"},
            ]),
            context_builder=builder, registry=host.registry, harness=host.harness,
            workspace_manager=host.workspace_manager,
            recorder=TrajectoryRecorder(
                self.journal, session.id, journal=self.journal,
                snapshot_provider=session.to_snapshot,
            ),
        )
        result = ConversationApplicationCoordinator(self.journal).run_execution(
            ResumeExecutionCommand(admission.operation_id, admission.conversation_id), runtime,
        )
        self.assertIs(result.state, RuntimeState.COMPLETED, result.failure)
        rejected = self.journal.connection.execute(
            """SELECT op.status, op.request_id, metric.population_kind,
                      metric.conversation_id, metric.turn_id, metric.runtime_execution_id,
                      metric.value, metric.classification, metric.coverage_status
               FROM context_operations AS op JOIN m3_metric_samples AS metric
                 USING(context_operation_id)
               WHERE op.operation_kind='compaction' AND op.status='rejected'
                 AND metric.metric_name='context_compaction_latency_ms'"""
        ).fetchall()
        self.assertEqual(len(rejected), 1)
        self.assertEqual(tuple(rejected[0]),
                         ("rejected", None, "compaction_rejected",
                          admission.conversation_id, admission.turn_id,
                          admission.runtime_execution_id, None, "unknown", "incomplete"))

    def test_native_second_compaction_supersedes_first_and_reopens_current_only(self) -> None:
        (self.root / "value.txt").write_text("value\n" + "x" * 10_000, encoding="utf-8")
        (self.root / "value2.txt").write_text("second\n" + "y" * 20_000, encoding="utf-8")
        (self.root / "value3.txt").write_text("third\n" + "z" * 10_000, encoding="utf-8")
        admission = self._admit(
            operation="native-regeneration-admit", session_id="native-regeneration-session",
            policy=RunPolicy(max_output_tokens=0),
        )
        before = capture_direct_tree_manifest(self.root)
        counter = ExactTokenCounter(
            lambda _provider, _model, messages: sum(len(message.content) for message in messages)
        )
        builder = BudgetedContextBuilder(
            capability_registry=ModelCapabilityRegistry((ModelCapability(
                "scripted", "scripted", 24_000, token_counter=counter,
            ),)),
            config=ContextBudgetConfig(
                protocol_margin_tokens=0, high_watermark_ratio=0.3,
                target_after_compression_ratio=0.2,
            ),
        )
        summary_payload = {
            "schema_version": 1, "goals": ["M3 task", "inspect precisely"],
            "constraints": [], "decisions": ["Use the read observation"],
            "files_read": [], "edits": [], "tests": [], "errors": [], "unresolved": [],
        }
        summary_backend = ScriptedBackend([
            {"final": json.dumps(summary_payload)},
            {"final": json.dumps(summary_payload)},
        ])
        compression_engine = CompressionEngine(summary_backend)
        host = AgentApplication(
            Path(self.temporary.name) / "native-regeneration-host", journal=self.journal,
            event_store=self.journal, context_builder=builder,
            compression_engine=compression_engine,
        )
        session = self.journal.load_session(admission.legacy_session_id)
        runtime = AgentRuntime(
            session=session, backend=ScriptedBackend([
                {"tool_calls": [{"id": "read-first", "name": "read_file",
                                 "arguments": {"path": "value.txt"}}]},
                {"tool_calls": [{"id": "read-second", "name": "read_file",
                                 "arguments": {"path": "value2.txt"}}]},
                {"tool_calls": [{"id": "read-third", "name": "read_file",
                                 "arguments": {"path": "value3.txt"}}]},
                {"final": "done"},
            ]),
            context_builder=builder, registry=host.registry, harness=host.harness,
            workspace_manager=host.workspace_manager,
            recorder=TrajectoryRecorder(
                self.journal, session.id, journal=self.journal,
                snapshot_provider=session.to_snapshot,
            ),
            compression_engine=compression_engine, max_compression_calls=2,
        )
        original_events = self.journal.list_conversation_semantic_events(admission.legacy_session_id)
        result = ConversationApplicationCoordinator(self.journal).run_execution(
            ResumeExecutionCommand(admission.operation_id, admission.conversation_id), runtime,
        )
        self.assertIs(result.state, RuntimeState.COMPLETED, result.failure)
        self.assertEqual(len(summary_backend.requests), 2, [
            (event.sequence, event.event_type.value)
            for event in self.journal.list_events(admission.legacy_session_id)
        ])
        summaries = self.journal.connection.execute(
            """SELECT summary_artifact_id, source_start_sequence, source_end_sequence,
                      parent_summary_id, status, superseded_by
               FROM conversation_summary_artifacts WHERE conversation_id=?
               ORDER BY source_end_sequence""",
            (admission.conversation_id,),
        ).fetchall()
        self.assertEqual(len(summaries), 2)
        first, second = summaries
        self.assertEqual((first["source_start_sequence"], second["source_start_sequence"]),
                         (1, 1))
        self.assertGreater(second["source_end_sequence"], first["source_end_sequence"])
        self.assertEqual((first["status"], first["superseded_by"]),
                         ("superseded", second["summary_artifact_id"]))
        self.assertEqual((second["status"], second["parent_summary_id"]),
                         ("valid", first["summary_artifact_id"]))
        final_manifest = self.journal.connection.execute(
            """SELECT context.manifest_json FROM context_manifests AS context
               JOIN frozen_model_requests AS request USING(request_id)
               WHERE request.legacy_session_id=? AND request.request_kind='agent'
               ORDER BY request.request_ordinal DESC LIMIT 1""",
            (admission.legacy_session_id,),
        ).fetchone()
        selected_summaries = [item["source_id"] for item in
                              json.loads(final_manifest["manifest_json"])["selections"]
                              if item["source_class"] == "summary"
                              and item["disposition"] == "included"]
        self.assertEqual(selected_summaries, [second["summary_artifact_id"]])
        events_after = self.journal.list_conversation_semantic_events(admission.legacy_session_id)
        self.assertEqual(events_after[:len(original_events)], original_events)
        database = self.journal.db_path
        self.journal.close()
        self.journal = SQLiteRunJournal(database)
        snapshot = self.journal.load_m3_context_snapshot(admission.operation_id)
        summary_sources = [source for source in snapshot["typed_sources"]
                           if source["source_class"] == "summary"
                           and source["status"] == "eligible"]
        self.assertEqual([source["source_id"] for source in summary_sources],
                         [second["summary_artifact_id"]])
        self.assertEqual(capture_direct_tree_manifest(self.root), before)

    def test_artifact_file_and_summary_provenance_are_persisted_without_tree_writes(self) -> None:
        admission = self._admit(operation="artifact-admit", session_id="artifact-session")
        before = tree_fingerprint(self.root)
        artifact, observation = capture_tool_result_artifact(
            session_id=admission.legacy_session_id, tool_call_id="tool-1",
            channel="stdout", content=b"abcdef", capture_limit=4,
        )
        self.journal.record_tool_result_artifact(
            admission.legacy_session_id, artifact, observation,
        )
        with self.assertRaisesRegex(InvariantViolation, "identity/content collision"):
            self.journal.record_tool_result_artifact(
                admission.legacy_session_id, replace(artifact, content=b"zzzz"), observation,
            )
        file_item = capture_file_context_item(
            workspace_binding_id=self.direct.binding.workspace_binding_id,
            root=self.root, path="value.txt", origin_kind="tool_result",
            origin_id=artifact.artifact_id, byte_start=0, byte_end=6,
        )
        self.journal.record_file_context_item(file_item)
        events = tuple(
            {"sequence": event["sequence"], "event_type": event["event_type"],
             "provenance_kind": event["provenance_kind"], "payload": event["provenance"]}
            for event in self.journal.list_conversation_semantic_events(admission.legacy_session_id)
        )
        claim = SummaryClaim(
            "claim-1", "conversational_intent", "inspect precisely", 1, 1,
        )
        summary = build_summary_artifact(
            conversation_id=admission.conversation_id, events=events,
            content="The user requested a precise inspection.", claims=(claim,),
        )
        self.journal.record_summary_artifact(
            summary,
            instruction_manifest_id=admission.instruction_manifest.instruction_manifest_id,
            workspace_binding_id=self.direct.binding.workspace_binding_id,
        )
        changed_content = "A contradictory replacement summary."
        with self.assertRaisesRegex(InvariantViolation, "identity/content collision"):
            self.journal.record_summary_artifact(
                replace(summary, content=changed_content,
                        content_digest=sha256_bytes(changed_content.encode("utf-8"))),
                instruction_manifest_id=admission.instruction_manifest.instruction_manifest_id,
                workspace_binding_id=self.direct.binding.workspace_binding_id,
            )
        with self.assertRaisesRegex(InvariantViolation, "source artifact is unavailable"):
            missing_claim = SummaryClaim(
                "missing-claim", "repository_code_fact", "value.txt was read", 1, 1,
                source_artifact_id="missing-artifact", source_revision="revision",
            )
            self.journal.record_summary_artifact(
                replace(summary, summary_artifact_id="summary-with-missing-source",
                        claims=(missing_claim,)),
                instruction_manifest_id=admission.instruction_manifest.instruction_manifest_id,
                workspace_binding_id=self.direct.binding.workspace_binding_id,
            )
        self.assertEqual(tree_fingerprint(self.root), before)

        self.assertEqual(self.journal.connection.execute(
            "SELECT capture_completeness FROM tool_result_artifacts"
        ).fetchone()[0], "incomplete")
        self.assertEqual(self.journal.connection.execute(
            "SELECT COUNT(*) FROM conversation_summary_claims"
        ).fetchone()[0], 1)
        huge, huge_observation = capture_tool_result_artifact(
            session_id=admission.legacy_session_id, tool_call_id="tool-huge",
            channel="stdout", content=b"z" * (300 * 1024),
        )
        self.journal.record_tool_result_artifact(
            admission.legacy_session_id, huge, huge_observation,
        )
        self.assertEqual(self.journal.connection.execute(
            """SELECT capture_completeness FROM tool_result_artifacts
               WHERE artifact_id=?""", (huge.artifact_id,),
        ).fetchone()[0], "incomplete")
        new_events = tuple(
            {"sequence": event["sequence"], "event_type": event["event_type"],
             "provenance_kind": event["provenance_kind"], "payload": event["provenance"]}
            for event in self.journal.list_conversation_semantic_events(admission.legacy_session_id)
        )
        artifact_event = next(event for event in new_events
                              if event["event_type"] == "tool_observation_referenced"
                              and event["payload"]["artifact_id"] == artifact.artifact_id)
        code_claim = SummaryClaim(
            "code-claim", "repository_code_fact", "value.txt contains stable text",
            artifact_event["sequence"], artifact_event["sequence"],
            source_artifact_id=artifact.artifact_id, source_revision=file_item.revision,
        )
        successor = build_summary_artifact(
            conversation_id=admission.conversation_id, events=new_events,
            content="The user requested inspection and value.txt was read.",
            claims=(replace(claim, claim_id="successor-intent"), code_claim),
        )
        self.journal.record_summary_artifact(
            successor, instruction_manifest_id=admission.instruction_manifest.instruction_manifest_id,
            workspace_binding_id=self.direct.binding.workspace_binding_id,
            supersedes=summary.summary_artifact_id,
        )
        self.assertEqual(self.journal.connection.execute(
            "SELECT status FROM conversation_summary_artifacts WHERE summary_artifact_id=?",
            (summary.summary_artifact_id,),
        ).fetchone()[0], "superseded")
        current = self.journal.load_m3_context_snapshot(admission.operation_id)
        selected = next(source for source in current["typed_sources"]
                        if source["source_id"] == successor.summary_artifact_id)
        self.assertIn("value.txt contains stable text", selected["content"])
        (self.root / "value.txt").write_text("changed\n", encoding="utf-8")
        drifted = self.journal.load_m3_context_snapshot(admission.operation_id)
        selected = next(source for source in drifted["typed_sources"]
                        if source["source_id"] == successor.summary_artifact_id)
        self.assertIn("inspect precisely", selected["content"])
        self.assertIn("historical_claims", selected["content"])
        self.assertNotIn("value.txt contains stable text", json.loads(selected["content"])["valid_claims"])

    def test_file_context_capture_is_bounded_lossless_and_stales_on_mutation(self) -> None:
        admission = self._admit(operation="file-bytes-admit", session_id="file-bytes-session")
        (self.root / "oversize.bin").write_bytes(b"x" * (1024 * 1024 + 1))
        with self.assertRaisesRegex(ValueError, "bounded capture limit"):
            capture_file_context_item(
                workspace_binding_id=self.direct.binding.workspace_binding_id,
                root=self.root, path="oversize.bin", origin_kind="user", origin_id="file-request",
            )
        binary = b"prefix\x00\xff\xfe suffix"
        (self.root / "binary.dat").write_bytes(binary)
        item = capture_file_context_item(
            workspace_binding_id=self.direct.binding.workspace_binding_id,
            root=self.root, path="binary.dat", origin_kind="user", origin_id="file-request",
            byte_start=2, byte_end=10,
        )
        self.assertEqual(item.content, binary[2:10])
        self.assertEqual(item.encoding, "binary")
        self.journal.record_file_context_item(item)
        typed = self.journal.load_m3_context_snapshot(admission.operation_id)["typed_sources"]
        visible = next(source for source in typed if source["source_id"] == item.file_context_item_id)
        payload = json.loads(visible["content"])
        self.assertEqual(base64.b64decode(payload["content_base64"]), binary[2:10])
        self.assertEqual(visible["status"], "eligible")
        (self.root / "binary.dat").write_bytes(b"changed before freeze")
        stale = self.journal.load_m3_context_snapshot(admission.operation_id)["typed_sources"]
        self.assertEqual(next(source for source in stale
                              if source["source_id"] == item.file_context_item_id)["status"], "stale")
        database = self.journal.db_path
        self.journal.close()
        self.journal = SQLiteRunJournal(database)
        stored = self.journal.connection.execute(
            "SELECT content, encoding, capture_completeness FROM file_context_items WHERE file_context_item_id=?",
            (item.file_context_item_id,),
        ).fetchone()
        self.assertEqual((bytes(stored["content"]), stored["encoding"],
                          stored["capture_completeness"]), (binary[2:10], "binary", "incomplete"))

    def test_file_bytes_survive_post_freeze_external_mutation_and_exact_uncertain_retry(self) -> None:
        admission = self._admit(operation="file-retry-admit", session_id="file-retry-session")
        original = (self.root / "value.txt").read_bytes()
        item = capture_file_context_item(
            workspace_binding_id=self.direct.binding.workspace_binding_id,
            root=self.root, path="value.txt", origin_kind="user", origin_id="file-retry",
        )
        self.journal.record_file_context_item(item)
        host = AgentApplication(
            Path(self.temporary.name) / "file-retry-host", journal=self.journal,
            event_store=self.journal, context_builder=BudgetedContextBuilder(),
        )
        coordinator = ConversationApplicationCoordinator(self.journal)

        def make_runtime(*, fault=None, backend=None):
            session = self.journal.load_session(admission.legacy_session_id)
            return AgentRuntime(
                session=session, backend=backend or CrashBeforeProvider(),
                context_builder=BudgetedContextBuilder(), registry=host.registry,
                harness=host.harness, workspace_manager=host.workspace_manager,
                recorder=TrajectoryRecorder(
                    self.journal, session.id, journal=self.journal,
                    snapshot_provider=session.to_snapshot,
                ), fault_injector=fault,
            )

        def crash(stage):
            if stage == "after_model_dispatch":
                raise HostLoss(stage)

        try:
            first_result = coordinator.run_execution(
                ResumeExecutionCommand(admission.operation_id, admission.conversation_id),
                make_runtime(fault=crash),
            )
        except HostLoss:
            pass
        else:
            self.fail(f"expected after-dispatch host loss, got {first_result.state}: {first_result.failure}")
        before = self.journal.connection.execute(
            "SELECT request_json FROM frozen_model_requests WHERE request_kind='agent'"
        ).fetchone()[0]
        self.assertIn(base64.b64encode(original).decode("ascii"), before)
        (self.root / "value.txt").write_text("external replacement\n", encoding="utf-8")
        self.assertEqual(next(source["status"] for source in
                              self.journal.load_m3_context_snapshot(admission.operation_id)["typed_sources"]
                              if source["source_id"] == item.file_context_item_id), "stale")
        retry_backend = CrashBeforeProvider()
        with patch.object(self.journal, "load_m3_context_snapshot", side_effect=AssertionError("retry reread context")):
            result = coordinator.run_execution(
                ResumeExecutionCommand(admission.operation_id, admission.conversation_id),
                make_runtime(backend=retry_backend),
            )
        self.assertIs(result.state, RuntimeState.COMPLETED)
        self.assertEqual(retry_backend.calls, 1)
        self.assertEqual(retry_backend.requests, [json.loads(before)])
        after = self.journal.connection.execute(
            "SELECT request_json FROM frozen_model_requests WHERE request_kind='agent'"
        ).fetchone()[0]
        self.assertEqual(after, before)
        self.assertEqual(self.journal.connection.execute(
            "SELECT COUNT(*) FROM model_attempts"
        ).fetchone()[0], 2)

    def test_file_mutation_between_compose_and_freeze_excludes_stale_bytes(self) -> None:
        admission = self._admit(operation="file-race-admit", session_id="file-race-session")
        original = (self.root / "value.txt").read_bytes()
        item = capture_file_context_item(
            workspace_binding_id=self.direct.binding.workspace_binding_id,
            root=self.root, path="value.txt", origin_kind="user", origin_id="file-race",
        )
        self.journal.record_file_context_item(item)
        host = AgentApplication(
            Path(self.temporary.name) / "file-race-host", journal=self.journal,
            event_store=self.journal, context_builder=BudgetedContextBuilder(),
        )
        session = self.journal.load_session(admission.legacy_session_id)
        backend = CrashBeforeProvider()
        runtime = AgentRuntime(
            session=session, backend=backend, context_builder=BudgetedContextBuilder(),
            registry=host.registry, harness=host.harness,
            workspace_manager=host.workspace_manager,
            recorder=TrajectoryRecorder(
                self.journal, session.id, journal=self.journal,
                snapshot_provider=session.to_snapshot,
            ),
        )
        coordinator = ConversationApplicationCoordinator(self.journal)
        original_provider = coordinator._runtime_model_request_provider
        changed = False

        def interleaved(admitted, current_runtime):
            provide = original_provider(admitted, current_runtime)

            def mutate_after_compose(request_id, current_session, messages, tools):
                nonlocal changed
                frozen = provide(request_id, current_session, messages, tools)
                if not changed:
                    changed = True
                    (self.root / "value.txt").write_text("external pre-freeze change\n", encoding="utf-8")
                return frozen

            return mutate_after_compose

        with patch.object(coordinator, "_runtime_model_request_provider", side_effect=interleaved):
            result = coordinator.run_execution(
                ResumeExecutionCommand(admission.operation_id, admission.conversation_id), runtime,
            )
        self.assertTrue(changed)
        self.assertIs(result.state, RuntimeState.FAILED)
        self.assertEqual(result.failure["kind"], "source_repository_modified")
        self.assertEqual(backend.calls, 1)
        request_json = self.journal.connection.execute(
            "SELECT request_json FROM frozen_model_requests WHERE request_kind='agent'"
        ).fetchone()[0]
        self.assertNotIn(base64.b64encode(original).decode("ascii"), request_json)
        self.assertEqual(self.journal.connection.execute(
            "SELECT COUNT(*) FROM context_operations WHERE status='stale_rebuild'"
        ).fetchone()[0], 1)

    def test_committed_product_response_reuse_is_counted_once_without_new_usage(self) -> None:
        admission = self._admit(operation="reuse-admit", session_id="reuse-session")
        host = AgentApplication(
            Path(self.temporary.name) / "reuse-host", journal=self.journal,
            event_store=self.journal, context_builder=BudgetedContextBuilder(),
        )
        coordinator = ConversationApplicationCoordinator(self.journal)

        def runtime(*, fault=None):
            session = self.journal.load_session(admission.legacy_session_id)
            return AgentRuntime(
                session=session, backend=CrashBeforeProvider(),
                context_builder=BudgetedContextBuilder(), registry=host.registry,
                harness=host.harness, workspace_manager=host.workspace_manager,
                recorder=TrajectoryRecorder(
                    self.journal, session.id, journal=self.journal,
                    snapshot_provider=session.to_snapshot,
                ), fault_injector=fault,
            )

        def crash(stage):
            if stage == "after_model_response":
                raise HostLoss(stage)

        with self.assertRaises(HostLoss):
            coordinator.run_execution(
                ResumeExecutionCommand(admission.operation_id, admission.conversation_id),
                runtime(fault=crash),
            )
        with patch.object(self.journal, "load_m3_context_snapshot", side_effect=AssertionError("reused response reread")):
            result = coordinator.run_execution(
                ResumeExecutionCommand(admission.operation_id, admission.conversation_id),
                runtime(),
            )
        self.assertIs(result.state, RuntimeState.COMPLETED)
        samples = self.journal.connection.execute(
            """SELECT metric_name, value, request_id, attempt_id
               FROM m3_metric_samples WHERE metric_name IN
               ('model_response_reuse_count','model_attempt_latency_ms','token_usage')"""
        ).fetchall()
        self.assertEqual([row["metric_name"] for row in samples].count("model_response_reuse_count"), 1)
        self.assertEqual([row["metric_name"] for row in samples].count("model_attempt_latency_ms"), 1)
        self.assertEqual([row["metric_name"] for row in samples].count("token_usage"), 1)
        self.assertTrue(all(row["request_id"] and row["attempt_id"] for row in samples))

    def test_safe_boundary_cancel_has_identity_bound_count_without_invented_attempt(self) -> None:
        admission = self._admit(operation="cancel-admit", session_id="cancel-session")
        version = int(self.journal.inspect_conversation(admission.conversation_id)["product_version"])
        payload = {"reason": "stop"}
        digest = self.journal.canonical_input_digest(
            conversation_id=admission.conversation_id, input_kind="cancel", payload=payload,
            correlation_id=None, expected_conversation_version=version,
        )
        self.journal.append_product_input(
            operation_id="cancel-intent", payload_digest=digest,
            conversation_id=admission.conversation_id, input_kind="cancel",
            payload=payload, expected_conversation_version=version,
        )
        next_version = int(self.journal.inspect_conversation(admission.conversation_id)["product_version"])
        self.journal.apply_cancel_at_safe_boundary(
            conversation_id=admission.conversation_id,
            legacy_session_id=admission.legacy_session_id,
            operation_id="cancel-intent", expected_conversation_version=next_version,
        )
        row = self.journal.connection.execute(
            """SELECT population_kind, conversation_id, turn_id, runtime_execution_id,
                      request_id, attempt_id, value FROM m3_metric_samples
               WHERE metric_name='run_cancel_count'"""
        ).fetchone()
        self.assertEqual(tuple(row), (
            "cancel_safe_boundary", admission.conversation_id, admission.turn_id,
            admission.runtime_execution_id, None, None, 1.0,
        ))

    def test_commit_before_call_resumes_same_attempt_without_recomposition(self) -> None:
        admission = self._admit(operation="crash-admit", session_id="crash-session")
        coordinator = ConversationApplicationCoordinator(self.journal)
        host = AgentApplication(
            Path(self.temporary.name) / "crash-host", journal=self.journal,
            event_store=self.journal, context_builder=BudgetedContextBuilder(),
        )

        class CountingBuilder:
            def __init__(self) -> None:
                self.calls = 0
                self.delegate = BudgetedContextBuilder()

            def build(self, request):
                self.calls += 1
                return self.delegate.build(request)

        def make_runtime(*, fault=None, backend=None, builder=None):
            session = self.journal.load_session(admission.legacy_session_id)
            return AgentRuntime(
                session=session, backend=backend or CrashBeforeProvider(),
                context_builder=builder or BudgetedContextBuilder(), registry=host.registry,
                harness=host.harness, workspace_manager=host.workspace_manager,
                recorder=TrajectoryRecorder(
                    self.journal, session.id, journal=self.journal,
                    snapshot_provider=session.to_snapshot,
                ), fault_injector=fault,
            )

        def crash(stage: str) -> None:
            if stage == "after_model_start":
                raise HostLoss(stage)

        first_backend = CrashBeforeProvider()
        first_builder = CountingBuilder()
        with self.assertRaises(HostLoss):
            coordinator.run_execution(
                ResumeExecutionCommand(admission.operation_id, admission.conversation_id),
                make_runtime(fault=crash, backend=first_backend, builder=first_builder),
            )
        self.assertEqual(first_backend.calls, 0)
        self.assertEqual(self.journal.connection.execute(
            "SELECT COUNT(*) FROM model_attempts"
        ).fetchone()[0], 1)
        self.assertEqual(self.journal.connection.execute(
            "SELECT COUNT(*) FROM model_attempt_dispatches"
        ).fetchone()[0], 0)
        resumed_backend = CrashBeforeProvider()
        resumed_builder = CountingBuilder()
        result = coordinator.run_execution(
            ResumeExecutionCommand(admission.operation_id, admission.conversation_id),
            make_runtime(backend=resumed_backend, builder=resumed_builder),
        )
        self.assertIs(result.state, RuntimeState.COMPLETED, result.failure)
        self.assertEqual(resumed_backend.calls, 1)
        self.assertEqual(resumed_builder.calls, 0)
        self.assertEqual(self.journal.connection.execute(
            "SELECT COUNT(*) FROM model_attempts"
        ).fetchone()[0], 1)
        self.assertEqual(self.journal.connection.execute(
            "SELECT COUNT(*) FROM model_attempt_dispatches"
        ).fetchone()[0], 1)

    def test_unresolved_instruction_conflict_uses_correlated_existing_wait_state(self) -> None:
        contents = (b"Always run tests before finishing.\n", b"Never run tests before finishing.\n")
        paths = (self.root / "left" / "AGENTS.md", self.root / "right" / "AGENTS.md")
        for path, content in zip(paths, contents):
            path.parent.mkdir()
            path.write_bytes(content)
        sources = tuple(
            DiscoveredInstruction(
                f"conflict-source-{index}", str(path), path.relative_to(self.root).as_posix(),
                "conversation", 50, 5, sha256_bytes(content), "discovered", None, content,
            )
            for index, (path, content) in enumerate(zip(paths, contents), start=1)
        )
        discovery = InstructionDiscovery(
            str(self.root), str(self.root), str(self.root), sources, {}, sum(map(len, contents)), True,
        )
        with patch("coding_agent.product_instructions.discover_instruction_sources", return_value=discovery):
            admission = self._admit(operation="conflict-admit", session_id="conflict-session")
        host = AgentApplication(
            Path(self.temporary.name) / "conflict-host", journal=self.journal,
            event_store=self.journal, context_builder=BudgetedContextBuilder(),
        )
        session = self.journal.load_session(admission.legacy_session_id)
        backend = CrashBeforeProvider()
        runtime = AgentRuntime(
            session=session, backend=backend, context_builder=BudgetedContextBuilder(),
            registry=host.registry, harness=host.harness,
            workspace_manager=host.workspace_manager,
            recorder=TrajectoryRecorder(
                self.journal, session.id, journal=self.journal,
                snapshot_provider=session.to_snapshot,
            ),
        )
        result = ConversationApplicationCoordinator(self.journal).run_execution(
            ResumeExecutionCommand(admission.operation_id, admission.conversation_id), runtime,
        )
        self.assertIs(result.state, RuntimeState.WAITING_USER_INPUT)
        self.assertEqual(backend.calls, 0)
        pending = self.journal.inspect_conversation(admission.conversation_id)[
            "outstanding_input_requests"
        ]
        self.assertEqual(pending[0]["payload"]["conflict_keys"], ["prose.action.run tests before finishing"])
        version = int(self.journal.inspect_conversation(admission.conversation_id)["product_version"])
        reply_digest = self.journal.canonical_input_digest(
            conversation_id=admission.conversation_id, input_kind="reply",
            payload={"text": "Always run tests before finishing"},
            correlation_id=pending[0]["request_id"], expected_conversation_version=version,
        )
        coordinator = ConversationApplicationCoordinator(self.journal)
        coordinator.reply(UserReplyCommand(
            "resolve-conflict", reply_digest, admission.conversation_id, version,
            pending[0]["request_id"], "Always run tests before finishing",
        ))
        resolved = self.journal.load_m3_context_snapshot(admission.operation_id)
        self.assertEqual(resolved["instruction_conflicts"], ())
        self.assertIn("Always run tests before finishing", resolved["current_intent"])
        self.assertTrue(any("instruction_conflict_resolved" in outcome
                            for outcome in resolved["resolver_outcomes"]))
        self.assertNotEqual(resolved["instruction_manifest_id"],
                            admission.instruction_manifest.instruction_manifest_id)
        resumed_session = self.journal.load_session(admission.legacy_session_id)
        resumed = AgentRuntime(
            session=resumed_session, backend=ScriptedBackend([{"final": "resolved"}]),
            context_builder=BudgetedContextBuilder(), registry=host.registry,
            harness=host.harness, workspace_manager=host.workspace_manager,
            recorder=TrajectoryRecorder(
                self.journal, resumed_session.id, journal=self.journal,
                snapshot_provider=resumed_session.to_snapshot,
            ),
        )
        outcome = coordinator.run_execution(
            ResumeExecutionCommand(admission.operation_id, admission.conversation_id), resumed,
        )
        self.assertIs(outcome.state, RuntimeState.COMPLETED, outcome.failure)
        self.assertIn("instruction_conflict_resolved", self.journal.connection.execute(
            "SELECT request_json FROM frozen_model_requests ORDER BY created_at DESC LIMIT 1"
        ).fetchone()["request_json"])

    def test_atomic_freeze_fault_matrix_never_calls_provider_or_leaves_half_pair(self) -> None:
        stages = (
            "before_m3_frozen_request", "after_m3_frozen_request",
            "before_m3_context_manifest", "after_m3_context_manifest",
            "before_m3_context_operation", "after_m3_context_operation",
            "before_m3_context_selections", "after_m3_context_selections",
            "before_m3_attempt_intent", "after_m3_attempt_intent",
        )
        for index, stage in enumerate(stages, start=1):
            with self.subTest(stage=stage), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary) / "repo"
                root.mkdir()
                journal = SQLiteRunJournal(Path(temporary) / "state.db")
                direct = discover_direct_workspace(journal, root)
                snapshot = RuntimeSnapshot(
                    session_id=f"freeze-{index}", task="freeze", source_path=str(root),
                    state=RuntimeState.CREATED, policy=RunPolicy(),
                    source_fingerprint=tree_fingerprint(root),
                )
                digest = journal.canonical_admission_digest(
                    initial_request="freeze", repository_id=direct.repository_id,
                    project_scope_id=direct.project_scope.project_scope_id,
                    workspace_binding_id=direct.binding.workspace_binding_id,
                    snapshot=snapshot, policy=RunPolicy(), conversation_id=None,
                    expected_conversation_version=None,
                )
                admission = journal.admit_turn(
                    operation_id=f"freeze-admit-{index}", payload_digest=digest,
                    initial_request="freeze", repository_id=direct.repository_id,
                    project_scope_id=direct.project_scope.project_scope_id,
                    workspace_binding_id=direct.binding.workspace_binding_id,
                    snapshot=snapshot, policy=RunPolicy(),
                )
                host = AgentApplication(
                    Path(temporary) / "host", journal=journal, event_store=journal,
                    context_builder=BudgetedContextBuilder(),
                )
                session = journal.load_session(admission.legacy_session_id)
                backend = CrashBeforeProvider()
                runtime = AgentRuntime(
                    session=session, backend=backend,
                    context_builder=BudgetedContextBuilder(), registry=host.registry,
                    harness=host.harness, workspace_manager=host.workspace_manager,
                    recorder=TrajectoryRecorder(
                        journal, session.id, journal=journal,
                        snapshot_provider=session.to_snapshot,
                    ),
                )

                def fail(point: str) -> None:
                    if point == stage:
                        raise RuntimeError(point)

                journal.commit_hook = fail
                result = ConversationApplicationCoordinator(journal).run_execution(
                    ResumeExecutionCommand(admission.operation_id, admission.conversation_id),
                    runtime,
                )
                self.assertIs(result.state, RuntimeState.FAILED)
                self.assertEqual(backend.calls, 0)
                for table in (
                    "frozen_model_requests", "context_manifests", "model_attempts",
                    "context_operations", "context_selection_items", "model_calls",
                ):
                    self.assertEqual(journal.connection.execute(
                        f"SELECT COUNT(*) FROM {table}"  # noqa: S608 - fixed test table list
                    ).fetchone()[0], 0)
                journal.close()


if __name__ == "__main__":
    unittest.main()
