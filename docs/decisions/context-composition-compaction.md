# ADR: Context Composition and Compaction

Date: 2026-09-20
Status: **Accepted architecture decision; implementation inactive**

## Context

The product is a local, interactive Coding Agent with a long-lived Conversation, one user-facing
Turn per request, and one recoverable RuntimeExecution per Turn:

```text
Conversation
└── Turn
    └── RuntimeExecution
        └── ModelRequest*
```

The model has a finite context window, while the product retains durable conversation history,
Runtime recovery state, instruction snapshots, workspace observations, tool results, and code
change evidence. Those records have different owners, lifecycles, and validity rules. Treating the
assembled prompt as the owner of any of them would create a second authority and make drift,
recovery, audit, and compaction ambiguous.

The current implementation has useful foundations: a deterministic sectioned ContextBuilder,
token budgets, bounded repository metadata, persisted model requests, SQLite-authoritative
messages and events, SummaryRecord lineage, and non-destructive compression. It still builds
context inside the current one-shot Session/Runtime, treats one latest Session summary as its main
compression unit, rebuilds context in some resume paths, and may truncate tool output before the
persisted ToolResult. These are implementation facts, not the target authority model.

This decision builds on:

- [`project-repository-workspace-conversation.md`](./project-repository-workspace-conversation.md),
  which defines RepositoryIdentity, ProjectScope, WorkspaceBinding, explicit Conversation rebind,
  workspace drift, and writer coordination;
- [`conversation-turn-runtime-execution-lifecycle.md`](./conversation-turn-runtime-execution-lifecycle.md),
  which defines appendable Turn inputs, wait states, cancellation, retry, and the distinction
  between `resume_conversation` and `resume_execution`;
- [`tool-capability-command-permission.md`](./tool-capability-command-permission.md), which defines
  durable ToolInvocation, permission outcomes, ToolHarness final validation, and side-effect
  recovery boundaries;
- [`diff-code-checkpoint-undo.md`](./diff-code-checkpoint-undo.md), which defines workspace-diff
  views, controlled-change evidence, CodeCheckpoint, CodeRewindOperation, and revision-sensitive
  rollback; and
- [`project-instructions-repository-rules.md`](./project-instructions-repository-rules.md), which
  defines InstructionSource, InstructionScope, immutable InstructionManifest, path-aware
  activation, and instruction trust.

This ADR defines target architecture only. It does not activate implementation, change the
database or public Runtime IPC schema, define a migration, modify the roadmap, or redesign the
optional Memory subsystem.

## Decision

### 1. Context is a per-ModelRequest projection

Context is not product state. It is a finite projection of versioned authoritative inputs for one
specific ModelRequest.

The architecture distinguishes three questions:

```text
authoritative source records
    answer: what is the product or workspace state?

Frozen ModelRequest
    answers: what model-visible input did the model receive for this request?

ContextManifest
    answers: why were these sources selected, omitted, truncated, or rejected?
```

A Frozen ModelRequest is historical evidence of what the model saw. A repository or code fact
inside that request proves only that the fact was presented at that time. It does not make the
request the current authority for the filesystem, Git state, instructions, tool result, or user
intent.

ContextBuilder or ContextComposer is as stateless as practical. It may apply deterministic
selection, budgeting, rendering, and validation rules, but it does not own Conversation,
transcript, Workspace, instructions, RuntimeExecution, ToolResult, Summary, or future Memory
records. Rebuilding or discarding a projection cannot mutate those authorities.

### 2. Context-support objects remain subordinate to the accepted product model

The target uses the following supporting objects:

- `FrozenContextPackage`: an immutable composition result containing one exact normalized
  ModelRequest, its ContextManifest, their identities and integrity bindings;
- `ContextManifest`: audit metadata for source selection, validity, omissions, budget use, and the
  effective context digest;
- `SummaryArtifact`: a Conversation-scoped derived artifact over one identified source range;
- `SummaryClaim`: a typed, source-referenced claim inside a SummaryArtifact;
- `ToolResultArtifact`: captured-result evidence owned by one ToolInvocation;
- `NormalizedObservation`: a bounded, model-usable projection of a ToolResultArtifact or explicit
  incomplete capture;
