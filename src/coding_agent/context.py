from __future__ import annotations

from dataclasses import dataclass
import json
import math
from typing import Callable, Mapping, Protocol, Sequence

from coding_agent.domain import (
    BuiltContext,
    ContextBuildInput,
    ContextBudgetError,
    ContextSection,
    Message,
    RepositorySnapshot,
)


class TokenCounter(Protocol):
    """Provider-aware token accounting seam.

    Implementations must make their accuracy explicit through ``name``. A
    conservative estimator is useful for unknown models, but it must never be
    reported as provider-exact accounting.
    """

    @property
    def name(self) -> str:
        ...

    def count_messages(
        self,
        provider: str,
        model: str,
        messages: Sequence[Message],
    ) -> int:
        ...


class ExactTokenCounter:
    """Adapter for a provider tokenizer supplied by the application or tests."""

    name = "provider_exact"

    def __init__(
        self,
        counter: Callable[[str, str, Sequence[Message]], int],
    ):
        self._counter = counter

    def count_messages(
        self,
        provider: str,
        model: str,
        messages: Sequence[Message],
    ) -> int:
        value = self._counter(provider, model, messages)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError("exact token counter must return a non-negative integer")
        return value


class ConservativeTokenCounter:
    """Named fallback estimator for models without a tokenizer integration."""

    name = "named_estimator"

    def count_messages(
        self,
        provider: str,
        model: str,
        messages: Sequence[Message],
    ) -> int:
        del provider, model
        total = 0
        for message in messages:
            encoded_length = len(message.content.encode("utf-8"))
            # Four bytes/token is intentionally conservative for mixed natural
            # language and code. The name is persisted so this is not confused
            # with a provider's exact usage accounting.
            total += max(1, math.ceil(encoded_length / 4)) + 4
            if message.tool_call_id:
                total += max(1, math.ceil(len(message.tool_call_id) / 8))
        return total


FallbackTokenCounter = ConservativeTokenCounter
NamedEstimator = ConservativeTokenCounter
ProviderTokenCounter = ExactTokenCounter


@dataclass(frozen=True)
class ModelCapability:
    provider: str
    model: str
    context_limit: int
    protocol_margin_tokens: int = 0
    token_counter: TokenCounter | None = None
    source: str = "registry"

    def __post_init__(self) -> None:
        if not self.provider or not self.model:
            raise ValueError("model capability provider and model are required")
        if self.context_limit <= 0:
            raise ValueError("model context limit must be positive")
        if self.protocol_margin_tokens < 0:
            raise ValueError("protocol margin cannot be negative")


class UnknownModelCapability(ContextBudgetError):
    def __init__(self, provider: str, model: str):
        super().__init__(
            "unknown model capability",
            kind="unknown_model_capability",
            details={"provider": provider, "model": model},
        )


class ModelCapabilityRegistry:
    """Explicit model limits and tokenizer choices with optional fallback."""

    def __init__(
        self,
        capabilities: Sequence[ModelCapability] = (),
        *,
        fallback: ModelCapability | None = None,
    ):
        self._capabilities: dict[tuple[str, str], ModelCapability] = {}
        self.fallback = fallback
        for capability in capabilities:
            self.register(capability)

    def register(
        self,
        capability: ModelCapability | str,
        model: str | None = None,
        *,
        context_limit: int | None = None,
        protocol_margin_tokens: int = 0,
        token_counter: TokenCounter | None = None,
    ) -> ModelCapability:
        if isinstance(capability, ModelCapability):
            value = capability
        else:
            if model is None or context_limit is None:
                raise ValueError("provider, model and context_limit are required")
            value = ModelCapability(
                provider=capability,
                model=model,
                context_limit=context_limit,
                protocol_margin_tokens=protocol_margin_tokens,
                token_counter=token_counter,
            )
        key = (value.provider, value.model)
        if key in self._capabilities:
            raise ValueError(f"duplicate model capability: {value.provider}/{value.model}")
        self._capabilities[key] = value
        return value

    def resolve(
        self,
        provider: str,
        model: str,
        *,
        allow_fallback: bool = True,
    ) -> ModelCapability:
        exact = self._capabilities.get((provider, model))
        if exact is not None:
            return exact
        wildcard = self._capabilities.get((provider, "*"))
        if wildcard is not None:
            return ModelCapability(
                provider=provider,
                model=model,
                context_limit=wildcard.context_limit,
                protocol_margin_tokens=wildcard.protocol_margin_tokens,
                token_counter=wildcard.token_counter,
                source=wildcard.source,
            )
        if allow_fallback and self.fallback is not None:
            return ModelCapability(
                provider=provider,
                model=model,
                context_limit=self.fallback.context_limit,
                protocol_margin_tokens=self.fallback.protocol_margin_tokens,
                token_counter=self.fallback.token_counter,
                source="fallback",
            )
        raise UnknownModelCapability(provider, model)

    capability = resolve


