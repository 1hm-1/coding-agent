# ADR: Memory Product Positioning

Date: 2026-09-20
Status: **Accepted architecture decision; implementation inactive**

## Context

The product is a local, interactive Coding Agent. Its required product model and current target
architecture are:

```text
RepositoryIdentity
└── ProjectScope
    └── WorkspaceBinding

Conversation
└── Turn
    └── RuntimeExecution
        └── ModelRequest*
```

The core Coding Agent workflow already has, or is being designed around, dedicated authorities for
conversation history, current user intent, project instructions, workspace and Git state, model
context, tool results, code change evidence, and Runtime recovery. Long-term Memory must not copy
those records into a second source of truth merely because an earlier product direction emphasized
personal-Agent continuity.

The current implementation nevertheless contains useful Memory governance assets. SQLite is the
record authority; records have scope, revision, provenance, version, status, expiry, supersede, and
tombstone semantics; proposals are validated before activation; and retrieval can be audited in a
Context manifest. The default Application, headless path, and Runtime IPC do not depend on or
automatically inject Memory. Existing lexical/BM25 evaluation also produced no qualifying default
Dynamic Recall backend. Those are implementation and evidence facts, not reasons either to delete
Memory or to make it a required product dependency.

This decision builds on:

- [`project-repository-workspace-conversation.md`](./project-repository-workspace-conversation.md),
  which defines RepositoryIdentity, ProjectScope, WorkspaceBinding, worktree sharing, clone
  separation, and explicit Conversation rebind;
- [`conversation-turn-runtime-execution-lifecycle.md`](./conversation-turn-runtime-execution-lifecycle.md),
  which defines Conversation history, Turn inputs, one Turn to one RuntimeExecution, retry, and
  resume semantics;
- [`project-instructions-repository-rules.md`](./project-instructions-repository-rules.md), which
  makes explicit instructions the current scoped rule authority;
- [`context-composition-compaction.md`](./context-composition-compaction.md), which makes Context a
  per-ModelRequest projection and reserves the required envelope for current authoritative inputs;
  and
- [`diff-code-checkpoint-undo.md`](./diff-code-checkpoint-undo.md) and
  [`tool-capability-command-permission.md`](./tool-capability-command-permission.md), which keep code,
  tool, permission, and recovery authority outside Memory.

This ADR defines product positioning and authority boundaries only. It does not modify code,
database schema, Runtime IPC, the roadmap, or an implementation plan.

## Decision

### 1. Memory is an optional supporting subsystem

Memory is not a required path for Coding Agent V1 or the Phase 2 core workflow. All of the following
must remain complete and usable when the Memory service is disabled, unavailable, empty, corrupt,
or ineligible for the active scope:

- Conversation, Turn, and RuntimeExecution;
- WorkspaceBinding and repository/project identity;
- Project Instructions and InstructionManifest;
- transcript retention, Conversation resume, and history authority;
- Context composition and compaction;
- tool execution and permission handling;
- diff, code checkpoint, and CodeRewindOperation; and
- Runtime checkpoint, retry, reconciliation, and crash recovery.

Memory failure cannot prevent creation of a Conversation, acceptance of an ordinary Turn, creation
or recovery of a RuntimeExecution, composition of its required context envelope, or use of current
workspace evidence. The normal degraded mode is `memory-off`.

An explicit request to save a preference is different: if persistence fails, the product must say
that the preference was not saved. It must not acknowledge a durable save merely because the
request remains in the Conversation transcript. This failure still does not create a new product
entity or make later unrelated coding work depend on Memory recovery.

### 2. V1 adopts M2-lite

The architecture retains Optional Governed Memory, but the only independent Memory product
capability required in V1 is **Explicit UserPreference Memory**.

A UserPreference is eligible only when the user expresses durable save intent, for example:

- “remember ...”;
- “from now on ...”; or
- “default to ... in the future.”

The preference remains soft, scoped, revocable context. An ordinary statement or one-Turn request
does not become Memory merely because the model judges it likely to recur.

V1 does not require:

- automatic Memory extraction;
- generic proposal generation;
- automatic ProjectExperience extraction or injection;
- automatic DecisionMemory;
- end-of-Conversation Memory generation;
- Dynamic Recall;
- Core Snapshot; or
- dense retrieval, embeddings, or reranking.

These omissions do not mean that the governance subsystem is removed. They limit which behavior
the initial product promises.