- `ContextExcerpt`: an optional selected range or summary of a larger source; and
- `FileContextItem`: a revision-bound full-file or range projection used by one ModelRequest.

Their containment is:

```text
Conversation
├── Turn
│   └── RuntimeExecution
│       └── ModelRequest*
│           └── ContextManifest
└── SummaryArtifact*

ToolInvocation
└── ToolResultArtifact*
```

`FrozenContextPackage` is a commit unit and value object around ModelRequest and ContextManifest;
it is not another long-lived product entity. SummaryArtifact does not create a Task, Turn, or
RuntimeExecution. ToolResultArtifact does not own tool execution state.

### 3. Authority ownership is explicit

The following boundaries are fixed:

| Record or resource | Authority |
|---|---|
| Conversation durable log | ordered conversation semantics and user/Agent product history |
| Runtime and tool audit journal | FSM, recovery, model/tool call, permission, retry, and execution audit |
| Filesystem and Git | current code, working-tree, index, HEAD, branch, and repository state |
| InstructionManifest and referenced immutable snapshots | applicable instruction epoch for an execution/model request |
| ToolResultArtifact | exact captured tool-result bytes or structured data, subject to explicit capture-completeness metadata |
| SummaryArtifact | derived Conversation cache only |
| ContextManifest | explanation and audit of one context-selection decision |
| Frozen ModelRequest | historical evidence of exactly what the model-visible normalized request contained |

Derived workspace snapshots, snippets, diffs, test summaries, normalized observations, search
results, and future Memory retrieval results do not outrank their sources. Context rendering order
does not create authority.

The Conversation durable log is semantically append-only. User corrections append new entries;
FAILED and CANCELLED Turns remain; compaction does not delete transcript; regenerating a Summary
does not modify its source. Future privacy deletion or retention garbage collection must use
explicit tombstone, unavailable, or retention state rather than silently rewriting history.

### 4. Context ownership is hybrid

The Product layer supplies a versioned product snapshot containing or referencing:

- Conversation and transcript view;
- current Turn inputs and durable ordering;
- InstructionManifest and instruction snapshots;
- RepositoryIdentity, ProjectScope, and WorkspaceBinding;
- workspace and context policy;
- eligible Summary candidates; and
- source authority, trust, scope, and eligibility.

RuntimeExecution supplies a versioned execution snapshot containing or referencing:

- execution-local causal frontier;
- pending assistant/tool protocol groups;
- current model-actionable Runtime state;
- resolver outcomes required for the next decision; and
- durable sequence frontier.

A stateless ContextComposer combines those inputs for one ModelRequest. Product state is not copied
back into Runtime ownership. Runtime state is not copied into Conversation authority.

The composition boundary is per ModelRequest, not per RuntimeExecution. A newly accepted
SteeringInput, UserReply, tool observation, reconciliation outcome, or path-aware instruction
activation therefore produces a new projection for the next model decision without changing the
RuntimeExecution identity.

### 5. Context composition has a validated freeze boundary

The logical pipeline is:

```text
allocate ModelRequest and ContextManifest identities
    → read versioned Product inputs
    → read Runtime causal frontier
    → exclude stale, inapplicable, superseded, or unavailable candidates
    → establish the required envelope
    → deterministically allocate remaining budget
    → render provider-neutral normalized model input
    → validate source revisions and durable frontiers
    → freeze exact ModelRequest and ContextManifest
    → durable atomic commit
    → provider adapter call
```

The provider call cannot start unless the Frozen ContextPackage is durably committed. If a source
revision or durable frontier changes before commit, the candidate package is discarded and a new
one is composed. A change after commit does not rewrite the historical request; it becomes drift
that a later ModelRequest must validate.

Provider-specific wire formats remain inside model adapters. The exact normalized ModelRequest
contains every model-visible message, tool schema, model option, and other semantic input before
transport-only adaptation. The adapter identity/version is included in audit metadata so the
normalized request remains interpretable.

### 6. The required envelope is non-evictable

