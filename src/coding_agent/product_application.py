"""Thin M2 Product application coordinator.

It contains no mutable aggregate, Runtime transition, ToolHarness call,
prompt/context composition, policy decision, writer ownership, or filesystem
authority.  Those remain in the repository/Runtime and later milestones.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Mapping

from coding_agent.domain import Message, RunPolicy, RunResult, RuntimeSnapshot, RuntimeState, Session
from coding_agent.persistence import ProductLifecycleConflict
from coding_agent.product_domain import TurnAdmission
from coding_agent.product_persistence import ProductRepository
from coding_agent.runtime import AgentRuntime


@dataclass(frozen=True)
class AdmissionCommand:
    operation_id: str
    payload_digest: str
    initial_request: str
    repository_id: str
    project_scope_id: str
    workspace_binding_id: str
    snapshot: RuntimeSnapshot
    policy: RunPolicy
    conversation_id: str | None = None
    expected_conversation_version: int | None = None


@dataclass(frozen=True)
class SteeringCommand:
    operation_id: str
    payload_digest: str
    conversation_id: str
    expected_conversation_version: int
    text: str


@dataclass(frozen=True)
class UserReplyCommand:
    operation_id: str
    payload_digest: str
    conversation_id: str
    expected_conversation_version: int
    correlation_id: str
    text: str


@dataclass(frozen=True)
class CancelControlCommand:
    operation_id: str
    payload_digest: str
    conversation_id: str
    expected_conversation_version: int
    reason: str = "user_requested"


@dataclass(frozen=True)
class OrdinaryInputRequestCommand:
    operation_id: str
    payload_digest: str
    conversation_id: str
    expected_conversation_version: int
    request_id: str
    prompt: str


@dataclass(frozen=True)
class RebindCommand:
    operation_id: str
    payload_digest: str
    conversation_id: str
    workspace_binding_id: str
    expected_version: int
    observation: Mapping[str, object]


@dataclass(frozen=True)
class ResumeExecutionCommand:
    admission_operation_id: str
    conversation_id: str


class ConversationApplicationCoordinator:
    """Stateless routing from typed Product commands to the repository port."""

    def __init__(self, repository: ProductRepository):
        self._repository = repository

    def admit(self, command: AdmissionCommand) -> TurnAdmission:
        return self._repository.admit_turn(**command.__dict__)

    def steer(self, command: SteeringCommand) -> str:
        return self._repository.append_product_input(
            operation_id=command.operation_id, payload_digest=command.payload_digest,
            conversation_id=command.conversation_id, input_kind="steering",
            payload={"text": command.text},
            expected_conversation_version=command.expected_conversation_version,
        )

    def reply(self, command: UserReplyCommand) -> str:
        return self._repository.append_product_input(
            operation_id=command.operation_id, payload_digest=command.payload_digest,
            conversation_id=command.conversation_id, input_kind="reply",
            payload={"text": command.text}, correlation_id=command.correlation_id,
            expected_conversation_version=command.expected_conversation_version,
        )

    def request_cancel(self, command: CancelControlCommand) -> str:
        return self._repository.append_product_input(
            operation_id=command.operation_id, payload_digest=command.payload_digest,
            conversation_id=command.conversation_id, input_kind="cancel",
            payload={"reason": command.reason},
            expected_conversation_version=command.expected_conversation_version,
        )

    def rebind_between_turns(
        self, command: RebindCommand,
    ) -> int:
        return self._repository.rebind_conversation(
            operation_id=command.operation_id, payload_digest=command.payload_digest,
            conversation_id=command.conversation_id,
            workspace_binding_id=command.workspace_binding_id,
            expected_version=command.expected_version, observation=command.observation,
        )

    def request_ordinary_input(self, command: OrdinaryInputRequestCommand) -> str:
        return self._repository.request_ordinary_input(
            operation_id=command.operation_id, payload_digest=command.payload_digest,
            conversation_id=command.conversation_id, request_id=command.request_id,
            payload={"prompt": command.prompt},
            expected_conversation_version=command.expected_conversation_version,
        )

    def consume_steering_at_safe_boundary(
        self, *, conversation_id: str, legacy_session_id: str,
        expected_conversation_version: int,
    ) -> list[str]:
        return self._repository.consume_steering_inputs(
            conversation_id=conversation_id, legacy_session_id=legacy_session_id,
            expected_conversation_version=expected_conversation_version,
        )

    def cancel_at_safe_boundary(
        self, *, conversation_id: str, legacy_session_id: str, operation_id: str,
        expected_conversation_version: int,
    ) -> None:
        self._repository.apply_cancel_at_safe_boundary(
            conversation_id=conversation_id, legacy_session_id=legacy_session_id,
            operation_id=operation_id,
            expected_conversation_version=expected_conversation_version,
        )

    def resume_conversation(self, conversation_id: str) -> dict[str, object]:
        """Inspect durable continuity only; never starts a Runtime execution."""
        conversation = self._repository.inspect_conversation(conversation_id)
        if conversation is None:
            raise LookupError("Conversation was not found")
        return conversation

    def resume_execution(self, command: ResumeExecutionCommand) -> TurnAdmission:
        """Validate one exact unfinished execution without starting it."""
        admission = self._repository.resume_execution(command.admission_operation_id)
        if admission.conversation_id != command.conversation_id:
            raise ProductLifecycleConflict("execution does not belong to the requested Conversation")
        return admission

    def _runtime_input_provider(
        self, admission: TurnAdmission,
    ) -> Callable[[Session], list[Message]]:
        def provide(session: Session) -> list[Message]:
            if session.id != admission.legacy_session_id:
                raise ProductLifecycleConflict("Runtime host does not match the admitted execution")
            input_ids = self._repository.consume_current_steering_inputs(
                conversation_id=admission.conversation_id,
                legacy_session_id=admission.legacy_session_id,
            )
            wanted = set(input_ids)
            return [
                message for message in self._repository.list_messages(admission.legacy_session_id)
                if str(message.metadata.get("m2_input_id")) in wanted
            ]
        return provide

    def run_execution(
        self, command: ResumeExecutionCommand, runtime: AgentRuntime,
    ) -> RunResult:
        """Attach typed Product input to the real Runtime/Context path."""
        admission = self.resume_execution(command)
        if runtime.session.id != admission.legacy_session_id:
            raise ProductLifecycleConflict("Runtime host does not match the admitted execution")
        runtime.attach_product_input_provider(self._runtime_input_provider(admission))
        if runtime.session.state is not RuntimeState.CREATED:
            runtime.resume()
        return runtime.run()

    def inspect_direct_startup(self, workspace_binding_id: str) -> dict[str, object]:
        """Read-only startup discovery of observations and coordination facts."""
        return self._repository.inspect_workspace_startup(workspace_binding_id)