def default_model_capabilities() -> ModelCapabilityRegistry:
    """Return conservative, explicit defaults for the built-in adapters."""

    estimator = ConservativeTokenCounter()
    registry = ModelCapabilityRegistry(
        fallback=ModelCapability(
            provider="unknown",
            model="unknown",
            context_limit=16_384,
            protocol_margin_tokens=256,
            token_counter=estimator,
            source="fallback",
        )
    )
    for model, limit in (
        ("gpt-4o", 128_000),
        ("gpt-4o-mini", 128_000),
        ("gpt-4-turbo", 128_000),
        ("gpt-3.5-turbo", 16_385),
    ):
        registry.register(
            ModelCapability(
                provider="openai-compatible",
                model=model,
                context_limit=limit,
                protocol_margin_tokens=256,
                token_counter=estimator,
            )
        )
    for model in (
        "claude-3-5-sonnet-20240620",
        "claude-3-5-sonnet-20241022",
        "claude-3-opus-20240229",
        "claude-3-haiku-20240307",
    ):
        registry.register(
            ModelCapability(
                provider="anthropic",
                model=model,
                context_limit=200_000,
                protocol_margin_tokens=256,
                token_counter=estimator,
            )
        )
    registry.register(
        ModelCapability(
            provider="scripted",
            model="scripted",
            context_limit=16_384,
            protocol_margin_tokens=0,
            token_counter=estimator,
        )
    )
    return registry


@dataclass(frozen=True)
class SectionBudget:
    minimum_tokens: int = 0
    target_tokens: int | None = None
    maximum_tokens: int | None = None

    def __post_init__(self) -> None:
        if self.minimum_tokens < 0:
            raise ValueError("section minimum cannot be negative")
        if self.target_tokens is not None and self.target_tokens < self.minimum_tokens:
            raise ValueError("section target cannot be below its minimum")
        if self.maximum_tokens is not None:
            if self.maximum_tokens < self.minimum_tokens:
                raise ValueError("section maximum cannot be below its minimum")
            if self.target_tokens is not None and self.target_tokens > self.maximum_tokens:
                raise ValueError("section target cannot exceed its maximum")


@dataclass(frozen=True)
class ContextBudgetConfig:
    protocol_margin_tokens: int = 0
    high_watermark_ratio: float = 0.85
    target_after_compression_ratio: float = 0.65
    section_budgets: Mapping[str, SectionBudget] | None = None
    repository_file_limit: int = 200

    def __post_init__(self) -> None:
        if self.protocol_margin_tokens < 0:
            raise ValueError("protocol margin cannot be negative")
        if not 0 < self.high_watermark_ratio <= 1:
            raise ValueError("high watermark ratio must be in (0, 1]")
        if not 0 < self.target_after_compression_ratio < self.high_watermark_ratio:
            raise ValueError("compression target must be below the high watermark")
        if self.repository_file_limit <= 0:
            raise ValueError("repository file limit must be positive")

    def budget_for(self, name: str) -> SectionBudget:
        if self.section_budgets is None:
            return SectionBudget()
        return self.section_budgets.get(name, SectionBudget())


class ContextBuilder(Protocol):
    def build(self, request: ContextBuildInput) -> BuiltContext:
        ...