Every ModelRequest first reserves a required envelope containing at least:

- system and Runtime model instructions;
- current-Turn InitialRequest and active user intent;
- every still-effective SteeringInput and UserReply needed to interpret that intent;
- the active applicable InstructionManifest content;
- the active assistant/tool protocol atomic group;
- the semantic resolver outcome required for the next decision, including permission denial,
  clarification reply, or reconciliation outcome; and
- unresolved user constraints and explicit decisions that the next decision must preserve.

PermissionDecision and ControlRequest remain structured control records. The required envelope
contains their model-relevant semantic outcome, not a false ordinary user message. For example, a
denied invocation appears as a structured denial observation; raw approval UI interaction need not
be exposed to the model.

If the required envelope plus provider protocol margin and reserved output tokens exceeds the
provider context window, composition fails explicitly. The system must not silently truncate
current intent, remove active instructions, split an atomic tool protocol group, or replace
non-evictable current-Turn material with a Summary.

### 7. Context budget uses deterministic hybrid allocation

After the required envelope, the allocator applies:

1. source-class minimum reservations for eligible content;
2. dynamic lending of unused reservations;
3. deterministic ordering by authority, causal relevance, semantic relevance, and recency; and
4. provider-accurate accounting for messages, tool schemas, protocol framing, output reservation,
   and configured safety margin.

Stale, inactive, superseded, untrusted, incompatible, and unavailable candidates are excluded
before allocation. SummaryArtifact, older transcript, and future optional Memory can consume only
remaining budget. Recent transcript is bounded and cannot grow without limit merely because it is
recent.

V1 selection is deterministic. Identical versioned inputs, composition-policy version, provider
capability, and token-counter version must produce the same candidate ordering, omissions, and
effective normalized context. Stable tie-breaking uses durable sequence and stable source identity,
not filesystem enumeration or map iteration order.

V1 does not use an LLM relevance selector. A future model-based selector is a separate auditable
derived operation and must record selector version, query, candidate set, scores or reasons, and
selector request identity. Its result remains a candidate ranking, not new authority.

### 8. Conversation transcript and model-visible transcript are distinct

The Conversation durable log retains ordered semantic entries, including InitialRequest,
SteeringInput, UserReply, assistant responses, semantic references to tool interactions and bounded
observations, Turn outcomes, and product operations whose result affects later conversation
semantics. Exact ToolInvocation and ToolResultArtifact authority remains in the tool journal rather
than being duplicated into the transcript. Runtime-only events such as lease renewal, retry timers,
and checkpoint mechanics remain in the Runtime journal.

The model-visible transcript is a per-request projection. It may contain:

- selected raw Conversation entries;
- canonical assistant/tool protocol messages;
- normalized product-operation notices, such as a completed workspace rebind, instruction refresh,
  or CodeRewindOperation outcome; and
- references or excerpts for large tool results.

PermissionDecision and ControlRequest do not become ordinary transcript messages. Failed and
cancelled Turns remain selectable history. A CodeRewindOperation remains a separate durable
product operation; its relevant outcome may be projected without turning the rewind into a model-
driven Turn.

### 9. Compaction creates derived artifacts and never replaces authority

Compaction creates or regenerates a SummaryArtifact over a stable Conversation source range. It
does not delete, edit, reorder, or close source transcript entries. It is an internal derived-
artifact operation, not a Turn, RuntimeExecution, Agent Task, or code checkpoint.

Compaction uses a hybrid trigger:

- threshold-based eligibility as projected context approaches a configured high watermark;
- lazy compaction at a safe boundary before a ModelRequest when optional history cannot fit; and
- opportunistic generation at a stable Turn boundary.

Compaction cannot activate over a range containing:

- unconsumed SteeringInput;
- an unclosed assistant/tool protocol atomic group;
- pending side-effect reconciliation;
- an unresolved causal frontier whose source range is not stable; or
- any other execution state that cannot be represented as a durable, reproducible range.

An explicit future compact command requests the same derived operation at a safe boundary; it does
not authorize destructive transcript replacement.