### 3. “Remember this” and “obey this” are different contracts

The user-facing boundary is:

```text
Memory     = remember this
Instruction = obey this
```

Memory records a soft preference or historical evidence that may help a later ModelRequest.
Instruction expresses a current, explicitly scoped rule that should be applied whenever its scope
and authority make it effective.

If the user requires stable application on every request, the content belongs in one of:

- user-global instructions;
- Project or repository instructions;
- repository configuration or other explicit project authority; or
- an explicit ConversationInstruction when its lifetime is only the current Conversation.

It must not depend on Memory retrieval. The product must not silently downgrade a requested hard
rule into an optional Memory preference or promise that a saved Memory will be present in every
future prompt.

The Memory authority order is fixed:

```text
enforcement policy
    > current explicit user intent
    > explicit ConversationInstruction
    > active InstructionManifest
    > current validated workspace and tool evidence
    > optional active Memory
```

Rendering order, retrieval score, confidence, or recency cannot raise Memory above those sources.
Memory never grants a capability, permission, trust decision, sandbox exception, writer authority,
or ToolHarness precondition.

### 4. V1 Memory records have a narrow product taxonomy

#### UserPreference

`UserPreference` is the only independent Memory record class that V1 must expose as a product
capability. It requires explicit user intent to persist the preference and supports:

- user scope;
- RepositoryIdentity scope, optionally narrowed to ProjectScope;
- version and content identity;
- provenance;
- supersede;
- deletion and tombstone;
- expiry when appropriate; and
- active, stale, and unavailable selection semantics.

A repository/project preference does not cross into a different RepositoryIdentity. Two separate
clones therefore do not share it by remote URL or history similarity. A user-scoped preference may
cross `/new` and repository boundaries. A project-scoped preference may be shared by worktrees of
the same RepositoryIdentity, but it cannot carry current workspace facts with it.

#### ProjectExperience

`ProjectExperience` remains a future optional concept for non-authoritative historical lessons that
are expensive to rediscover and cannot be derived cheaply from current code. V1 does not
automatically extract, activate, or inject it. Historical problems are found through Conversation
history search by default.

#### DecisionMemory

`DecisionMemory` is only a temporary bridge for a decision that has not yet reached its durable
project authority. Important stable decisions must eventually be promoted to an ADR, Project
Instruction, repository documentation, or explicit configuration. Once that authoritative artifact
exists, the Memory is superseded or shadowed and may retain only historical provenance and a
reference to the authority. DecisionMemory never becomes the long-term decision authority.

#### EpisodicHistory

Episodic history is not copied into MemoryRecord. The Conversation and Turn transcript is the
episodic history authority. Conversation history search returns original history or source
references instead of manufacturing a second historical record.

This product taxonomy is semantic. It does not freeze a database enum or migration in this ADR.

### 5. Explicit preference writes reuse governed lifecycle semantics

The V1 product flow is:

```text
durably accepted explicit user save intent
    → determine user or repository/project scope
    → policy and provenance validation
    → create a governed active UserPreference
    → record version, source, actor, and lifecycle audit
```

The implementation may internally reuse the existing proposal followed by activation transition,
including an atomic or immediately governed activation path. V1 does not require the proposal to
become a separate user-facing inbox or approval workflow when the same user has explicitly asked
to save a valid soft preference.

The following do not create active Memory in V1:

| Input or evidence | V1 result |
|---|---|
| Ordinary preference statement without durable save intent | Remains in Conversation transcript |
| Agent inference | No automatic activation; no required proposal |
| Tool observation | Remains ToolResult/tool evidence and transcript projection |
| Conversation or compaction Summary | Remains a derived Conversation artifact |
| Current code or repository fact | Re-read from current workspace authority |

The existing proposal governance remains available as a future architecture asset. Its existence
does not require generic proposal generation or proposal-management UX in V1.

### 6. Provenance records evidence, not objective truth

Every persisted preference must be traceable to its source. When the accepted product identities
exist, Memory provenance must be able to reference:

- source Conversation;
- source Turn;
- source semantic event;
- optional originating RuntimeExecution;
- actor and actor type;
- evidence class;
- RepositoryIdentity;
- optional ProjectScope; and
- record content/version identity.

Runtime audit data is referenced rather than copied into Memory. A committed event proves only
that an actor expressed, observed, or inferred the recorded content at a particular point. It does
not prove the content is objectively true.

