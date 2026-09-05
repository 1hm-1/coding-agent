from __future__ import annotations

import json
from dataclasses import replace
import tempfile
import unittest
from pathlib import Path

from coding_agent.compression import (
    CompressionEngine,
    RequiredFactVerifier,
    SummaryValidationError,
    stale_summary,
)
from coding_agent.context import (
    BudgetedContextBuilder,
    ConservativeTokenCounter,
    ContextBudgetConfig,
    ExactTokenCounter,
    ModelCapability,
    ModelCapabilityRegistry,
    PassthroughContextBuilder,
    UnknownModelCapability,
)
from coding_agent.domain import (
    ContextBuildInput,
    ContextBudgetError,
    EventType,
    FileFact,
    Message,
    RepositorySnapshot,
    RunPolicy,
    RuntimeState,
    SummaryRecord,
    TestFact,
)
from coding_agent.persistence import JournalMutation, SQLiteRunJournal, SummaryMutation
from coding_agent.models.scripted import ScriptedBackend


def _request(
    *,
    messages: tuple[Message, ...] = (),
    registry: ModelCapabilityRegistry | None = None,
    policy: RunPolicy | None = None,
) -> ContextBuildInput:
    return ContextBuildInput(
        session_id="context-session",
        task="keep the required task fact",
        messages=(Message(role="user", content="keep the required task fact"), *messages),
        runtime_state=RuntimeState.BUILDING_CONTEXT,
        policy=policy or RunPolicy(max_output_tokens=0),
        repository_snapshot=RepositorySnapshot(
            workspace_revision="revision-1",
            file_paths=("value.txt",),
            diff_summary="",
        ),
        latest_summary=None,
        provider="test",
        model="exact-model",
    )