Generation failure records the failed attempt and leaves the last valid Summary and source
transcript unchanged. A new Summary becomes eligible only after source lineage, generated content,
validation result, and artifact identity are durably committed. If optional history still cannot
fit, the composer may select raw excerpts or omit lower-priority optional sources. It must not enter
an unbounded compaction loop.

A model-backed Summary generation attempt uses the same durable-before-provider integrity rule as
an Agent ModelRequest: its exact normalized generation request and source-lineage manifest are
committed before the provider call. It is owned by the internal Summary derivation audit, does not
join the RuntimeExecution's Agent ModelRequest sequence, and cannot affect Turn or RuntimeExecution
outcome. The physical request-record reuse, if any, is deferred and cannot turn compaction into a
second RuntimeExecution.

### 10. SummaryArtifact is range-addressed and claim-provenanced

Every SummaryArtifact records at least:

- Summary ID and schema/version;
- Conversation identity;
- source Conversation sequence range and range digest;
- whether the range is contiguous and any explicitly excluded entry classes;
- creation durable sequence;
- generator kind and, when model-backed, provider/model and generation-request identity;
- summary policy or prompt version;
- relevant InstructionManifest and workspace/repository revision dependencies used during
  generation;
- content digest;
- validation state;
- parent or superseded Summary relationships; and
- compatibility, staleness, and retention state.

V1 uses a range-addressed rolling prefix Summary plus a raw recent transcript tail. The domain
allows future multiple range summaries and selective recall without requiring V1 to implement
multi-segment retrieval. If a rolling Summary is generated using a prior Summary for efficiency,
it retains transitive lineage to the original Conversation ranges and identifies the parent
artifact. Summary-of-summary does not promote a parent Summary to authority.

Each SummaryClaim has a type and source references. The minimum claim types are:

- conversational intent;
- explicit decision;
- completed work;
- unresolved decision;
- repository or code fact; and
- tool or test fact.

Conversational intent and explicit decisions remain subordinate to their original user entries
and later corrections. Completed-work claims reference Turn outcome and relevant change evidence;
they do not assert that current code still has that state. Unresolved-decision claims remain active
only until a referenced resolution supersedes them.

Repository/code and tool/test claims are revision-sensitive. They identify file, workspace,
ToolResultArtifact, execution, or other evidence revisions sufficient to detect staleness. A
workspace change invalidates affected claims, not unrelated conversational intent. A stale claim
may be included only when clearly represented as historical; it cannot be selected as a current
fact.

Summary does not copy project instructions as durable rules. Applicable instructions come from
InstructionManifest. Summary also does not convert an ordinary conversational preference into a
ConversationInstruction or cross-Conversation Memory.

### 11. Tool results use artifact, observation, and excerpt layers

Tool output has three distinct layers:

```text
ToolResultArtifact
    → NormalizedObservation
        → ContextExcerpt or SummaryClaim
```

`ToolResultArtifact` is the captured-result authority for one ToolInvocation. It records at least:

- artifact ID;
- content digest and size;
- MIME type and encoding;
- stdout, stderr, structured-result, or other channel;
- configured capture limit;
- whether capture is complete;
- captured byte range; and
- originating ToolInvocation.

Prompt truncation, durable artifact completeness, and process-output capture completeness are
independent facts:

- a complete durable artifact may be excerpted for a prompt;
- a normalized observation may be complete for its structured semantics while omitting raw text;
- a process may exceed its capture limit, leaving an explicitly incomplete artifact; and
- no preview or Summary may be represented as the missing complete result.

The default model input contains a bounded NormalizedObservation with status, exit information,
structured diagnostics, relevant excerpt, truncation/completeness markers, and artifact handle.
Later range or search reads use the artifact handle and produce new bounded observations. If the
capture itself is incomplete, the Agent must rerun an authorized operation or proceed with the
explicitly incomplete evidence.

Binary or non-text artifacts are represented by metadata and a handle unless a supported bounded
renderer produces a separate derived observation. Artifact access, retention, redaction,
encryption, and garbage collection must not change the originating ToolInvocation history.

### 12. File and snippet context is revision-bound

Every FileContextItem records at least:

- WorkspaceBinding;
- normalized workspace-relative path;
- file type where relevant;
- content revision or hash;
- included byte and/or line range;
- digest of the exact included content; and
- originating read, search, or observation reference.

Small relevant files may be included in full; larger files use selected ranges. Recently read
files, search matches, and dependency neighborhoods are selection signals, not persistent copies
of the filesystem.

Before a new ModelRequest is frozen, revision mismatch makes an old FileContextItem stale. It is
excluded, explicitly marked historical, or reread if still relevant. The Conversation cannot keep
file bodies as a second current-code authority.

Once a ModelRequest is durably frozen, its included bytes remain exact historical input even if an
IDE or external process immediately changes the file. The next ModelRequest observes the new
revision. ToolHarness write preconditions continue to protect actual file mutation; context
freshness does not replace final side-effect validation.

### 13. ModelRequest and ContextManifest are committed together

Before a provider call, one atomic durable boundary commits:

- exact normalized ModelRequest;
- ContextManifest;
- exact model-visible request-content digest;
- ContextManifest ID and digest;
- bidirectional identity bindings; and
- the model-call attempt state required by Runtime recovery.

Failure to commit means no provider call may start.

Mutual binding must not create a circular hash. The canonical request-content digest covers only
the exact model-visible normalized payload, excluding its audit envelope. The persisted request
audit envelope references ContextManifest ID and digest. ContextManifest references ModelRequest
ID and the request-content digest, while its own digest excludes its self-digest field. The atomic
binding record therefore proves the pair without requiring a mathematically self-referential
digest.

ContextManifest is explanation metadata, not a replacement for the exact request or source
snapshots. Losing a source later does not change what the historical request contained, but it may
make source-level reproduction, revalidation, or continued recovery unavailable.

### 14. ContextManifest records selection responsibility

ContextManifest records enough information to explain one ModelRequest, including at least:

- manifest ID/version and composition-policy version;
- ModelRequest, Conversation, Turn, and RuntimeExecution identity;
- provider, model, adapter, token-counter, and capability versions;
- system/model-policy revision;
- InstructionManifest ID and digest;
- included user-input durable sequences;
- selected transcript ranges or entry IDs;
- Summary IDs, source ranges, content digests, and claim selections;
- RepositoryIdentity, ProjectScope, WorkspaceBinding, workspace revision, and observed Git topology;
- file/snippet source refs, revisions, ranges, and digests;
- ToolResultArtifact and NormalizedObservation refs;
- selected diff, test, diagnostic, or controlled-change evidence refs;
- future optional Memory retrieval refs when present;
- omitted, truncated, stale, superseded, unavailable, duplicate, or inactive sources and reasons;
- provider window, output reservation, protocol/tool-schema cost, source reservations, lending,
  and actual token allocation;
- compression and selection reasons;
- durable source and execution frontiers; and
- effective normalized-context digest and exact request-content digest.

The Manifest need not copy complete source content already present in the exact ModelRequest or an
immutable source snapshot. It must record enough identity and revision information to explain why
that content was eligible and selected.

### 15. Wait states preserve their existing lifecycle semantics

Context composition does not introduce another wait-state machine.

- `WAITING_PERMISSION`: no new model decision is required while waiting. Approval revalidates and
  executes the same committed ToolInvocation; denial produces a structured observation for the
  next ModelRequest. Context rebuild cannot mutate the invocation or approval digest.
- `WAITING_USER_INPUT`: the Agent question and correlated UserReply are Conversation entries. The
  reply enters the next required envelope in durable order.
- `WAITING_RECONCILIATION`: compaction is blocked over the unstable frontier. After resolution, the
  reconciliation outcome is included in the next required envelope.
- `INTERRUPTED`: the stable checkpoint, original InstructionManifest, immutable WorkspaceBinding,
  and causal frontier remain recovery inputs. Resume validation may create a new future
  ModelRequest but cannot rewrite historical requests.

Permission UI remains outside ContextComposer. Context composition cannot grant capability,
permission, sandbox access, writer authority, or ToolHarness admission.

