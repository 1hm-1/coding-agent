"""Stateless deterministic M3 Context composition and evidence projections."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import math
from pathlib import Path
import uuid
from typing import Iterable, Mapping, Sequence

from coding_agent.context import ConservativeTokenCounter, TokenCounter
from coding_agent.domain import ContextBudgetError, Message
from coding_agent.product_domain import (
    ContextSelection,
    FileContextItem,
    FrozenContextPackage,
    NormalizedObservation,
    SummaryArtifact,
    SummaryClaim,
    ToolResultArtifact,
)


CONTEXT_POLICY_VERSION = "m3-hybrid-v1"
TOKEN_COUNTER_VERSION = "utf8-bytes-ceil4-v1"
OPTIONAL_CLASS_ORDER = ("recent_transcript", "observation", "file", "summary", "memory")
CLASS_RESERVATIONS: Mapping[str, tuple[int, int]] = {
    "recent_transcript": (128, 1024),
    "observation": (128, 768),
    "file": (128, 1024),
    "summary": (64, 512),
    "memory": (0, 0),
}


class ContextRequiredContentExceedsBudget(ContextBudgetError):
    kind = "context_required_content_exceeds_budget"

    def __init__(self, message: str) -> None:
        super().__init__(message, kind=self.kind)


def canonical_json(value: object) -> str:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False,
    )


def digest_json(value: object) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def estimate_tokens(value: str) -> int:
    if not value:
        return 0
    return max(1, math.ceil(len(value.encode("utf-8")) / 4))


def required_int(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise ValueError(f"{name} must be an integer")
    return int(value)


@dataclass(frozen=True)
class ContextSource:
    source_class: str
    source_id: str
    content: str
    authority: int = 0
    causal_relevance: int = 0
    semantic_score: int = 0
    recency: int = 0
    durable_sequence: int = 0
    status: str = "eligible"
    omission_reason: str | None = None
    atomic: bool = False

    @property
    def digest(self) -> str:
        return hashlib.sha256(self.content.encode("utf-8")).hexdigest()

    @property
    def token_count(self) -> int:
        return estimate_tokens(self.content)


@dataclass(frozen=True)
class ProductContextSnapshot:
    system_instructions: str
    runtime_instructions: str
    current_intent: tuple[str, ...]
    instruction_content: tuple[str, ...]
    unresolved_constraints: tuple[str, ...] = ()
    optional_sources: tuple[ContextSource, ...] = ()


@dataclass(frozen=True)
class RuntimeContextSnapshot:
    causal_frontier: int
    protocol_group: tuple[Mapping[str, object], ...] = ()
    resolver_outcomes: tuple[str, ...] = ()
    tool_schemas: tuple[Mapping[str, object], ...] = ()


@dataclass(frozen=True)
class CompositionBudget:
    context_window: int
    output_reserve: int
    framing_margin: int

    def __post_init__(self) -> None:
        if self.context_window <= 0 or min(self.output_reserve, self.framing_margin) < 0:
            raise ValueError("invalid context budget")


@dataclass(frozen=True)
class ComposedContext:
    package: FrozenContextPackage
    normalized_payload: Mapping[str, object]
    manifest_payload: Mapping[str, object]
    request_digest: str
    manifest_digest: str


class ContextComposer:
    """Pure hybrid allocator; identical versioned input produces identical bytes."""

    policy_version = CONTEXT_POLICY_VERSION
    token_counter_version = TOKEN_COUNTER_VERSION

    def __init__(
        self, *, token_counter: TokenCounter | None = None,
        provider: str = "", model: str = "", capability_source: str = "",
        capability_version: str = "",
    ) -> None:
        self._token_counter = token_counter
        self._provider = provider
        self._model = model
        self._capability_source = capability_source
        self._capability_version = capability_version
        if token_counter is not None:
            self.token_counter_version = token_counter.name

    def _count(self, content: str) -> int:
        # Source-level estimates drive allocation only. Provider counters must
        # see the complete, correctly-ordered message sequence exactly as sent.
        return estimate_tokens(content)

    def _count_request(
        self, messages: Sequence[Mapping[str, object]],
        tools: Sequence[Mapping[str, object]], tool_estimate: int,
    ) -> tuple[int, str, str]:
        normalized = tuple(Message.from_dict(item) for item in messages)
        counter = self._token_counter
        if counter is None:
            return (
                ConservativeTokenCounter().count_messages(self._provider, self._model, normalized)
                + tool_estimate,
                "named_estimator", "conservative_messages_and_tools",
            )
        request_counter = getattr(counter, "count_request", None)
        if callable(request_counter):
            total = request_counter(self._provider, self._model, normalized, tuple(tools))
            if isinstance(total, bool) or not isinstance(total, int) or total < 0:
                raise ValueError("provider request counter must return a non-negative integer")
            return total, counter.name, "provider_request_counter"
        message_tokens = counter.count_messages(self._provider, self._model, normalized)
        return (
            message_tokens + tool_estimate, counter.name,
            "provider_messages_conservative_tools" if tools else "provider_messages_no_tools",
        )

    def compose(
        self,
        product: ProductContextSnapshot,
        runtime: RuntimeContextSnapshot,
        budget: CompositionBudget,
        *,
        request_id: str,
        context_manifest_id: str,
        max_output_tokens: int,
        model_options: Mapping[str, object] | None = None,
    ) -> ComposedContext:
        tool_schema_json = canonical_json(runtime.tool_schemas)
        tool_schema_tokens = (
            estimate_tokens(tool_schema_json) + 8 * len(runtime.tool_schemas)
            if runtime.tool_schemas else 0
        )
        available = (
            budget.context_window - budget.output_reserve - budget.framing_margin
            - tool_schema_tokens
        )
        required_sources: list[ContextSource] = [
            ContextSource("required", "system", product.system_instructions, authority=100, atomic=True),
            ContextSource("required", "runtime", product.runtime_instructions, authority=100, atomic=True),
        ]
        required_sources.extend(
            ContextSource("required", f"intent:{index}", content, authority=100, atomic=True)
            for index, content in enumerate(product.current_intent, start=1)
        )
        required_sources.extend(
            ContextSource("required", f"instruction:{index}", content, authority=90, atomic=True)
            for index, content in enumerate(product.instruction_content, start=1)
        )
        if runtime.protocol_group:
            required_sources.append(ContextSource(
                "required", "active_protocol_group", canonical_json(runtime.protocol_group),
                authority=100, atomic=True,
            ))
        required_sources.extend(
            ContextSource("required", f"resolver:{index}", content, authority=100, atomic=True)
            for index, content in enumerate(runtime.resolver_outcomes, start=1)
        )
        required_sources.extend(
            ContextSource("required", f"constraint:{index}", content, authority=100, atomic=True)
            for index, content in enumerate(product.unresolved_constraints, start=1)
        )
        required_tokens = sum(self._count(source.content) for source in required_sources)
        # Estimates may overstate or understate a provider tokenizer. The
        # complete normalized request below is the authoritative overflow gate.
        remaining = max(0, available - required_tokens)
        selections: list[ContextSelection] = [
            ContextSelection(
                source.source_class, source.source_id, "included", "required_non_evictable",
                self._count(source.content), source.digest,
            )
            for source in required_sources
        ]
        included_optional: list[ContextSource] = []
        optional_by_class = {
            source_class: [
                source for source in product.optional_sources if source.source_class == source_class
            ]
            for source_class in OPTIONAL_CLASS_ORDER
        }
        ordered_by_class: dict[str, list[ContextSource]] = {}
        for source_class in OPTIONAL_CLASS_ORDER:
            ordered_by_class[source_class] = sorted(
                optional_by_class[source_class],
                key=lambda source: (
                    -source.authority, -source.causal_relevance, -source.semantic_score,
                    -source.recency, -source.durable_sequence, source.source_id,
                ),
            )
        # Reserve each class's minimum before any class borrows surplus.
        reservations: dict[str, int] = {}
        unreserved = remaining
        for source_class in OPTIONAL_CLASS_ORDER:
            minimum, maximum = CLASS_RESERVATIONS[source_class]
            demand = sum(
                self._count(source.content) for source in ordered_by_class[source_class]
                if source.status == "eligible" and source_class != "memory"
            )
            reservation = min(minimum, maximum, demand, unreserved)
            reservations[source_class] = reservation
            unreserved -= reservation
        selected: set[tuple[str, str]] = set()
        spent: dict[str, int] = {}
        # First pass consumes only each class's protected reservation.
        for source_class in OPTIONAL_CLASS_ORDER:
            used = 0
            for source in ordered_by_class[source_class]:
                cost = self._count(source.content)
                if (
                    source_class != "memory" and source.status == "eligible"
                    and cost <= reservations[source_class] - used
                ):
                    selected.add((source_class, source.source_id))
                    used += cost
            spent[source_class] = used
        # Then lend every token left unspent by reservations in fixed class
        # order. Atomic candidates never consume a partial allocation.
        lendable = remaining - sum(spent.values())
        for source_class in OPTIONAL_CLASS_ORDER:
            maximum = CLASS_RESERVATIONS[source_class][1]
            for source in ordered_by_class[source_class]:
                key = (source_class, source.source_id)
                cost = self._count(source.content)
                if (
                    key not in selected and source_class != "memory"
                    and source.status == "eligible"
                    and cost <= lendable and cost <= maximum - spent[source_class]
                ):
                    selected.add(key)
                    spent[source_class] += cost
                    lendable -= cost
        for source_class in OPTIONAL_CLASS_ORDER:
            for source in ordered_by_class[source_class]:
                if source_class == "memory":
                    disposition, reason = "omitted", "memory_absent"
                elif source.status != "eligible":
                    disposition = source.status if source.status in {
                        "stale", "inactive", "untrusted", "superseded",
                    } else "omitted"
                    reason = source.omission_reason or f"source_{source.status}"
                elif (source_class, source.source_id) in selected:
                    disposition, reason = "included", "deterministic_hybrid_allocation"
                    included_optional.append(source)
                else:
                    disposition, reason = "omitted", "class_or_total_budget_exhausted"
                selections.append(ContextSelection(
                    source_class, source.source_id, disposition, reason,
                    self._count(source.content), source.digest,
                ))

        def reconciled_allocation() -> tuple[dict[str, int], dict[str, int],
                                             dict[str, int], dict[str, int], int]:
            # The provider's exact request count can force further evictions
            # after estimated class allocation.  The manifest describes the
            # *final* selected set, never the pre-eviction allocation.
            final_spent = {
                source_class: sum(
                    item.token_count for item in selections
                    if item.source_class == source_class and item.disposition == "included"
                ) for source_class in OPTIONAL_CLASS_ORDER
            }
            if sum(final_spent.values()) != sum(
                self._count(source.content) for source in included_optional
            ):
                raise ValueError("optional selection ledger does not match normalized request")
            final_lending = {
                source_class: max(0, final_spent[source_class] - reservations[source_class])
                for source_class in OPTIONAL_CLASS_ORDER
            }
            final_released = {
                source_class: max(0, reservations[source_class] - final_spent[source_class])
                for source_class in OPTIONAL_CLASS_ORDER
            }
            final_lendable = remaining - sum(final_spent.values())
            if final_lendable < 0:
                raise ValueError("optional selection exceeds its estimated allocation")
            return final_spent, final_lending, final_released, dict(final_spent), final_lendable

        spent, lending, released, allocation, lendable = reconciled_allocation()

        def normalized_messages() -> list[Mapping[str, object]]:
            result: list[Mapping[str, object]] = []
            for source in required_sources:
                if source.source_id == "active_protocol_group":
                    result.extend(dict(message) for message in runtime.protocol_group)
                    continue
                role = "system" if source.source_id in {"system", "runtime"} else "user"
                result.append({
                    "role": role, "content": source.content, "tool_call_id": None,
                    "metadata": {"m3_source_id": source.source_id},
                })
            result.extend({
                "role": "user", "content": source.content, "tool_call_id": None,
                "metadata": {"m3_source_id": source.source_id},
            } for source in included_optional)
            return result

        messages = normalized_messages()
        total_request_tokens, counter_identity, tool_accounting = self._count_request(
            messages, runtime.tool_schemas, tool_schema_tokens,
        )
        while total_request_tokens + budget.output_reserve + budget.framing_margin > budget.context_window:
            if not included_optional:
                raise ContextRequiredContentExceedsBudget(
                    "the exact normalized required request exceeds the model context window"
                )
            removed = included_optional.pop()
            selections = [
                ContextSelection(
                    item.source_class, item.source_id,
                    "omitted" if item.source_class == removed.source_class
                    and item.source_id == removed.source_id else item.disposition,
                    "exact_request_overflow" if item.source_class == removed.source_class
                    and item.source_id == removed.source_id else item.reason,
                    item.token_count, item.source_digest,
                ) for item in selections
            ]
            spent, lending, released, allocation, lendable = reconciled_allocation()
            messages = normalized_messages()
            total_request_tokens, counter_identity, tool_accounting = self._count_request(
                messages, runtime.tool_schemas, tool_schema_tokens,
            )
        optional_tokens = sum(spent.values())
        package = FrozenContextPackage(
            tuple(messages), tuple(selections), self.policy_version,
            self.token_counter_version, required_tokens, optional_tokens,
            max(0, budget.context_window - budget.output_reserve - budget.framing_margin), "absent",
        )
        normalized_payload = {
            "request_id": request_id,
            "messages": list(messages),
            "tools": list(runtime.tool_schemas),
            "max_output_tokens": max_output_tokens,
            "metadata": dict(model_options or {}),
        }
        request_digest = digest_json(normalized_payload)
        manifest_without_digest = {
            "context_manifest_id": context_manifest_id,
            "request_id": request_id,
            "request_digest": request_digest,
            "policy_version": self.policy_version,
            "provider": self._provider,
            "model": self._model,
            "capability_source": self._capability_source,
            "capability_version": self._capability_version,
            "token_counter_version": self.token_counter_version,
            "context_window": budget.context_window,
            "output_reserve": budget.output_reserve,
            "framing_margin": budget.framing_margin,
            "tool_schema_tokens": tool_schema_tokens,
            "tool_accounting": tool_accounting,
            "counter_identity": counter_identity,
            "counter_classification": (
                "measured" if tool_accounting == "provider_request_counter" else "estimated"
            ),
            "total_request_tokens": total_request_tokens,
            "total_request_tokens_classification": (
                "measured" if tool_accounting == "provider_request_counter" else "estimated"
            ),
            "required_tokens": required_tokens,
            "optional_tokens": optional_tokens,
            "unallocated_optional_tokens": lendable,
            "class_ledger": {
                source_class: {
                    "minimum": CLASS_RESERVATIONS[source_class][0],
                    "maximum": CLASS_RESERVATIONS[source_class][1],
                    "reserved": reservations[source_class],
                    "lent": lending[source_class],
                    "released": released[source_class],
                    "allocated": allocation[source_class],
                    "spent": spent[source_class],
                    "unused": 0,
                }
                for source_class in OPTIONAL_CLASS_ORDER
            },
            "memory_status": "absent",
            "causal_frontier": runtime.causal_frontier,
            "selections": [selection.__dict__ for selection in selections],
        }
        manifest_digest = digest_json(manifest_without_digest)
        return ComposedContext(
            package, normalized_payload,
            {**manifest_without_digest, "manifest_digest": manifest_digest},
            request_digest, manifest_digest,
        )


def project_conversation_transcript(
    events: Iterable[Mapping[str, object]],
) -> tuple[ContextSource, ...]:
    """Project semantic events without copying Runtime-only journal records."""
    projected: list[ContextSource] = []
    for event in sorted(events, key=lambda value: required_int(value.get("sequence", 0), "sequence")):
        event_type = str(event.get("event_type", ""))
        if event_type not in {
            "initial_request_accepted", "steering_accepted", "reply_accepted",
            "assistant_response", "product_operation_outcome", "turn_finalized",
            "tool_observation_referenced",
        }:
            continue
        sequence = required_int(event.get("sequence", 0), "sequence")
        projected.append(ContextSource(
            "recent_transcript", f"conversation-event:{sequence}", canonical_json(event),
            authority=40, causal_relevance=sequence, recency=sequence,
            durable_sequence=sequence,
        ))
    return tuple(projected)


def capture_tool_result_artifact(
    *, session_id: str, tool_call_id: str, channel: str, content: bytes | None,
    mime_type: str = "text/plain", encoding: str = "utf-8",
    capture_limit: int = 256 * 1024, legacy_completeness: str | None = None,
) -> tuple[ToolResultArtifact, NormalizedObservation]:
    raw = content or b""
    captured = raw[:capture_limit]
    if legacy_completeness is not None:
        completeness = legacy_completeness
    else:
        completeness = "complete" if len(raw) <= capture_limit else "incomplete"
    digest = hashlib.sha256(captured).hexdigest()
    artifact_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"artifact:{session_id}:{tool_call_id}:{channel}:{digest}"))
    artifact = ToolResultArtifact(
        artifact_id, tool_call_id, channel, mime_type, encoding, digest,
        len(captured), 0, len(captured), capture_limit, completeness, captured,
    )
    excerpt_bytes = captured[:4096]
    excerpt = excerpt_bytes.decode(encoding, errors="replace") if mime_type.startswith("text/") else None
    observation = NormalizedObservation(
        str(uuid.uuid5(uuid.NAMESPACE_URL, f"observation:{artifact_id}")), artifact_id,
        "captured", {"channel": channel, "captured_size": len(captured)}, excerpt,
        len(captured) > len(excerpt_bytes), completeness,
    )
    return artifact, observation


def capture_file_context_item(
    *, workspace_binding_id: str, root: str | Path, path: str | Path,
    origin_kind: str, origin_id: str, byte_start: int = 0, byte_end: int | None = None,
) -> FileContextItem:
    max_file_bytes = 1024 * 1024
    max_range_bytes = 64 * 1024
    boundary = Path(root).resolve(strict=True)
    candidate = boundary / path
    resolved = candidate.resolve(strict=True)
    try:
        normalized = resolved.relative_to(boundary).as_posix()
    except ValueError as exc:
        raise ValueError("file context path escapes WorkspaceBinding") from exc
    if not resolved.is_file():
        raise ValueError("file context item must reference a regular file")
    if resolved.stat().st_size > max_file_bytes:
        raise ValueError("file context source exceeds the bounded capture limit")
    with resolved.open("rb") as stream:
        content = stream.read(max_file_bytes + 1)
    if len(content) > max_file_bytes:
        raise ValueError("file context source changed beyond the bounded capture limit")
    end = min(len(content), byte_start + max_range_bytes) if byte_end is None else byte_end
    if byte_start < 0 or end < byte_start or end > len(content):
        raise ValueError("file context range is invalid")
    if end - byte_start > max_range_bytes:
        raise ValueError("file context range exceeds the bounded capture limit")
    selected = content[byte_start:end]
    try:
        selected.decode("utf-8", errors="strict")
        encoding = "utf-8"
    except UnicodeDecodeError:
        encoding = "binary"
    revision = hashlib.sha256(content).hexdigest()
    range_digest = hashlib.sha256(selected).hexdigest()
    identity = str(uuid.uuid5(
        uuid.NAMESPACE_URL,
        f"file-context:{workspace_binding_id}:{normalized}:{revision}:{byte_start}:{end}",
    ))
    return FileContextItem(
        identity, workspace_binding_id, normalized, "regular", revision, revision,
        byte_start, end, range_digest, origin_kind, origin_id,
        content=selected, encoding=encoding,
        capture_completeness=("complete" if end == len(content) else "incomplete"),
    )


def build_summary_artifact(
    *, conversation_id: str, events: Sequence[Mapping[str, object]], content: str,
    claims: Sequence[SummaryClaim], policy_version: str = "m3-summary-v1",
    generator_version: str = "deterministic-test-v1",
) -> SummaryArtifact:
    if not events:
        raise ValueError("summary requires a non-empty contiguous prefix")
    ordered = sorted(events, key=lambda event: required_int(event["sequence"], "sequence"))
    sequences = [required_int(event["sequence"], "sequence") for event in ordered]
    if sequences != list(range(1, sequences[-1] + 1)):
        raise ValueError("summary source must be one contiguous transcript prefix")
    source_digest = digest_json(ordered)
    content_digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
    summary_id = str(uuid.uuid5(
        uuid.NAMESPACE_URL,
        f"summary:{conversation_id}:{source_digest}:{policy_version}",
    ))
    return SummaryArtifact(
        summary_id, conversation_id, sequences[0], sequences[-1], source_digest,
        content, content_digest, policy_version, generator_version, tuple(claims),
    )