class ContextEngineTest(unittest.TestCase):
    def _registry(self, limit: int = 2000) -> ModelCapabilityRegistry:
        counter = ExactTokenCounter(
            lambda _provider, _model, messages: sum(len(message.content) for message in messages)
        )
        return ModelCapabilityRegistry(
            [
                ModelCapability(
                    provider="test",
                    model="exact-model",
                    context_limit=limit,
                    token_counter=counter,
                )
            ]
        )

    def test_exact_and_unknown_model_fallback_are_explicit(self) -> None:
        request = _request(registry=self._registry())
        builder = BudgetedContextBuilder(
            capability_registry=self._registry(),
            token_counter=ExactTokenCounter(
                lambda _provider, _model, messages: sum(len(message.content) for message in messages)
            ),
        )
        built = builder.build(request)
        self.assertEqual(built.counter, "provider_exact")
        self.assertEqual(built.capability_source, "registry")

        fallback = ModelCapabilityRegistry(
            fallback=ModelCapability(
                provider="fallback",
                model="fallback",
                context_limit=1000,
                token_counter=ConservativeTokenCounter(),
            )
        )
        fallback_request = replace(request, provider="unknown", model="new-model")
        fallback_built = BudgetedContextBuilder(capability_registry=fallback).build(fallback_request)
        self.assertEqual(fallback_built.counter, "named_estimator")
        self.assertEqual(fallback_built.capability_source, "fallback")
        with self.assertRaises(UnknownModelCapability):
            BudgetedContextBuilder(
                capability_registry=ModelCapabilityRegistry(),
                allow_model_fallback=False,
            ).build(fallback_request)

    def test_sections_are_deterministic_and_passthrough_is_available(self) -> None:
        messages = (
            Message(role="assistant", content="old decision"),
            Message(role="tool", tool_call_id="read-1", content="old observation"),
        )
        request = _request(messages=messages, registry=self._registry())
        builder = BudgetedContextBuilder(capability_registry=self._registry())
        first = builder.build(request)
        second = builder.build(request)
        self.assertEqual(first.manifest(), second.manifest())
        self.assertEqual(
            [section.name for section in first.sections],
            ["system", "task_runtime", "repository", "summary", "recent"],
        )
        passthrough = PassthroughContextBuilder(capability_registry=self._registry()).build(request)
        self.assertEqual(len(passthrough.messages), len(request.messages) + 1)

    def test_soft_recent_history_is_trimmed_but_latest_observation_is_retained(self) -> None:
        messages = tuple(
            Message(role="assistant", content=f"old decision {index} " + "x" * 80)
            for index in range(4)
        ) + (
            Message(
                role="tool",
                tool_call_id="latest-test",
                content=json.dumps(
                    {
                        "tool_name": "restricted_test",
                        "data": {"profile": "python_unittest", "passed": True},
                    }
                ),
            ),
        )
        request = _request(messages=messages)
        builder = BudgetedContextBuilder(
            capability_registry=self._registry(limit=700),
            config=ContextBudgetConfig(protocol_margin_tokens=0),
        )
        built = builder.build(request)
        recent = next(section for section in built.sections if section.name == "recent")
        self.assertTrue(recent.truncated)
        self.assertIn("latest-test", {message.tool_call_id for message in recent.messages})
        self.assertLessEqual(built.total_input_tokens, built.budget_tokens)
        self.assertNotIn("old decision 0", " ".join(message.content for message in recent.messages))

    def test_tool_call_turn_is_not_split_when_recent_history_is_trimmed(self) -> None:
        messages = (
            Message(
                role="assistant",
                content="assistant response",
                metadata={
                    "tool_calls": [
                        {"id": "call-0", "name": "read_file", "arguments": {}},
                        {"id": "call-1", "name": "read_file", "arguments": {}},
                        {"id": "call-2", "name": "read_file", "arguments": {}},
                    ]
                },
            ),
            Message(role="tool", tool_call_id="call-0", content="first result"),
            Message(role="tool", tool_call_id="call-1", content="second result " * 100),
            Message(role="tool", tool_call_id="call-2", content="third result " * 20),
        )
        request = _request(messages=messages)
        builder = BudgetedContextBuilder(
            capability_registry=self._registry(limit=950),
            config=ContextBudgetConfig(protocol_margin_tokens=0),
        )

        with self.assertRaises(ContextBudgetError) as captured:
            builder.build(request)

        self.assertEqual(
            captured.exception.kind,
            "context_required_content_exceeds_budget",
        )

    def test_incomplete_tool_call_turn_fails_closed(self) -> None:
        messages = (
            Message(
                role="assistant",
                content="assistant response",
                metadata={
                    "tool_calls": [
                        {"id": "call-0", "name": "read_file", "arguments": {}},
                        {"id": "call-1", "name": "read_file", "arguments": {}},
                    ]
                },
            ),
            Message(role="tool", tool_call_id="call-0", content="first result"),
        )
        request = _request(messages=messages)

        with self.assertRaises(ContextBudgetError) as captured:
            BudgetedContextBuilder(
                capability_registry=self._registry(),
                config=ContextBudgetConfig(protocol_margin_tokens=0),
            ).build(request)

        self.assertEqual(captured.exception.kind, "context_invalid_tool_history")

    def test_required_content_over_budget_is_classified(self) -> None:
        request = _request()
        with self.assertRaises(ContextBudgetError) as captured:
            BudgetedContextBuilder(
                capability_registry=self._registry(limit=80),
                config=ContextBudgetConfig(protocol_margin_tokens=0),
            ).build(request)
        self.assertEqual(captured.exception.kind, "context_required_content_exceeds_budget")

    def test_high_watermark_and_target_use_assembled_input_boundaries(self) -> None:
        counter = ExactTokenCounter(
            lambda _provider, _model, messages: len(messages)
        )
        registry = ModelCapabilityRegistry(
            [
                ModelCapability(
                    provider="test",
                    model="exact-model",
                    context_limit=5,
                    token_counter=counter,
                )
            ]
        )
        request = _request()
        builder = BudgetedContextBuilder(
            capability_registry=registry,
            token_counter=counter,
            system_policy="",
        )
        at_boundary = builder.build_unbounded(request)
        self.assertEqual(at_boundary.total_input_tokens, 4)
        self.assertEqual(at_boundary.high_watermark_tokens, 4)
        self.assertEqual(at_boundary.target_after_compression_tokens, 3)
        self.assertFalse(at_boundary.needs_compression)

        tight_registry = ModelCapabilityRegistry(
            [
                ModelCapability(
                    provider="test",
                    model="exact-model",
                    context_limit=4,
                    token_counter=counter,
                )
            ]
        )
        over_boundary = BudgetedContextBuilder(
            capability_registry=tight_registry,
            token_counter=counter,
            system_policy="",
        ).build_unbounded(request)
        self.assertEqual(over_boundary.high_watermark_tokens, 3)
        self.assertEqual(over_boundary.target_after_compression_tokens, 2)
        self.assertTrue(over_boundary.needs_compression)