class PassthroughContextBuilder:
    """Complete-history baseline retained for M1 regression and M3 A/B."""

    SYSTEM_POLICY = (
        "You are a coding agent operating only in an isolated workspace. "
        "Use only the supplied tools. Read before editing, make the smallest exact edit, "
        "run the restricted test profile, and report the verified result."
    )

    def __init__(
        self,
        *,
        capability_registry: ModelCapabilityRegistry | None = None,
        token_counter: TokenCounter | None = None,
    ):
        self.capability_registry = capability_registry or default_model_capabilities()
        self.token_counter = token_counter

    def build(
        self,
        request: ContextBuildInput | str,
        messages: Sequence[Message] | None = None,
    ) -> BuiltContext | tuple[Message, ...]:
        # Preserve the pre-M3 two-argument helper contract for external M1 users.
        if isinstance(request, str):
            if messages is None:
                raise TypeError("legacy passthrough build requires messages")
            return (Message(role="system", content=self.SYSTEM_POLICY), *messages)

        capability = self.capability_registry.resolve(request.provider, request.model)
        counter = self.token_counter or capability.token_counter or ConservativeTokenCounter()
        system = Message(role="system", content=self.SYSTEM_POLICY)
        context_messages = (system, *tuple(request.messages))
        sections = (
            ContextSection(
                name="passthrough",
                messages=context_messages,
                estimated_tokens=counter.count_messages(
                    request.provider, request.model, context_messages
                ),
                source_refs=("session",),
            ),
        )
        margin = max(capability.protocol_margin_tokens, 0)
        budget = capability.context_limit - request.policy.max_output_tokens - margin
        return BuiltContext(
            messages=context_messages,
            sections=sections,
            total_input_tokens=sections[0].estimated_tokens,
            budget_tokens=max(0, budget),
            provider=request.provider,
            model=request.model,
            workspace_revision=request.repository_snapshot.workspace_revision,
            counter=counter.name,
            capability_source=capability.source,
            last_test=(
                request.repository_snapshot.last_test.to_dict()
                if request.repository_snapshot.last_test is not None
                else None
            ),
            high_watermark_tokens=max(0, int(max(0, budget) * 0.85)),
            pre_compression_input_tokens=sections[0].estimated_tokens,
            target_after_compression_tokens=max(0, int(max(0, budget) * 0.65)),
        )


