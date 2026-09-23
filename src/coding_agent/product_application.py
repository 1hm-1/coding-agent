"""Thin M2 Product application coordinator.

It contains no mutable aggregate, Runtime transition, ToolHarness call,
prompt/context composition, policy decision, writer ownership, or filesystem
authority.  Those remain in the repository/Runtime and later milestones.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import uuid
from typing import Callable, Mapping, Sequence, cast

from coding_agent.context import ConservativeTokenCounter, default_model_capabilities
from coding_agent.domain import Message, ModelRequest, RunPolicy, RunResult, RuntimeSnapshot, RuntimeState, Session
from coding_agent.persistence import ProductLifecycleConflict
from coding_agent.product_context import (
    CompositionBudget,
    ContextComposer,
    ContextRequiredContentExceedsBudget,
    ContextSource,
    ProductContextSnapshot,
    RuntimeContextSnapshot,
    project_conversation_transcript,
)
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


@dataclass(frozen=True)
class InstructionTrustCommand:
    operation_id: str
    payload_digest: str
    repository_id: str
    project_scope_id: str
    workspace_binding_id: str | None
    trusted: bool
    source_kind: str = "agents_md"


@dataclass(frozen=True)
class InstructionRefreshCommand:
    operation_id: str
    payload_digest: str
    conversation_id: str
    expected_version: int


@dataclass(frozen=True)
class PathInstructionActivationCommand:
    operation_id: str
    payload_digest: str
    conversation_id: str
    expected_version: int
    target_paths: tuple[str, ...]


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

    def configure_instruction_trust(self, command: InstructionTrustCommand) -> str:
        return self._repository.configure_instruction_trust(**command.__dict__)

    def refresh_instructions(self, command: InstructionRefreshCommand) -> str:
        return self._repository.refresh_instruction_manifest(
            **command.__dict__, reason="explicit_refresh", target_paths=(),
        )

    def activate_path_instructions(self, command: PathInstructionActivationCommand) -> str:
        return self._repository.refresh_instruction_manifest(
            **command.__dict__, reason="path_activation",
        )

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

    def _runtime_model_request_provider(
        self, admission: TurnAdmission, runtime: AgentRuntime,
    ) -> Callable[
        [str, Session, Sequence[Message], Sequence[Mapping[str, object]]],
        tuple[ModelRequest, Mapping[str, object]],
    ]:
        """Bridge the immutable Product snapshot into the real Runtime call gate."""
        provider_name = runtime.backend.name
        model_name = str(getattr(runtime.backend, "model", provider_name))
        builder = runtime.context_builder
        registry = getattr(builder, "capability_registry", None) or default_model_capabilities()
        capability = registry.resolve(provider_name, model_name)
        counter = getattr(builder, "token_counter", None) or capability.token_counter or ConservativeTokenCounter()
        composer = ContextComposer(
            token_counter=counter, provider=provider_name, model=model_name,
            capability_source=capability.source, capability_version=capability.version,
        )

        def provide(
            request_id: str, session: Session, messages: Sequence[Message],
            tools: Sequence[Mapping[str, object]],
        ) -> tuple[ModelRequest, Mapping[str, object]]:
            if session.id != admission.legacy_session_id:
                raise ProductLifecycleConflict("Runtime host does not match the admitted execution")
            snapshot = self._repository.load_m3_context_snapshot(admission.operation_id)
            semantic_events = cast(
                Sequence[Mapping[str, object]], snapshot["semantic_events"],
            )
            transcript = list(project_conversation_transcript(semantic_events))
            protocol_start = len(messages)
            if messages and messages[-1].role in {"tool", "assistant"}:
                for index in range(len(messages) - 1, -1, -1):
                    message = messages[index]
                    if message.role == "assistant" and message.metadata.get("tool_calls"):
                        if all(item.role == "tool" for item in messages[index + 1:]):
                            protocol_start = index
                        break
                    if message.role != "tool":
                        break
            protocol_group = tuple(
                message.to_dict() for message in messages[protocol_start:]
            )
            effective_intent = set(cast(Sequence[str], snapshot["current_intent"]))
            for index, message in enumerate(messages, start=1):
                if index > protocol_start or (
                    message.role == "user" and message.content in effective_intent
                ):
                    continue
                transcript.append(ContextSource(
                    "recent_transcript", f"runtime-message:{index}", message.content,
                    causal_relevance=index, recency=index, durable_sequence=index,
                ))
            for item in cast(Sequence[Mapping[str, object]], snapshot["typed_sources"]):
                transcript.append(ContextSource(
                    str(item["source_class"]), str(item["source_id"]),
                    str(item["content"]), status=str(item["status"]),
                    omission_reason=(str(item["reason"]) if item["reason"] is not None else None),
                    causal_relevance=1,
                ))
            tool_schemas: tuple[Mapping[str, object], ...] = tuple(tools)
            manifest_id = str(uuid.uuid5(
                uuid.NAMESPACE_URL, f"m3-context-manifest:{request_id}",
            ))
            try:
                composed = composer.compose(
                ProductContextSnapshot(
                    system_instructions="You are a coding agent operating under the durable Product contract.",
                    runtime_instructions=(
                        "Follow the active Runtime state and tool protocol; do not infer unrecorded effects."
                    ),
                    current_intent=tuple(cast(Sequence[str], snapshot["current_intent"])),
                    instruction_content=tuple(cast(Sequence[str], snapshot["instruction_content"])),
                    unresolved_constraints=(
                        ("Instruction sources changed after activation; frozen snapshots remain effective until explicit refresh.",)
                        if snapshot.get("instruction_status") == "stale" else ()
                    ),
                    optional_sources=tuple(transcript),
                ),
                RuntimeContextSnapshot(
                    causal_frontier=int(cast(int, snapshot["causal_frontier"])),
                    protocol_group=protocol_group,
                    resolver_outcomes=tuple(cast(Sequence[str], snapshot["resolver_outcomes"])),
                    tool_schemas=tool_schemas,
                ),
                CompositionBudget(
                    context_window=capability.context_limit,
                    output_reserve=session.policy.max_output_tokens,
                    framing_margin=capability.protocol_margin_tokens +
                    getattr(getattr(builder, "config", None), "protocol_margin_tokens", 0),
                ),
                request_id=request_id,
                context_manifest_id=manifest_id,
                max_output_tokens=session.policy.max_output_tokens,
                model_options={
                    "session_id": session.id,
                    "m3_instruction_manifest_id": str(snapshot["instruction_manifest_id"]),
                    "m3_instruction_manifest_revision": int(cast(int, snapshot["instruction_manifest_revision"])),
                    "m3_product_version": int(cast(int, snapshot["product_version"])),
                    "m3_causal_frontier": int(cast(int, snapshot["causal_frontier"])),
                    "m3_turn_id": str(snapshot["turn_id"]),
                    "m3_runtime_execution_id": str(snapshot["runtime_execution_id"]),
                    "m3_admission_operation_id": admission.operation_id,
                    "m3_capability_source": capability.source,
                    "m3_capability_version": capability.version,
                    "m3_provider": provider_name,
                    "m3_model": model_name,
                    "m3_file_sources": [
                        {"path": source["path"], "revision": source["revision"]}
                        for item in cast(Sequence[Mapping[str, object]], snapshot["typed_sources"])
                        if item["source_class"] == "file" and item["status"] == "eligible"
                        for source in (cast(Mapping[str, object], json.loads(str(item["content"]))),)
                    ],
                },
                )
            except ContextRequiredContentExceedsBudget as exc:
                self._repository.record_m3_context_failure(
                    operation_id=admission.operation_id,
                    proposed_request_id=request_id, status="overflow",
                    reason=str(exc), policy_version=composer.policy_version,
                )
                raise
            return ModelRequest.from_dict(composed.normalized_payload), composed.manifest_payload

        return provide

    def run_execution(
        self, command: ResumeExecutionCommand, runtime: AgentRuntime,
    ) -> RunResult:
        """Attach typed Product input to the real Runtime/Context path."""
        admission = self.resume_execution(command)
        if runtime.session.id != admission.legacy_session_id:
            raise ProductLifecycleConflict("Runtime host does not match the admitted execution")
        # A committed model call is resumed from its frozen request.  Even a
        # changed/deleted live file must not trigger a context reread here.
        frozen_call = (
            runtime.session.active_call_id is not None
            and self._repository.get_model_call(
                runtime.session.id, runtime.session.active_call_id,
            ) is not None
        )
        product_snapshot = (
            None if frozen_call else self._repository.load_m3_context_snapshot(admission.operation_id)
        )
        conflicts = tuple(cast(
            Sequence[str], product_snapshot.get("instruction_conflicts", ()) if product_snapshot else (),
        ))
        if conflicts:
            assert product_snapshot is not None
            conversation = self._repository.inspect_conversation(admission.conversation_id)
            if conversation is None:
                raise ProductLifecycleConflict("conversation was not found")
            outstanding = tuple(cast(
                Sequence[Mapping[str, object]],
                conversation.get("outstanding_input_requests", ()),
            ))
            if not outstanding:
                request_id = str(uuid.uuid5(
                    uuid.NAMESPACE_URL,
                    f"m3-instruction-conflict:{product_snapshot['instruction_manifest_id']}",
                ))
                payload = {
                    "prompt": "Resolve conflicting project instructions.",
                    "conflict_keys": list(conflicts),
                    "instruction_manifest_id": str(product_snapshot["instruction_manifest_id"]),
                }
                version = int(cast(int, conversation["product_version"]))
                self._repository.request_ordinary_input(
                    operation_id=f"m3-conflict-wait:{product_snapshot['instruction_manifest_id']}",
                    payload_digest=self._repository.canonical_input_digest(
                        conversation_id=admission.conversation_id,
                        input_kind="ordinary_input_request", payload=payload,
                        correlation_id=request_id,
                        expected_conversation_version=version,
                    ),
                    conversation_id=admission.conversation_id, request_id=request_id,
                    payload=payload, expected_conversation_version=version,
                )
            runtime.refresh_product_state()
        runtime.attach_product_input_provider(self._runtime_input_provider(admission))
        runtime.attach_product_model_request_provider(
            self._runtime_model_request_provider(admission, runtime),
        )
        if runtime.session.state not in {RuntimeState.CREATED, RuntimeState.WAITING_USER_INPUT}:
            runtime.resume()
        return runtime.run()

    def inspect_direct_startup(self, workspace_binding_id: str) -> dict[str, object]:
        """Read-only startup discovery of observations and coordination facts."""
        return self._repository.inspect_workspace_startup(workspace_binding_id)