class SummaryAndPersistenceTest(unittest.TestCase):
    def _summary(self) -> SummaryRecord:
        return SummaryRecord(
            summary_id="summary-1",
            schema_version=1,
            session_id="summary-session",
            source_event_start=3,
            source_event_end=4,
            source_event_hash="event-hash",
            workspace_revision="revision-1",
            goals=("keep the required task fact",),
            files_read=(FileFact("value.txt", "file-hash", "revision-1"),),
            tests=(TestFact("python_unittest", True),),
        )

    def test_summary_round_trip_and_stale_invalidation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            journal = SQLiteRunJournal(root / "state.db")
            from coding_agent.domain import Session

            session = Session(
                id="summary-session",
                task="keep the required task fact",
                source_path=str(root),
                state=RuntimeState.BUILDING_CONTEXT,
                policy=RunPolicy(),
                source_fingerprint="source",
            )
            journal.create_session(session.to_snapshot(), Message(role="user", content=session.task))
            candidate = replace(session.to_snapshot(), state=RuntimeState.BUILDING_CONTEXT)
            summary = self._summary()
            journal.commit(
                JournalMutation(
                    session_id=session.id,
                    expected_version=0,
                    expected_state=RuntimeState.BUILDING_CONTEXT,
                    snapshot_after=candidate,
                    event_type=EventType.COMPRESSION_FINISHED,
                    payload={"summary_id": summary.summary_id},
                    summary=SummaryMutation(record=summary),
                )
            )
            self.assertEqual(journal.get_latest_summary(session.id), summary)
            self.assertEqual(journal.list_summaries(session.id), [summary])
            stale = stale_summary(
                summary,
                RepositorySnapshot(
                    workspace_revision="revision-2",
                    read_files=(FileFact("value.txt", "new-hash", "revision-2"),),
                ),
            )
            self.assertTrue(stale.stale)
            self.assertTrue(stale.files_read[0].stale)
            journal.close()

    def test_summary_verifier_rejects_missing_required_facts(self) -> None:
        verifier = RequiredFactVerifier()
        with self.assertRaises(SummaryValidationError):
            verifier.verify(
                SummaryRecord(
                    summary_id="bad",
                    schema_version=1,
                    session_id="s",
                    source_event_start=1,
                    source_event_end=1,
                    source_event_hash="hash",
                    workspace_revision="revision",
                ),
                task="task",
                messages=(),
                events=(),
                repository_snapshot=RepositorySnapshot("revision"),
            )

    def test_compression_schema_or_provider_failure_falls_back_to_raw_history(self) -> None:
        from coding_agent.application import AgentApplication

        for summarizer_response in (
            {"final": "not-json"},
            {"error": {"kind": "timeout", "message": "summary timed out"}},
        ):
            with self.subTest(summarizer_response=summarizer_response), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                source = root / "source"
                source.mkdir()
                (source / "value.txt").write_text("value\n" + "x" * 700, encoding="utf-8")
                counter = ExactTokenCounter(
                    lambda _provider, _model, messages: sum(
                        len(message.content) for message in messages
                    )
                )
                registry = ModelCapabilityRegistry(
                    [
                        ModelCapability(
                            provider="scripted",
                            model="scripted",
                            context_limit=2000,
                            token_counter=counter,
                        )
                    ]
                )
                application = AgentApplication(
                    root / "agent-home",
                    context_builder=BudgetedContextBuilder(
                        capability_registry=registry,
                        config=ContextBudgetConfig(protocol_margin_tokens=0),
                    ),
                    compression_engine=CompressionEngine(
                        ScriptedBackend([summarizer_response])
                    ),
                )
                result = application.run_task(
                    source=source,
                    task="Read the value and answer",
                    backend=ScriptedBackend(
                        [
                            {
                                "tool_calls": [
                                    {
                                        "id": "read-1",
                                        "name": "read_file",
                                        "arguments": {"path": "value.txt"},
                                    }
                                ]
                            },
                            {"final": "value"},
                        ]
                    ),
                    policy=RunPolicy(max_output_tokens=0),
                )
                self.assertEqual(result.state, RuntimeState.COMPLETED, result.failure)
                events = application.journal.list_events(result.session_id)
                rejected = [
                    event
                    for event in events
                    if event.event_type is EventType.COMPRESSION_REJECTED
                ]
                self.assertTrue(rejected)
                self.assertEqual(application.journal.list_summaries(result.session_id), [])

    def test_compression_round_trip_is_observable_and_keeps_raw_events(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            source.mkdir()
            (source / "value.txt").write_text("value\n" + "x" * 2000, encoding="utf-8")
            from coding_agent.application import AgentApplication

            counter = ExactTokenCounter(
                lambda _provider, _model, messages: sum(
                    len(message.content) for message in messages
                )
            )
            registry = ModelCapabilityRegistry(
                [
                    ModelCapability(
                        provider="scripted",
                        model="scripted",
                        context_limit=1200,
                        token_counter=counter,
                    )
                ]
            )
            from coding_agent.context import BudgetedContextBuilder

            summary_payload = {
                "schema_version": 1,
                "goals": ["Read the value and answer"],
                "constraints": [],
                "decisions": ["Use the read observation"],
                "files_read": [],
                "edits": [],
                "tests": [],
                "errors": [],
                "unresolved": [],
            }
            application = AgentApplication(
                root / "agent-home",
                context_builder=BudgetedContextBuilder(
                    capability_registry=registry,
                    config=ContextBudgetConfig(protocol_margin_tokens=0),
                ),
                compression_engine=CompressionEngine(
                    ScriptedBackend([{"final": json.dumps(summary_payload)}])
                ),
            )
            result = application.run_task(
                source=source,
                task="Read the value and answer",
                backend=ScriptedBackend(
                    [
                        {
                            "tool_calls": [
                                {
                                    "id": "read-1",
                                    "name": "read_file",
                                    "arguments": {"path": "value.txt"},
                                }
                            ]
                        },
                        {"final": "value"},
                    ]
                ),
                policy=RunPolicy(max_output_tokens=0),
            )
            self.assertEqual(result.state, RuntimeState.COMPLETED, result.failure)
            events = application.journal.list_events(result.session_id)
            self.assertIn(EventType.COMPRESSION_STARTED, [event.event_type for event in events])
            self.assertIn(EventType.COMPRESSION_FINISHED, [event.event_type for event in events])
            summaries = application.journal.list_summaries(result.session_id)
            self.assertEqual(len(summaries), 1)
            self.assertEqual(
                application.replay_session(result.session_id).final_state,
                RuntimeState.COMPLETED,
            )
            self.assertEqual(
                len([event for event in events if event.event_type is EventType.MESSAGE_ADDED]),
                4,
            )


if __name__ == "__main__":
    unittest.main()