class BudgetedContextBuilder:
    """Deterministic sectioned context builder with hard-retention guarantees."""

    SECTION_ORDER = ("system", "task_runtime", "repository", "summary", "recent")

    def __init__(
        self,
        *,
        capability_registry: ModelCapabilityRegistry | None = None,
        token_counter: TokenCounter | None = None,
        config: ContextBudgetConfig | None = None,
        system_policy: str = PassthroughContextBuilder.SYSTEM_POLICY,
        allow_model_fallback: bool = True,
    ):
        self.capability_registry = capability_registry or default_model_capabilities()
        self.token_counter = token_counter
        self.config = config or ContextBudgetConfig()
        self.system_policy = system_policy
        self.allow_model_fallback = allow_model_fallback

    def build(self, request: ContextBuildInput) -> BuiltContext:
        capability = self.capability_registry.resolve(
            request.provider,
            request.model,
            allow_fallback=self.allow_model_fallback,
        )
        counter = self.token_counter or capability.token_counter or ConservativeTokenCounter()
        budget = (
            capability.context_limit
            - request.policy.max_output_tokens
            - capability.protocol_margin_tokens
            - self.config.protocol_margin_tokens
        )
        if budget < 0:
            raise ContextBudgetError(
                "required context budget is negative",
                kind="context_required_content_exceeds_budget",
                details={"budget_tokens": budget},
            )

        sections = self._candidate_sections(request, counter)
        pre_total = sum(section.estimated_tokens for section in sections)
        high_watermark = int(budget * self.config.high_watermark_ratio)
        fitted = self._fit_sections(
            sections,
            request=request,
            counter=counter,
            budget=budget,
        )
        messages = tuple(message for section in fitted for message in section.messages)
        total = counter.count_messages(request.provider, request.model, messages)
        summary = request.latest_summary
        compressed = summary is not None and not summary.stale and summary.summary_id in {
            str(ref) for ref in self._summary_refs(fitted)
        }
        return BuiltContext(
            messages=messages,
            sections=tuple(fitted),
            total_input_tokens=total,
            budget_tokens=budget,
            provider=request.provider,
            model=request.model,
            workspace_revision=request.repository_snapshot.workspace_revision,
            counter=counter.name,
            capability_source=capability.source,
            last_test=(
                request.repository_snapshot.last_test.to_dict()
                if request.repository_snapshot.last_test is not None
                else None
            ),
            summary_id=summary.summary_id if compressed and summary is not None else None,
            compressed=compressed,
            pre_compression_input_tokens=pre_total,
            high_watermark_tokens=high_watermark,
            target_after_compression_tokens=max(
                0,
                int(max(0, budget) * self.config.target_after_compression_ratio),
            ),
        )

    def build_unbounded(self, request: ContextBuildInput) -> BuiltContext:
        """Build the full candidate used to decide whether compression is needed."""

        capability = self.capability_registry.resolve(
            request.provider,
            request.model,
            allow_fallback=self.allow_model_fallback,
        )
        counter = self.token_counter or capability.token_counter or ConservativeTokenCounter()
        budget = (
            capability.context_limit
            - request.policy.max_output_tokens
            - capability.protocol_margin_tokens
            - self.config.protocol_margin_tokens
        )
        sections = self._candidate_sections(request, counter)
        total = sum(section.estimated_tokens for section in sections)
        summary = request.latest_summary
        compressed = summary is not None and not summary.stale and bool(summary.summary_id)
        return BuiltContext(
            messages=tuple(message for section in sections for message in section.messages),
            sections=tuple(sections),
            total_input_tokens=total,
            budget_tokens=max(0, budget),
            provider=request.provider,
            model=request.model,
            workspace_revision=request.repository_snapshot.workspace_revision,
            counter=counter.name,
            capability_source=capability.source,
            summary_id=summary.summary_id if compressed and summary is not None else None,
            compressed=compressed,
            pre_compression_input_tokens=total,
            high_watermark_tokens=max(0, int(max(0, budget) * self.config.high_watermark_ratio)),
            target_after_compression_tokens=max(
                0,
                int(max(0, budget) * self.config.target_after_compression_ratio),
            ),
        )

    def _candidate_sections(
        self,
        request: ContextBuildInput,
        counter: TokenCounter,
    ) -> list[ContextSection]:
        messages = tuple(request.messages)
        initial = next(
            (message for message in messages if message.role == "user"),
            Message(role="user", content=request.task),
        )
        system = ContextSection(
            name="system",
            messages=(Message(role="system", content=self.system_policy),),
            estimated_tokens=0,
            source_refs=("system-policy", "tool-protocol"),
        )
        runtime_payload = {
            "task": request.task,
            "runtime_state": request.runtime_state.value,
            "remaining_budgets": {
                "steps": request.policy.max_steps,
                "model_calls": request.policy.max_model_calls,
                "tool_calls": request.policy.max_tool_calls,
                "output_tokens": request.policy.max_output_tokens,
            },
            "pending_tool_calls": [
                call.to_dict() for call in request.pending_tool_calls
            ],
            "active_call_id": request.active_call_id,
            "active_call_kind": request.active_call_kind,
        }
        task_runtime = ContextSection(
            name="task_runtime",
            messages=(
                initial,
                Message(
                    role="system",
                    content=json.dumps(runtime_payload, ensure_ascii=False, sort_keys=True),
                    metadata={"context_kind": "runtime_state"},
                ),
            ),
            estimated_tokens=0,
            source_refs=("task", "runtime-state", "remaining-budgets"),
        )
        repository_payload = request.repository_snapshot.to_dict()
        repository = ContextSection(
            name="repository",
            messages=(
                Message(
                    role="system",
                    content=json.dumps(
                        repository_payload,
                        ensure_ascii=False,
                        sort_keys=True,
                    ),
                    metadata={"context_kind": "repository_snapshot"},
                ),
            ),
            estimated_tokens=0,
            source_refs=(
                "workspace-revision",
                "file-list",
                "diff-summary",
                "last-test",
            ),
        )

        summary = request.latest_summary
        summary_section = ContextSection(
            name="summary",
            messages=(
                Message(
                    role="system",
                    content=json.dumps(summary.to_dict(), ensure_ascii=False, sort_keys=True),
                    metadata={
                        "context_kind": "validated_summary",
                        "summary_id": summary.summary_id,
                    },
                ),
            )
            if summary is not None and not summary.stale
            else (),
            estimated_tokens=0,
            source_refs=(f"summary:{summary.summary_id}",)
            if summary is not None and not summary.stale
            else (),
        )

        start_index = 1 if messages and messages[0] == initial else 0
        if summary is not None and summary.source_message_end is not None:
            start_index = max(start_index, summary.source_message_end + 1)
        recent_messages = messages[start_index:]
        recent = ContextSection(
            name="recent",
            messages=recent_messages,
            estimated_tokens=0,
            source_refs=self._recent_refs(recent_messages),
        )

        result: list[ContextSection] = []
        for section in (system, task_runtime, repository, summary_section, recent):
            result.append(
                ContextSection(
                    name=section.name,
                    messages=section.messages,
                    estimated_tokens=counter.count_messages(
                        request.provider, request.model, section.messages
                    ),
                    source_refs=section.source_refs,
                    truncated=section.truncated,
                )
            )
        return result

    def _fit_sections(
        self,
        sections: list[ContextSection],
        *,
        request: ContextBuildInput,
        counter: TokenCounter,
        budget: int,
    ) -> list[ContextSection]:
        by_name = {section.name: section for section in sections}
        hard_names = {"system", "task_runtime", "repository"}
        hard_tokens = sum(by_name[name].estimated_tokens for name in hard_names)
        recent = by_name["recent"]
        required_indices = self._required_recent_indices(recent.messages)
        required_messages = tuple(
            message
            for index, message in enumerate(recent.messages)
            if index in required_indices
        )
        required_recent_tokens = counter.count_messages(
            request.provider, request.model, required_messages
        )
        if hard_tokens + required_recent_tokens > budget:
            raise ContextBudgetError(
                "required context content exceeds the input budget",
                kind="context_required_content_exceeds_budget",
                details={
                    "required_tokens": hard_tokens + required_recent_tokens,
                    "budget_tokens": budget,
                },
            )

        summary = by_name["summary"]
        total = sum(section.estimated_tokens for section in sections)
        if total <= budget:
            return self._apply_section_limits(sections, request, counter, budget)

        # First remove only soft recent history, preserving the newest tool/test
        # facts and active tool request. This is the M3.1 truncation boundary.
        available_recent = budget - hard_tokens - summary.estimated_tokens
        if available_recent < required_recent_tokens:
            available_recent = required_recent_tokens
        fitted_recent = self._fit_recent(
            recent,
            required_indices,
            available_recent,
            request,
            counter,
        )
        sections = [
            fitted_recent if section.name == "recent" else section
            for section in sections
        ]
        total = sum(section.estimated_tokens for section in sections)

        if total > budget and summary.messages:
            sections = [
                replace_section(section, messages=(), estimated_tokens=0, truncated=True)
                if section.name == "summary"
                else section
                for section in sections
            ]
            total = sum(section.estimated_tokens for section in sections)

        if total > budget:
            # Fitting section maxima can further reduce optional recent history.
            available_recent = budget - hard_tokens
            sections = [
                self._fit_recent(
                    section,
                    required_indices,
                    max(required_recent_tokens, available_recent),
                    request,
                    counter,
                )
                if section.name == "recent"
                else section
                for section in sections
            ]
            total = sum(section.estimated_tokens for section in sections)
        if total > budget:
            raise ContextBudgetError(
                "context cannot fit without dropping required content",
                kind="context_required_content_exceeds_budget",
                details={"budget_tokens": budget, "selected_tokens": total},
            )
        return self._apply_section_limits(sections, request, counter, budget)

    def _apply_section_limits(
        self,
        sections: list[ContextSection],
        request: ContextBuildInput,
        counter: TokenCounter,
        budget: int,
    ) -> list[ContextSection]:
        result: list[ContextSection] = []
        total = 0
        for section in sections:
            limit = self.config.budget_for(section.name).maximum_tokens
            if limit is not None and section.estimated_tokens > limit:
                if section.name == "recent":
                    required = self._required_recent_indices(section.messages)
                    section = self._fit_recent(section, required, limit, request, counter)
                elif section.name == "summary":
                    section = replace_section(section, messages=(), estimated_tokens=0, truncated=True)
                else:
                    raise ContextBudgetError(
                        f"{section.name} section exceeds its required maximum",
                        kind="context_required_content_exceeds_budget",
                        details={"section": section.name, "maximum_tokens": limit},
                    )
            result.append(section)
            total += section.estimated_tokens
        if total > budget:
            raise ContextBudgetError(
                "section maximum configuration exceeds the input budget",
                kind="context_required_content_exceeds_budget",
                details={"budget_tokens": budget, "selected_tokens": total},
            )
        return result

    def _fit_recent(
        self,
        section: ContextSection,
        required_indices: set[int],
        available: int,
        request: ContextBuildInput,
        counter: TokenCounter,
    ) -> ContextSection:
        messages = section.messages
        selected_indices = set(required_indices)
        selected = tuple(messages[index] for index in sorted(selected_indices))
        required_tokens = counter.count_messages(request.provider, request.model, selected)
        if required_tokens > available:
            raise ContextBudgetError(
                "required observations exceed the recent section budget",
                kind="context_required_content_exceeds_budget",
                details={"required_tokens": required_tokens, "available_tokens": available},
            )
        remaining = available - required_tokens
        for index in range(len(messages) - 1, -1, -1):
            if index in selected_indices:
                continue
            candidate = tuple(messages[item] for item in sorted((*selected_indices, index)))
            candidate_tokens = counter.count_messages(
                request.provider, request.model, candidate
            )
            if candidate_tokens <= available:
                selected_indices.add(index)
                remaining = available - candidate_tokens
            if remaining <= 0:
                break
        selected_messages = tuple(messages[index] for index in sorted(selected_indices))
        selected_tokens = counter.count_messages(
            request.provider, request.model, selected_messages
        )
        return replace_section(
            section,
            messages=selected_messages,
            estimated_tokens=selected_tokens,
            truncated=len(selected_indices) != len(messages),
        )

    @staticmethod
    def _required_recent_indices(messages: Sequence[Message]) -> set[int]:
        required: set[int] = set()
        last_tool = next(
            (
                index
                for index in range(len(messages) - 1, -1, -1)
                if messages[index].role == "tool"
            ),
            None,
        )
        if last_tool is not None:
            required.add(last_tool)
        last_test = None
        for index in range(len(messages) - 1, -1, -1):
            if messages[index].role != "tool":
                continue
            try:
                payload = json.loads(messages[index].content)
            except (TypeError, ValueError):
                continue
            if isinstance(payload, Mapping) and payload.get("tool_name") == "restricted_test":
                last_test = index
                break
        if last_test is not None:
            required.add(last_test)
        for index in range(len(messages) - 1, -1, -1):
            if messages[index].role == "assistant" and messages[index].metadata.get("tool_calls"):
                required.add(index)
                break
        return required

    @staticmethod
    def _recent_refs(messages: Sequence[Message]) -> tuple[str, ...]:
        refs: list[str] = []
        for message in messages:
            if message.tool_call_id:
                refs.append(f"tool:{message.tool_call_id}")
            request_id = message.metadata.get("request_id")
            if request_id is not None:
                refs.append(f"model:{request_id}")
        return tuple(dict.fromkeys(refs))

    @staticmethod
    def _summary_refs(sections: Sequence[ContextSection]) -> tuple[str, ...]:
        summary = next((section for section in sections if section.name == "summary"), None)
        return summary.source_refs if summary is not None and summary.messages else ()


def replace_section(
    section: ContextSection,
    *,
    messages: tuple[Message, ...],
    estimated_tokens: int,
    truncated: bool | None = None,
) -> ContextSection:
    return ContextSection(
        name=section.name,
        messages=messages,
        estimated_tokens=estimated_tokens,
        source_refs=section.source_refs,
        truncated=section.truncated if truncated is None else truncated,
    )


def repository_snapshot_from_dict(raw: Mapping[str, object]) -> RepositorySnapshot:
    """Small compatibility helper for callers loading a manifest fixture."""

    return RepositorySnapshot.from_dict(raw)