### 16. Resume preserves historical requests and creates new requests for new decisions

`resume_conversation` and a new Turn compose from current validated state:

- current WorkspaceBinding and workspace facts;
- the new Turn's InstructionManifest;
- append-only Conversation history;
- valid Summary candidates; and
- the new InitialRequest.

`resume_execution` follows three rules:

1. If a committed ModelRequest has a durably persisted provider response, recovery consumes that
   exact response.
2. If the request was committed but provider outcome is unknown, a retry of that same provider
   request reuses the exact Frozen ModelRequest and creates a new attempt record. Current workspace
   state cannot reconstruct or overwrite the request under the same identity.
3. If the execution needs a new model decision, it creates a new ModelRequest and ContextManifest.
   It keeps the immutable InstructionManifest epoch durably active at that execution frontier and
   the RuntimeExecution's immutable WorkspaceBinding, may read current validated workspace facts,
   and includes applicable drift or reconciliation outcomes. In the ordinary case this is the
   original Turn-start manifest; an explicitly committed safe-boundary instruction refresh, as
   defined by the Project Instructions ADR, creates a new immutable epoch for later requests
   without changing any prior request.

The resulting guarantee is:

> Historical inputs are exactly recoverable; future decisions can continue interpretably.

Exact recovery is conditional on the records required by the declared recovery window remaining
available and uncorrupted. A future privacy deletion or retention operation that tombstones an
exact request, instruction snapshot, required ToolResultArtifact, or other continuation evidence
must also mark the affected execution or request as no longer exactly recoverable. It cannot
silently substitute current state while retaining a recoverable label.

### 17. Future Memory is only an optional context source

This ADR defines only the boundary. A future Memory record may be an authoritative record of what
the Memory subsystem stored; its retrieval result is still an optional derived context candidate.
Memory cannot own the current Conversation transcript, Runtime state, workspace state, or project
instructions, and it cannot displace the required envelope. When Memory conflicts with current
instructions or explicit user intent, the higher-authority current sources govern.

## Architectural invariants

1. Context is frozen independently for every ModelRequest.
2. ContextComposer owns no product, workspace, instruction, Runtime, tool-result, Summary, or
   Memory authority.
3. Conversation semantic history is append-only; compaction never deletes it.
4. One Turn still has one RuntimeExecution; ordinary interaction does not gain a persistent Task.
5. ContextManifest is subordinate audit evidence for one ModelRequest.
6. SummaryArtifact is a Conversation-derived cache and never instruction or fact authority.
7. ToolResultArtifact remains subordinate to its originating ToolInvocation.
8. File and repository context is revision-bound and never becomes a second filesystem.
9. The required envelope is non-evictable; overflow is an explicit failure.
10. V1 selection is deterministic for identical versioned inputs and policy versions.
11. Assistant/tool protocol groups are retained or omitted atomically.
12. Stale sources are excluded before budget allocation unless explicitly represented as
    historical evidence.
13. Exact ModelRequest and ContextManifest are durably committed before provider execution.
14. A historical request is immutable after commit.
15. Retrying an unknown-outcome provider call reuses the exact request; a changed context creates a
    new ModelRequest.
16. A RuntimeExecution never changes WorkspaceBinding or InstructionManifest through context
    rebuilding.
17. Context composition cannot grant permission, capability, trust, writer authority, or sandbox
    admission.
18. Prompt truncation and artifact capture completeness are separately auditable.

## Consistency review with prior ADRs

### RepositoryIdentity, ProjectScope, and WorkspaceBinding

There is no identity conflict. Context sources reference the accepted hierarchy rather than owning
it. FileContextItem and workspace observations bind to one WorkspaceBinding. Repository-shared
descriptors remain hints or observations, not a cross-clone authority merger.

A Conversation rebind occurs only between Turns. A new Turn composes from the newly confirmed
WorkspaceBinding. Summary conversational claims may remain useful across a rebind, but code,
repository, diff, file, and tool claims retain their original WorkspaceBinding/revision and require
compatibility validation. They do not silently migrate.

### RuntimeExecution workspace and instruction immutability