For an explicit UserPreference, the authoritative claim is narrowly phrased: the user explicitly
asked the product to remember a preference. It is not evidence that a repository fact is correct,
that a command is safe, or that a rule should override Instructions. A confidence score cannot
replace evidence class or authority.

### 7. Saved state and context use are separate

A Memory record's lifecycle answers whether the preference was successfully persisted and remains
active. A ContextManifest answers whether a particular ModelRequest actually used it.

```text
active UserPreference
    ≠ included in every ModelRequest
```

The default read policy is:

- generic automatic top-k retrieval is OFF;
- default Core Snapshot is OFF;
- Memory receives no required-envelope token reservation;
- an explicit active UserPreference may become a ContextComposer candidate only when scope and
  relevance match;
- stale, superseded, conflicting, unavailable, or incompatible records are excluded before budget
  allocation; and
- required context may consume the full input budget, in which case all Memory is omitted.

Memory cannot displace current user intent, applicable instructions, active Runtime protocol state,
current code evidence, or the output budget and provider margins required by the Context ADR.
Candidate selection in V1 remains deterministic for identical versioned inputs and policy.

When Memory enters a ModelRequest, it is an optional, low-authority context source. The exact
Frozen ModelRequest records what the model saw. Its ContextManifest records at least:

- Memory record ID and version;
- scope;
- evidence class;
- retrieval or eligibility reason;
- deterministic selection/relevance reason;
- injected token count; and
- injected, omitted, conflicting, stale, unavailable, or budget-excluded outcome and reason.

Memory selection does not mutate Conversation transcript, InstructionManifest, Workspace facts, or
Runtime state. A later Memory change cannot rewrite an already committed Frozen ModelRequest.

### 8. Conversation history search precedes automatic Memory extraction

When a user asks why something was done previously, the lookup order is:

1. authoritative ADR, Project Instruction, repository documentation, or explicit configuration;
2. original Conversation and Turn history; and
3. optional Memory hints that retain source references.

Conversation history search is therefore a higher-priority product capability than automatic
long-term extraction, automatic ProjectExperience recall, dense Memory retrieval, or reranking.
History search projects from the Conversation authority; it is not a MemoryRecord class and cannot
be replaced by a generated summary or Memory claim.

This ADR does not define a history-search implementation, index, CLI, or tool contract.

### 9. Invalidation and conflict rules are explicit

- A current user correction takes effect immediately for the current Turn and supersedes the
  corresponding stored preference when its intended scope is clear.
- Project Instructions and InstructionManifest win over conflicting Memory.
- Current validated workspace and tool evidence win over historical code-related Memory.
- Code-derived facts are not active long-term Memory by default.
- Repository/project Memory does not cross RepositoryIdentity.
- User-scoped preferences may cross `/new` and repository boundaries.
- Rebinding a Conversation does not convert Memory into workspace authority. Repository/project
  candidates are re-evaluated under the new binding's RepositoryIdentity and ProjectScope.
- `stale` means the record remains auditable but is no longer eligible as current context;
  `deleted` represents explicit removal with tombstone/retention semantics. They are not aliases.

A local project rule may shadow a user-scoped preference without globally staling or deleting that
preference. The ContextManifest records the scoped conflict. A new authoritative ADR or project
configuration supersedes a temporary DecisionMemory without erasing its historical provenance.

### 10. P2-R1 default serving assumptions are superseded

This ADR supersedes the following assumptions in the earlier P2-R1 product proposal and related
Phase 2 architecture text:

- `core_snapshot = ON` as the target default;
- a 1000–1500 token Core Snapshot injected into every Conversation or Turn;
- “safe automatic reads” as a default local-product requirement;
- generic automatic Memory proposals or extraction as a V1 requirement; and
- Core Snapshot as the next required vertical slice for the core Coding Agent workflow.

Those assumptions are **DEFERRED** and non-normative. Dynamic Recall, lexical/BM25 retrieval, dense
retrieval, embeddings, and reranking remain independent research or optional capabilities. They do
not enter the Coding Agent critical path and cannot block core implementation.

The parts of P2-R1 that preserve the Memory governance/control plane, SQLite authority,
provenance, scope isolation, lifecycle audit, and fail-closed handling remain valid.

### 11. Existing Memory implementation remains dormant and reusable

The existing implementation is not deleted. The following remain reusable architecture assets:

- SQLite authority;
- proposal, activation, rejection, stale, delete, and supersede lifecycle;
- provenance validation;
- scope isolation;
- version, expiry, and tombstone semantics;
- policy checks;
- retrieval audit and Context attribution seams; and
- negative evaluation and failed retrieval-candidate evidence.

Until separately activated, the subsystem remains dormant or explicitly opt-in. Default
Application, headless execution, and Runtime IPC do not acquire a Memory dependency or implicit
host Memory access merely because this ADR accepts M2-lite.

### 12. Memory does not add a top-level product entity

Memory records and retrieval selections are supporting objects. They do not change either accepted
main model:

```text
Conversation
└── Turn
    └── RuntimeExecution
        └── ModelRequest*

RepositoryIdentity
└── ProjectScope
    └── WorkspaceBinding
```

A Memory save does not create a Task, a second RuntimeExecution, a second transcript, or a second
workspace. Memory lifecycle state is not projected into RuntimeExecution FSM state. Retrieval and
omission are ModelRequest context decisions, not Conversation or Turn lifecycle transitions.

## Consistency review

### Context required envelope

M2-lite is consistent with the Context ADR. Memory is optional, has no minimum reservation, is
filtered before allocation, and may be omitted entirely. The required envelope remains
non-evictable. Historical reproduction remains exact because an injected record's version and
content are captured by the Frozen ModelRequest and explained by ContextManifest.

### Instructions

“Remember this” versus “obey this” forms a strict product boundary as long as mandatory behavior is
stored in an instruction authority and Memory remains optional. A saved preference cannot be used
as a hidden replacement for Project Instructions. The user may promote a preference into an
instruction through a separate explicit product action; that future UX is outside this ADR.

### Existing governance subsystem

Explicit UserPreference can reuse the current store, lifecycle, policy, provenance validator,
supersede, tombstone, and audit machinery. The current `SESSION` scope and episodic/semantic kind
names do not define the final product taxonomy, and existing Runtime-session provenance will need
future identity adaptation. The current requirement that repository records carry one exact
repository revision also does not by itself represent every revision-independent project
preference. Those are future persistence and domain-mapping concerns; this ADR does not authorize a
schema change, and neither issue conflicts with M2-lite or the main product model.

### Workspace and repository identity

Repository/project scope follows RepositoryIdentity and optional ProjectScope. Same-repository
worktrees may share eligible preferences; separate clones do not. Workspace drift cannot make a
Memory claim authoritative for current code, so rebind and revision validation rules remain
unchanged.

### Lifecycle and recovery

Memory unavailability does not introduce another Runtime wait state. A historical ModelRequest
that included Memory remains exactly recoverable from its frozen request. A future ModelRequest
re-evaluates current Memory eligibility without changing the execution's immutable
InstructionManifest or historical inputs.

No consistency issue requires redesign of Conversation, Turn, RuntimeExecution, ModelRequest,
RepositoryIdentity, ProjectScope, or WorkspaceBinding.

## V1 boundary

V1 requires only:

- explicit user intent to save a preference;
- governed, scoped active UserPreference lifecycle;
- correction, supersede, delete, and optional expiry;
- provenance and evidence classification;
- optional deterministic Context candidate selection;
- explicit distinction between saved and injected; and
- graceful memory-off behavior.

## Deferred

- automatic extraction or end-of-Conversation generation;
- generic proposal UX;
- automatic ProjectExperience and DecisionMemory;
- Core Snapshot;
- Dynamic Recall and automatic top-k retrieval;
- dense, embedding, hybrid, or reranking backends;
- automatic code-fact Memory;
- history-search implementation and indexing;
- physical persistence migration to Conversation/Turn/RepositoryIdentity/ProjectScope identities;
- Memory privacy export, encryption, retention duration, and garbage collection policy;
- user interface for promoting a Memory preference into an Instruction; and
- default Application, headless, or Runtime IPC wiring.

## Consequences

The core Coding Agent can progress without waiting for Memory product work or retrieval research.
Users still have a narrow, understandable cross-Conversation preference feature when they
explicitly request it. The architecture preserves prior governance investment without allowing it
to dictate product authority or consume prompt budget by default.

The trade-off is reduced automatic continuity. The Agent will not automatically remember every
project lesson, inferred preference, resolved failure, or Conversation summary. Users must rely on
Project Instructions for rules, repository documentation and ADRs for stable decisions, and
Conversation history search for historical work. This is intentional for V1: omission is safer
than silently elevating stale or inferred content into current Coding Agent behavior.
