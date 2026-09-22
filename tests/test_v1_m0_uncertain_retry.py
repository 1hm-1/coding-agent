from __future__ import annotations

from dataclasses import replace
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from coding_agent.application import AgentApplication
from coding_agent.context import BudgetedContextBuilder
from coding_agent.domain import EventType, Message, ModelResponse, RunPolicy, RuntimeState, Usage


class SimulatedHostLoss(BaseException):
    pass


def request_digest(value: object) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class MutableContextBuilder:
    def __init__(self, marker: str):
        self.marker = marker
        self.base = BudgetedContextBuilder()

    def build(self, request):
        built = self.base.build(request)
        marker = Message(role="user", content=f"test-controlled-context:{self.marker}")
        return replace(built, messages=(*built.messages, marker))


class CrashAfterProviderReceipt:
    name = "m0-fake-provider"

    def __init__(self):
        self.requests = []

    def complete(self, request):
        self.requests.append(request)
        raise SimulatedHostLoss("provider received request before host loss")


class CapturingProvider:
    name = "m0-fake-provider"

    def __init__(self):
        self.requests = []

    def complete(self, request):
        self.requests.append(request)
        return ModelResponse(text="done", usage=Usage(input_tokens=7, output_tokens=1))


class M0UncertainRetryCharacterizationTest(unittest.TestCase):
    def _source(self, root: Path) -> Path:
        source = root / "source"
        source.mkdir()
        (source / "value.txt").write_text("stable\n", encoding="utf-8")
        return source

    def _reproduce(self):
        temporary = tempfile.TemporaryDirectory(prefix="coding-agent-v1-m0-retry-")
        root = Path(temporary.name)
        builder = MutableContextBuilder("A")
        application = AgentApplication(root / "agent-home", context_builder=builder)
        first_provider = CrashAfterProviderReceipt()
        policy = RunPolicy(
            max_retries=1,
            retry_base_delay_seconds=0.0,
            retry_max_delay_seconds=0.0,
            retry_jitter_seconds=0.0,
        )
        with self.assertRaises(SimulatedHostLoss):
            application.run_task(
                source=self._source(root),
                task="Characterize uncertain retry.",
                backend=first_provider,
                policy=policy,
                session_id="m0-uncertain-retry",
            )
        session_id = str(application.list_sessions()[0]["id"])
        original = first_provider.requests[0].to_dict()
        persisted_before = application.journal.list_model_calls(session_id)[0]
        self.assertEqual(persisted_before["status"], "running")
        self.assertEqual(persisted_before["request"], original)

        builder.marker = "B"
        resumed_provider = CapturingProvider()
        result = application.resume_session(
            session_id,
            backend=resumed_provider,
            clock=lambda: 0.0,
            sleeper=lambda _: None,
            random_source=lambda: 0.5,
        )
        persisted_after = application.journal.list_model_calls(session_id)[0]
        events = application.journal.list_events(session_id)
        evidence = {
            "request_id": original["request_id"],
            "original_digest": request_digest(original),
            "resumed_digest": request_digest(resumed_provider.requests[0].to_dict()),
            "stored_digest_after_resume": request_digest(persisted_after["request"]),
            "original_request": original,
            "resumed_request": resumed_provider.requests[0].to_dict(),
            "stored_request_after_resume": persisted_after["request"],
            "status_before_resume": persisted_before["status"],
            "status_after_resume": persisted_after["status"],
            "attempt_after_resume": persisted_after["attempt"],
            "journal_events": [
                event.to_dict()
                for event in events
                if event.event_type
                in {
                    EventType.MODEL_CALL_STARTED,
                    EventType.MODEL_CALL_UNCERTAIN,
                    EventType.RETRY_SCHEDULED,
                    EventType.MODEL_CALL_SUCCEEDED,
                }
            ],
            "event_types": [event.event_type.value for event in events],
            "source_content_after_resume": (root / "source" / "value.txt").read_text(
                encoding="utf-8"
            ),
        }
        return temporary, application, result, first_provider, resumed_provider, evidence

    def _committed_response_control(self) -> dict[str, object]:
        with tempfile.TemporaryDirectory(prefix="coding-agent-v1-m0-committed-") as temporary:
            root = Path(temporary)
            builder = MutableContextBuilder("A")
            application = AgentApplication(root / "agent-home", context_builder=builder)

            def crash(stage: str) -> None:
                if stage == "after_model_response":
                    raise SimulatedHostLoss(stage)

            first = CapturingProvider()
            with self.assertRaises(SimulatedHostLoss):
                application.run_task(
                    source=self._source(root),
                    task="Reuse committed response.",
                    backend=first,
                    fault_injector=crash,
                )
            session_id = str(application.list_sessions()[0]["id"])
            before = application.journal.list_model_calls(session_id)[0]
            builder.marker = "B"
            resumed = CapturingProvider()
            result = application.resume_session(session_id, backend=resumed)
            after = application.journal.list_model_calls(session_id)[0]
            evidence: dict[str, object] = {
                "final_state": result.state.value,
                "first_provider_calls": len(first.requests),
                "resumed_provider_calls": len(resumed.requests),
                "request_digest_before_resume": request_digest(before["request"]),
                "request_digest_after_resume": request_digest(after["request"]),
                "source_content_after_resume": (root / "source" / "value.txt").read_text(
                    encoding="utf-8"
                ),
            }
            application.close()
            return evidence

    def test_current_behavior_reconstructs_and_overwrites_uncertain_request(self) -> None:
        temporary, application, result, first_provider, resumed_provider, evidence = self._reproduce()
        self.addCleanup(temporary.cleanup)
        self.addCleanup(application.close)
        self.assertIs(result.state, RuntimeState.COMPLETED)
        self.assertEqual(len(first_provider.requests), 1)
        self.assertEqual(len(resumed_provider.requests), 1)
        self.assertNotEqual(evidence["original_digest"], evidence["resumed_digest"])
        self.assertEqual(evidence["resumed_digest"], evidence["stored_digest_after_resume"])
        self.assertEqual(evidence["attempt_after_resume"], 2)
        self.assertIn(EventType.MODEL_CALL_UNCERTAIN.value, evidence["event_types"])
        self.assertIn(EventType.RETRY_SCHEDULED.value, evidence["event_types"])

    @unittest.expectedFailure
    def test_future_m3_invariant_reuses_exact_frozen_request(self) -> None:
        temporary, application, _, _, _, evidence = self._reproduce()
        self.addCleanup(temporary.cleanup)
        self.addCleanup(application.close)
        self.assertEqual(evidence["original_digest"], evidence["resumed_digest"])

    def test_committed_response_is_reused_without_another_provider_call(self) -> None:
        evidence = self._committed_response_control()
        self.assertEqual(evidence["final_state"], RuntimeState.COMPLETED.value)
        self.assertEqual(evidence["resumed_provider_calls"], 0)
        self.assertEqual(
            evidence["request_digest_before_resume"],
            evidence["request_digest_after_resume"],
        )


if __name__ == "__main__":
    unittest.main()