There is no conflict. A RuntimeExecution supplies its immutable WorkspaceBinding and the immutable
InstructionManifest epoch active at its durable causal frontier to every future ModelRequest.
Current file facts may be reread within that binding, while historical requests remain frozen.
Context rebuild cannot itself be used as execution rebind or instruction refresh.

The Project Instructions ADR permits an explicit safe-boundary instruction refresh that creates a
new immutable manifest revision inside the same execution only when the user explicitly requests
it. If that future product operation is used, it creates a new audited instruction epoch and
therefore a new ModelRequest; it never changes an already committed request. Absent that explicit
operation, `resume_execution` uses the original immutable manifest as already decided. Recovery
after such a refresh restores the durably active epoch from the checkpoint rather than rereading
current instruction files.

### Working-tree drift and code evidence

There is no conflict. Turn-boundary drift is normal. Active execution topology changes and unknown
side effects retain the stricter lifecycle/reconciliation rules. Per-file revision validation
prevents stale snippets from being presented as current and continues to support ToolHarness
optimistic write preconditions.

CodeRewindOperation and external edits invalidate affected file, diff, test, and code-summary
claims. They do not rewrite the Conversation transcript or old ModelRequests.

### ToolInvocation, permission, and ToolHarness

There is no conflict. ToolInvocation is committed before execution. PermissionRequest and
PermissionDecision retain their exact invocation binding. A context rebuild cannot alter a
committed invocation, and approval still requires final ToolHarness revalidation.

ToolResultArtifact records captured outcome evidence after or during the ToolHarness-supervised
operation; it does not authorize the operation or bypass recovery classification. A partial
artifact after host loss remains partial evidence and cannot resolve an unknown side effect by
itself unless the reconciliation policy says the recorded evidence is sufficient.

### Runtime recovery and retry

There is no conflict. The lifecycle ADR keeps provider retry, tool retry, and crash recovery inside
one RuntimeExecution. This ADR adds the request-level rule that the same provider request is replayed
from frozen input, while a newly reasoned decision receives a new request identity. This preserves
one Turn to one RuntimeExecution without conflating model attempts with executions.

### ModelRequest and ContextManifest digest binding

The accepted mutual-reference requirement is consistent once model-visible content integrity is
separated from the audit envelope. Stable IDs and a non-cyclic canonical digest definition avoid a
self-referential hash while still binding the exact request and Manifest atomically.

### Retention and exact recoverability

Append-only semantic history and future tombstone/retention semantics are compatible only if
recoverability status follows evidence availability. Deleting required payload is allowed by a
future policy, but the system must then expose the affected historical input or execution as
unavailable for exact recovery. This is a retention-state consequence, not a new lifecycle entity.

No accepted decision requires changing Conversation, Turn, RuntimeExecution, ModelRequest,
RepositoryIdentity, ProjectScope, or WorkspaceBinding relationships.

## Failure modes

- **Required envelope overflow:** reject composition with an explicit context-budget failure; do
  not truncate current intent, active instructions, or protocol state.
- **Source changes before freeze:** discard the candidate package and rebuild from a new durable
  frontier; do not call the provider.
- **Source changes after commit:** keep the historical request immutable and mark affected sources
  stale for the next request.
- **Atomic commit failure:** do not start the provider call.
- **Request or Manifest digest mismatch:** treat the package as corrupt and stop recovery rather
  than reconstructing it from mutable state.
- **Unsafe compaction frontier:** defer compaction and keep the existing transcript projection.
- **Summary generation or validation failure:** retain source transcript and the last valid Summary;
  record the failed attempt.
- **Summary source unavailable:** mark regeneration or verification unavailable; do not promote the
  Summary to authority.
- **Tool capture limit reached:** persist explicit incomplete-capture metadata; do not describe the
  preview as complete output.
- **Required artifact removed by retention:** mark dependent exact recovery unavailable.
- **File/snippet revision mismatch:** exclude, mark historical, or reread; never silently reuse it
  as current content.
- **Unknown provider outcome:** retry only from the exact frozen request or follow the Runtime's
  provider-reconciliation rule.
- **Workspace or Git topology incompatibility:** follow the existing drift and reconciliation
  lifecycle; ContextComposer cannot approve continuation.
- **Missing original InstructionManifest or snapshot:** fail exact execution recovery rather than
  loading current repository instructions under the old request identity.

## V1 boundary

V1 includes the following architectural behavior:

- one ContextManifest per Agent ModelRequest;
- exact normalized ModelRequest persistence before provider execution;
- hybrid deterministic budget allocation;
- non-evictable required envelope;
- append-only Conversation semantic history;
- range-addressed rolling prefix Summary plus raw recent tail;
- safe-boundary and threshold-aware compaction semantics;
- source-referenced typed Summary claims;
- ToolResultArtifact, NormalizedObservation, and ContextExcerpt separation;
- revision-bound file and snippet context; and
- exact-request retry versus new-request continuation semantics.

V1 does not require:

- multiple-range Summary retrieval;
- an LLM relevance selector;
- semantic or vector context search;
- a background compaction service;
- a new daemon or execution supervisor;
- a complete interactive context-inspection UI;
- future Memory integration; or
- a specific physical persistence layout.

## Deferred decisions

The following remain for later architecture or persistence work:

- database tables, migrations, serialized events, and public IPC representation;
- physical storage for exact ModelRequests, ContextManifests, summaries, and tool artifacts;
- retention duration, garbage collection, encryption, redaction, export, and privacy deletion UX;
- artifact range/search API and UI;
- exact capture limits by tool, channel, MIME type, and platform;
- binary renderers and structured diagnostic extractors;
- tokenizer/provider accounting calibration and configuration UX;
- source-class reservation values, high-watermark values, and model-specific tuning;
- Summary generator choice, prompt, validation depth, retry/cost accounting, persistence-primitive
  reuse, and regeneration scheduling;
- multiple range summaries and selective older-transcript recall;
- model-based relevance selection and its evaluation gate;
- cross-rebind Summary compatibility heuristics for code claims;
- manual compaction and context-inspection command UX; and
- the future optional Memory candidate interface.

Every deferred decision must preserve the authority, immutability, deterministic-V1, recovery,
staleness, and pre-provider durable-commit invariants in this ADR.

## Consequences

### Positive

- Long Conversations can compact without sacrificing transcript authority or audit.
- Runtime recovery can distinguish replay of an exact provider request from a new model decision.
- Workspace drift invalidates only affected code facts instead of erasing unrelated user intent.
- Large tool results remain inspectable without occupying every prompt.
- Context selection is explainable and reproducible without making ContextBuilder a state owner.
- Existing Runtime journal, checkpoint, instruction, permission, ToolHarness, and code-change
  reliability remain available beneath a Claude Code-like conversational product.

### Negative and risks

- Provenance, source revision, artifact, and omission metadata can be large.
- Context freeze spans multiple authorities and cannot create an atomic snapshot of an externally
  mutable filesystem; per-source revisions expose rather than eliminate that limitation.
- Deterministic selection is testable but may still make a poor relevance choice.
- Exact raw-result retention has storage and sensitive-data costs.
- Typed Summary claims reduce accidental authority but do not guarantee semantic completeness.
- Small-context models may fail more often because the architecture refuses to hide required
  envelope overflow.

## Reference product behavior

The product direction is informed by publicly documented Claude Code behavior:

- [How Claude Code works](https://code.claude.com/docs/en/how-claude-code-works) documents local
  persistence of messages, tool uses, and results, conversation resume, and automatic context
  management that removes older tool outputs before summarizing conversation history.
- [Explore the context window](https://code.claude.com/docs/en/context-window) documents context
  categories, automatic/manual compaction, reinjection of persistent project instructions, and
  selective rereading of files after compaction.
- [Commands](https://code.claude.com/docs/en/commands) documents `/context`, `/compact`, `/clear`,
  and `/resume` as distinct product operations.

Those sources establish observable product behavior only. They do not establish Claude Code's
internal transcript authority, Summary schema, ContextManifest, tool-artifact retention, atomic
request commit, or recovery representation. This ADR defines those boundaries for this project.
