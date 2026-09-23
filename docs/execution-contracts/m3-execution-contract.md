# Product-Layer M3 Execution Contract — Instructions, Context, and Frozen ModelRequest

Date issued: 2026-09-23
Status: **ACCEPTED / COMPLETE** (owner acceptance: 2026-09-23)
Milestone owner: Sol High planning/review owner
Executor: Terra High; if Terra High reports an execution barrier or cannot safely satisfy this
contract, the owner may explicitly hand the unchanged remaining scope to Sol Medium.
Baseline: clean accepted M2 commit `4cb1b88bdcb308008c0e4d99fb0fd9d5e69d2069`

This document authorizes **Product-Layer M3 only**. M0, M1, and M2 remain Accepted/complete. M4,
M5, optional M6, and M7 remain inactive.

**Publication-turn stop boundary:** this issuance turn publishes the contract and synchronizes
status/navigation documentation only. It authorizes no M3 production code, schema, migration,
fixture, test, metric implementation, or behavior change in this turn. M3 implementation starts
only in a later, separately started executor turn against this contract.

## 1. Authority, objective, and closed delivery boundary

Authority, in precedence order:

1. the eight Accepted product ADRs indexed by
   [`../architecture-consistency-audit.md`](../architecture-consistency-audit.md), especially
   Project Instructions, Context/Compaction, Conversation lifecycle, Workspace, Tool/Permission,
   Diff/Undo, and Memory positioning;
2. [`../target-architecture-snapshot.md`](../target-architecture-snapshot.md);
3. the M3 scope and exit criteria in
   [`../coding-agent-v1-implementation-roadmap.md`](../coding-agent-v1-implementation-roadmap.md);
4. the measurement contract in that roadmap and
   [`../evidence/v1-m0/metric-dictionary-v1.json`](../evidence/v1-m0/metric-dictionary-v1.json), plus
   the exact uncertain-retry characterization in
   [`../coding-agent-v1-m0-characterization.md`](../coding-agent-v1-m0-characterization.md) and
   [`../evidence/v1-m0/uncertain-retry-v1.json`](../evidence/v1-m0/uncertain-retry-v1.json);
5. the accepted [`m2-execution-contract.md`](./m2-execution-contract.md),
   [`../m2-product-lifecycle-design.md`](../m2-product-lifecycle-design.md), and
   [`../evidence/m2-implementation-verification-2026-09-23.md`](../evidence/m2-implementation-verification-2026-09-23.md);
6. the accepted [`m1-execution-contract.md`](./m1-execution-contract.md) and M1 persistence design;
   and
7. the unchanged public Runtime IPC producer authority in
   [`../protocol/runtime-ipc-v1.md`](../protocol/runtime-ipc-v1.md),
   [`../protocol/compatibility.md`](../protocol/compatibility.md), and `protocol/v1/*.schema.json`.

M3's sole objective is to make every new model decision reproducible and explainable: resolve and
freeze applicable soft instructions, compose deterministic bounded context from Product and
Runtime authorities, atomically persist the exact normalized ModelRequest and ContextManifest
before any provider attempt, retain append-only attempt evidence, compact without replacing the
Conversation transcript, bound tool/code observations with truthful provenance, correct uncertain
provider retry, and implement M0-classified model/context/compaction instrumentation.

M3 does not implement permission policy, code mutation protection, interactive CLI, Memory
serving, public IPC changes, or legacy removal. Direct Product paths targeting real user trees
remain strictly read-only. M3 acceptance satisfies only the instruction/context half of the
M3+M4 mutation rollout gate and cannot open that gate by itself.

## 2. Authorized M3 behavior

M3 may implement only the following behavior.

1. `InstructionSource`, `InstructionScope`, trust records, immutable content snapshots, immutable
   versioned `InstructionManifest`, manifest entries, conflicts/overrides, base discovery,
   path-aware safe-boundary activation, staleness, and explicit safe-boundary refresh.
2. A stateless `ContextComposer` port that consumes a versioned Product snapshot and a versioned
   Runtime causal snapshot and returns one `FrozenContextPackage`. Existing
   `BudgetedContextBuilder` and `PassthroughContextBuilder` remain adapters/baselines rather than
   authorities.
3. A non-evictable required envelope and versioned deterministic hybrid budget allocation with
   explicit source inclusion, omission, staleness, truncation, budget, and digest evidence.
4. One atomic pre-provider publication of the exact normalized `ModelRequest`, its
   `ContextManifest`, mutual non-cyclic digest bindings, the Runtime checkpoint/event frontier, and
   the first provider-attempt intent. A failed publication starts no provider call.
5. Immutable ModelRequest identity and payload, separate provider-attempt identities, and terminal
   attempt outcomes. A provider fallback is another attempt against the same exact request unless
   the Runtime requires a new model decision, in which case it receives a new request and manifest.
6. A Conversation transcript projection over `conversation_semantic_events`; assistant semantic
   responses, bounded tool-observation references, product-operation outcomes, and Turn outcomes
   may be appended. Exact tool calls/results remain Runtime/tool journal authority and are not
   copied into transcript as a second journal.
7. Conversation-scoped rolling-prefix `SummaryArtifact` and typed, source-referenced
   `SummaryClaim`; legacy Session `SummaryRecord` remains readable through a truthful compatibility
   adapter. Compaction never deletes, edits, reorders, or closes source transcript entries.
8. `ToolResultArtifact`, `NormalizedObservation`, `ContextExcerpt`, and `FileContextItem`
   boundaries. M3 may capture and project results of already-authorized existing tools; it may not
   add a tool, expand a permission, or make an existing capture more complete than the bytes the
   Harness actually retained.
9. Exact-request retry for an unknown provider outcome, committed-response reuse, and a new
   request for every genuinely new model decision.
10. Versioned instrumentation for every model attempt and Context/selection/compaction operation,
    including failures, retries, cancellations, unknown outcomes, fallbacks, and model-backed
    Summary auxiliary requests.

## 3. Instruction contract

### 3.1 Source providers, discovery bounds, and trust

The initial V1 file provider recognizes the exact UTF-8 filename `AGENTS.md`. It searches only:

- the accepted RepositoryIdentity root, when present;
- the ProjectScope root and its ancestor chain inside that repository;
- the concrete working directory chain inside the ProjectScope; and
- later target-directory chains reached by a read or proposed controlled write.

For non-Git workspaces the explicit ProjectScope/workspace root is the hard upper boundary.
Discovery never crosses into an unrelated nested repository. Directory traversal, normalized
relative paths, and manifest entry ordering are deterministic. The implementation limits one
source to 256 KiB, one discovery to 64 sources and 1 MiB of loaded instruction text. A bound,
decode, lstat, containment, or read failure produces an `unavailable` entry with a stable reason;
it never silently truncates or partially activates a source.

The domain/provider port remains filename-independent. Explicit user-global, workspace-local, and
future ConversationInstruction providers may be represented and tested through that port without
adding a CLI. No import/include syntax is introduced in M3. After canonical resolution, a symlink
whose target is fully contained in the accepted boundary may be loaded under its normalized source
identity; an escaping, broken, cyclic, or otherwise unverifiable target is audit-visible but
inactive/unavailable, and its target content is not read into model context.

Repository and path-source trust is a durable Product operation scoped exactly to
`RepositoryIdentity + ProjectScope`. Trust activation/revocation is stored only in agent-owned
SQLite and grants soft model-context eligibility only. Workspace-local activation additionally
binds to one WorkspaceBinding. User-global explicit configuration is separately scoped. Trust does
not grant capability, permission, filesystem access, writer authority, sandbox access, or a
ToolHarness exception. An inactive/untrusted source may contribute locator/scope/revision/digest
audit metadata, never model-visible content.

First-open trust should be resolved before accepting an ordinary request when the caller exposes
that use case. Because M5 UI is inactive, M3 exposes typed application operations and deterministic
outcomes only; it must not invent an interactive trust prompt.

### 3.2 Snapshot, precedence, conflict, activation, and refresh

Every source revision that becomes loaded/effective retains both a SHA-256 digest over canonical
raw UTF-8 bytes and an immutable content-addressed snapshot. Digests alone are insufficient.
Discovered inactive sources need only locator/scope/revision/digest metadata until activated.

Instruction authority is:

```text
current Turn durable intent
  > explicit ConversationInstruction
  > applicable path-specific Project/Workspace rule
  > repository/root Project rule
  > user-global default
  > historical or derived context
```

Higher authority or greater path specificity overrides only a direct semantic conflict; unrelated
rules remain additive. Stable filename or load order is never authority. Deterministically
resolvable structured conflicts retain both entries and an override edge. Material unresolved
natural-language conflicts retain every source, block only affected decisions, and use existing
`WAITING_USER_INPUT` plus a correlated `UserReply`; they do not create another FSM.

Turn Admission publishes a real base InstructionManifest, replacing the M2 placeholder for newly
admitted M3 executions. Discovery and immutable snapshots are prepared outside the SQLite
transaction, revalidated, then published inside the existing admission transaction with the Turn
bundle. No instruction snapshot means no falsely accepted M3 Turn. Existing M2 Turns retain their
truthful placeholder unless a separately tested compatibility activation creates their first real
manifest at a safe boundary; history is never rewritten.

A path-aware activation creates a new immutable manifest revision before the next model request.
A future M4 controlled write cannot execute unless the model request that proposed it references a
manifest covering the full target-path chain. During M3, every real-tree write remains unavailable.
M3 nevertheless implements the pure classification seam: recognized instruction-source mutation
is `exact_review_required`, never auto-allowed; M4 later maps it to exact invocation-bound `ASK`.
M3 must not create a PermissionRequest or execute the edit.

Loaded content never changes silently in one RuntimeExecution. Drift marks the source/manifest
stale but keeps the frozen snapshot effective. Explicit refresh is a typed, idempotent,
safe-boundary Product operation that creates a new manifest revision and checkpoint frontier; it is
not ordinary text or a PermissionDecision. Resume restores the checkpoint's exact manifest and
verifies every required snapshot digest. Missing/corrupt evidence fails exact automatic resume;
current files are never substituted under an old epoch.

Each manifest records the ADR-required identities, source type/locator/revision/scope, authority,
specificity, trust, disposition/reason, conflict/override edges, durable load sequence, parent and
refresh reason, effective digest, and immutable snapshot references.

## 4. Context, transcript, compaction, and artifact contract

### 4.1 Hybrid composition and required envelope

Product supplies Conversation/Turn entries, ordered current inputs, InstructionManifest,
RepositoryIdentity/ProjectScope/WorkspaceBinding, eligible Summary artifacts, and source
eligibility. Runtime supplies the exact causal frontier, active assistant/tool protocol group,
pending resolver outcomes, actionable FSM state, and durable sequence. `ContextComposer` owns none
of these authorities.

The required envelope contains, at minimum:

- system and Runtime model instructions and their revision;
- current InitialRequest plus every still-effective SteeringInput/UserReply needed to interpret it;
- all effective content from the active applicable InstructionManifest;
- the active assistant/tool protocol group as one indivisible unit;
- the model-relevant structured denial, clarification, reconciliation, or other resolver outcome;
  and
- unresolved current constraints and explicit decisions the next decision must preserve.

Tool schemas, provider framing/safety margin, and output reservation are budgeted before optional
sources. If the required envelope cannot fit, composition fails with a structured
`context_required_content_exceeds_budget`; it never truncates current intent, active instructions,
or an atomic protocol group and never calls the provider.

### 4.2 Deterministic hybrid allocation

After the required envelope, eligible classes are considered in this fixed class order: bounded
recent raw transcript, current bounded Runtime/tool observations, revision-bound file/snippet and
diagnostic evidence, valid rolling-prefix Summary, then optional Memory. M3 leaves Memory disabled,
so the final class is empty on default and Product paths.

The versioned policy assigns explicit minimum/maximum reservations per class, lends unused tokens
in the same fixed order, and orders candidates by authority, causal relevance, deterministic
semantic signals, recency, durable sequence, then stable source ID. Filesystem enumeration and map
iteration order never break ties. Identical versioned inputs, policy, provider capability,
token-counter version, and tool schemas produce identical selection, omission reasons, normalized
payload, and digests. V1 adds no LLM relevance selector.

Provider capability, context window, output reserve, framing margin, tool-schema cost, each source
reservation/allocation, included and omitted source, token count/classification, and policy/counter
version are persisted. Provider-reported usage is measured; versioned local token counts are
estimated unless an exact provider tokenizer is proven; unsupported/missing usage is unknown.

### 4.3 Transcript and SummaryArtifact

`conversation_semantic_events` remains the append-only Conversation transcript authority. Runtime
messages/events remain execution and protocol audit. A model-visible transcript is only a
per-request projection. Permission/control records are never fabricated as ordinary user messages.

V1 Summary uses one Conversation-scoped contiguous rolling-prefix range plus an unsummarized raw
tail. The source range/digest, excluded entry classes, generator/policy versions, parent/transitive
lineage, InstructionManifest/workspace dependencies, content digest, validation, stale/
compatibility state, and supersession are durable. Minimum typed claims are conversational intent,
explicit decision, completed work, unresolved decision, repository/code fact, and tool/test fact.
Every claim references its source; code/tool claims also bind revisions/artifacts. Drift invalidates
only affected claims. Stale facts may appear only as explicitly historical.

Compaction is bounded to at most one attempt for the same source-range digest and policy version at
one safe boundary. Unsafe causal frontiers, unconsumed steering, open protocol groups, and pending
reconciliation are ineligible. Failure records its attempt and leaves transcript plus last valid
Summary unchanged. If optional history still does not fit, use bounded raw excerpts or omit lower
priority optional sources; no compaction retry loop is allowed.

A model-backed Summary request has its own auxiliary request and attempt lineage, follows the same
durable-before-provider rule, and never consumes an Agent ModelRequest ordinal or changes the Turn/
RuntimeExecution outcome. It must be separately attributed in metrics.

### 4.4 Tool and file evidence

`ToolResultArtifact` records originating ToolInvocation, channel, MIME/encoding, digest, captured
size/range, configured capture limit, and capture completeness. `NormalizedObservation` records a
bounded semantic/status projection, artifact handle, excerpt, and independent truncation/
completeness flags. Prompt truncation, durable-artifact completeness, and process-capture
completeness are separate facts. Existing results for which pre-M3 capture completeness cannot be
proven are imported/adapted as `unknown` or `incomplete`, never `complete`.

Large text defaults to a bounded observation plus handle. An authorized existing range reread
creates another bounded observation; it does not mutate the original artifact. M3 introduces no
new artifact-search tool or public UI. Binary results expose metadata/handle unless an existing
bounded renderer is available.

Every `FileContextItem` binds WorkspaceBinding, normalized relative path, file type where known,
content hash/revision, exact byte/line range and digest, and originating read/search/observation.
Revision mismatch before freeze excludes it, marks it historical, or rereads through an already
authorized read path. After freeze, the bytes remain immutable historical request input. Context
freshness never substitutes for ToolHarness write preconditions.

## 5. Frozen ModelRequest, atomicity, retry, and recovery

The normalized `ModelRequest` includes every model-visible message, tool schema, model option, and
semantic setting before transport-only adapter conversion. Provider wire formats remain in
adapters. Canonical JSON is UTF-8, sorted-key, compact encoding; semantic request digest is SHA-256
over that exact canonical normalized payload and excludes the audit envelope.

`ContextManifest` references the request ID and request-content digest. The request audit envelope
references manifest ID/digest. The manifest digest excludes only its own digest field. One SQLite
transaction publishes both sides, the Runtime model-start event/checkpoint mutation, and attempt
intent. This is the sole provider-start gate.

The following rules are exact:

1. crash before commit: no request, attempt, event, or provider call is visible;
2. commit succeeds and crash before call: recovery reuses the committed request and may start the
   already-recorded attempt according to its never-dispatched evidence, without recomposition;
3. provider attempt starts and outcome is unknown: append an unknown/censored outcome if absent,
   then create a new attempt ID/ordinal against byte-identical canonical request content; do not
   read workspace/context, re-run ContextComposer, overwrite request JSON, or reuse an attempt ID;
4. response is committed: recovery consumes the exact committed response and performs zero
   provider recalls;
5. retryable known failure or provider fallback: retain failed attempt and append a new attempt
   against the same request unless a new model decision is explicitly required;
6. new steering/reply/tool observation/reconciliation/explicit instruction refresh requiring a
   new decision: allocate a new ModelRequest and ContextManifest; never modify a prior pair; and
7. request/manifest/snapshot digest mismatch or required evidence loss: fail exact recovery closed.

This directly replaces the M0-characterized defect. The existing test
`test_future_m3_invariant_reuses_exact_frozen_request` must become a normal passing test without
weakening, skipping, deleting, or changing its equality assertion. The M0 evidence remains
historical and is not rewritten.

## 6. Additive Schema v7 and compatibility migration

M3 uses exactly one next additive SQLite migration: **Schema v7**. It may add private tables,
indexes, triggers, and nullable/additive compatibility columns required here. It must not rename,
drop, reinterpret, or destructively rewrite v1–v6 records.

The v7 physical design, recorded before implementation in
`docs/m3-instruction-context-design.md`, must provide normalized identities or an equally strict
representation for:

- instruction trust, source, scope, immutable content snapshot, source revision, manifest,
  manifest entry, and conflict/override edges;
- immutable frozen model request and atomic ContextManifest binding;
- provider attempt intent plus append-only terminal/unknown outcome history;
- Context composition/selection operation and timing/token/source attribution;
- auxiliary Summary-generation request/attempt lineage;
- Conversation-scoped SummaryArtifact and SummaryClaim;
- ToolResultArtifact, NormalizedObservation, and optional excerpt/range records;
- revision-bound FileContextItem/source reference; and
- idempotency receipts/provenance needed by trust, refresh, activation, compaction, and recovery.

The design must name every table/constraint/index, canonical digest field, immutable versus mutable
field, ownership FK, idempotency key, and crash seam. Representation freedom does not relax the
semantics above. SQLite remains the only durable authority; content-addressed snapshots/artifacts
may use agent-owned files only if SQLite atomically owns their committed metadata and a staged
file cannot be mistaken for committed evidence. No file is stored in the bound checkout.

Migration rules:

- empty/new and every supported v1–v6 database upgrade transactionally to v7;
- failure at every v7 DDL/index/provenance/commit boundary rolls the entire migration back and
  leaves v6 usable by the accepted M2 implementation;
- committed v7 reopen is idempotent; unknown future versions reject before M3 work;
- historical `model_calls.request_json`, response/error payloads, `summaries`, messages, events,
  Product rows, Memory, and IPC-visible behavior retain byte meaning;
- each pre-v7 model call may receive a truthful legacy compatibility projection. Its exact stored
  request is immutable after import, but missing Context/source provenance is explicitly
  `legacy_unavailable`, never fabricated;
- an in-flight pre-v7 uncertain call is imported before resume so retry uses its exact persisted
  request and a new v7 attempt identity;
- legacy SummaryRecord adapts as legacy execution-scoped derived evidence and is never relabelled as
  a fully proven Conversation SummaryArtifact; and
- there is no destructive downgrade. Rollback uses an untouched v6 copy or forward repair.

Legacy `model_calls` remains readable and receives a compatibility projection for new calls. Its
`request_json` must equal the immutable v7 request and may never be changed by a retry. New attempt
history is authoritative in v7; the legacy row may expose latest status/attempt for old readers but
cannot erase earlier attempts.

## 7. Compatibility and security invariants

- Preserve legacy `run_task()`, `resume_session()`, interrupt/resolve-call, copied workspaces,
  session inspection, eval/oracle, JSONL export/replay, calculator/todo smokes, and all four
  semantic goldens. New Product context may be additive; existing entry points do not silently
  become direct-tree writers.
- Preserve Runtime IPC v1 request/capabilities/events/results/order/cancellation/exit codes, schema
  digests, vectors, and read-only-source/copied-workspace semantics. No Product instruction,
  manifest, request, artifact, transcript, or private v7 field enters public IPC v1 and no new
  capability is advertised.
- Preserve one Runtime FSM and one Agent loop. ContextComposer does not transition state; Product
  coordinator does not build prompts; persistence does not decide lifecycle policy.
- Preserve provider-specific wire isolation in adapters and ToolHarness as the only real side-
  effect gateway.
- Instructions are untrusted soft context relative to enforcement. Prompt text cannot authorize a
  tool or weaken containment, policy, writer, revision, sandbox, or recovery rules.
- Product real-tree paths remain read-only across success, failure, crash, retry, resume,
  compaction, instruction discovery/refresh, and artifact operations. Agent-owned SQLite/blob
  storage remains outside the checkout.
- Memory is absent/off by default. Existing opt-in research seams remain behaviorally unchanged;
  M3 must not query, inject, migrate, or depend on Memory for core Context.
- `COMPLETED` remains Runtime completion, not task success. Historical metric meaning remains
  unchanged; new projections are versioned.

## 8. Explicitly prohibited M4–M7 and unrelated work

The following are out of scope, including as scaffolding, defaults, indirect effects, or
"temporary" helpers.

- **M4:** ToolDefinition/capability expansion, PermissionPolicy execution, PermissionRequest/
  PermissionDecision flow, ALLOW/ASK/DENY/UNAVAILABLE product enforcement, permission UX,
  create/edit/delete on a real tree, command/test/build/cache enablement there, Git inspection or
  index effects, diff authorship, controlled change evidence, real CodeCheckpoint content,
  CodeRewindOperation/undo, or a general Shell.
- **M5:** interactive CLI attachment, slash commands, progress/rendering, background host, daemon,
  responsiveness UX, or product command routing.
- **M6:** default Memory, History Search, UserPreference UX, automatic extraction, Core Snapshot,
  Dynamic Recall, semantic/vector/embedding/RAG serving, or Memory as required Context input.
- **M7:** destructive legacy rename/removal, compatibility cleanup, support-matrix publication, or
  end-to-end release claims.
- Any real-tree mutation through instruction files, artifact/cache placement, commands, tests,
  Git, startup/admission markers, hooks/helpers, indirect children, or undo. M3 completion alone
  does not permit one.
- Any public IPC schema or capability change, protocol duplication, Platform consumer work, new
  dependency or lockfile change absent a dated owner amendment, paid/live Provider run, Skill,
  MCP, multi-Agent, UI framework, or modification of the sibling `../hermes-agent` repository.
- Any change to M1/M2 accepted identity, admission, ordering, rebind, writer/recovery, real-tree
  read-only, or compatibility semantics; any rewrite of Accepted ADR text or historical evidence
  merely to pass a test.

## 9. Required implementation sequence and file ownership

Terra High executes in this order and stops at a failed gate rather than layering later behavior on
an unverified foundation.

1. **M3 physical design freeze:** author `docs/m3-instruction-context-design.md` with the complete
   v7 schema, canonical serialization/digests, source/provider bounds, policy version and numeric
   reservations, operation/event taxonomy, idempotency, fault seams, legacy adapters, and every
   deferred M4–M7 item. It may choose representation, not scope.
2. **Domain and migration:** add pure Product instruction/context/artifact/request value objects and
   v7. Prove empty/v1–v6/future/fault/reopen migration before application behavior.
3. **Instruction subsystem:** implement bounded discovery, containment/symlink rules, trust,
   snapshots, manifest resolution, conflicts, path activation, drift, refresh, and exact resume.
   Integrate real base-manifest publication with M2 admission without enabling a write consumer.
4. **Context and artifacts:** implement hybrid snapshot ports, transcript projection,
   ToolResultArtifact/NormalizedObservation/FileContextItem, deterministic required-envelope and
   allocator, ContextManifest, and digest validation. Keep Memory absent.
5. **Frozen request and retry:** make FrozenContextPackage plus first attempt one pre-provider
   transaction; add append-only attempts/outcomes, exact uncertain retry, fallback, committed-
   response reuse, and crash recovery. Turn the M0 expected failure into a passing invariant.
6. **Summary migration:** add Conversation SummaryArtifact/claims, safe rolling-prefix compaction,
   separately frozen auxiliary requests/attempts, failure/regeneration/staleness, and the legacy
   SummaryRecord adapter.
7. **Observability and closure:** implement M0 metric identities/classifications, run every gate,
   produce the verification report, and update implementation-status docs. M3 remains ACTIVE until
   owner review/acceptance; the executor must not mark it Accepted/complete.

Expected ownership:

- `product_domain.py`: Product instruction, manifest, context-source, artifact, transcript,
  Summary, and frozen-request value objects/invariants; no SQL or provider wire format.
- New focused modules such as `product_instructions.py` and `product_context.py`: bounded discovery,
  resolution, stateless composition, allocation, and projections; no durable authority or FSM.
- `product_persistence.py`, `persistence.py`, and `migrations.py`: narrow v7 repositories,
  transactions, immutable/digest checks, migration, and compatibility projections; no hidden
  instruction precedence or context-selection policy in SQL.
- `context.py`: adapt existing counters/capabilities/builders/section renderers behind the new
  composer; preserve legacy builders and Memory-off defaults.
- `compression.py`: SummaryArtifact generation/validation adapters and auxiliary request seam; no
  transcript ownership or Runtime FSM.
- `domain.py` and `runtime.py`: minimum normalized request/attempt/event integration, atomic freeze
  gate, exact retry/reuse, and causal snapshot seam. Runtime retains every transition/retry
  decision.
- `product_application.py`: thin typed trust/refresh/activation and snapshot orchestration only;
  no prompt assembly, filesystem authority, tool execution, or mutable aggregate.
- `product_workspace.py`/`workspace.py`: read-only bounded instruction and file revision evidence;
  retain M2 observation-only Product behavior and legacy copied-workspace behavior.
- `models/`: only usage-presence/attempt-boundary metadata needed by the normalized adapter
  contract. Provider wire behavior remains adapter-local and compatibility-tested.
- `tools/`, `sandbox/`, `command_profiles.py`, `test_profiles.py`, `cli.py`, `protocol/`, and
  `memory/` are not M3 feature owners and remain behaviorally unchanged except a minimal post-
  Harness artifact-capture seam if unavoidable. No schema, capability, permission, or tool is
  added there.
- Focused M3 tests belong in new Product instruction/context/request/recovery modules plus migration
  and persistence tests. Existing historical/golden tests are regression evidence, not rewrite
  targets.

## 10. Adversarial acceptance matrix

| Area | Required proof |
|---|---|
| Instruction discovery | repository root, ProjectScope root, nested and cross-directory target paths, non-Git root, linked worktree, independent clone, rebind, unrelated nested repository, stable enumeration, source/count/byte/decode bounds |
| Trust and containment | inactive versus active content, RepositoryIdentity+ProjectScope scoping, cross-clone non-transfer, workspace-local non-transfer, revocation/new-Turn behavior, contained symlink, external/broken/cyclic symlink and import unavailable, no escaped-target content read |
| Manifest semantics | base admission atomicity, precedence/specificity/additive rules, deterministic structured override, natural-language conflict to correlated WAITING_USER_INPUT, unaffected work continues, path activation before next request, uncovered-write classification, exact-review seam, immutable revision/digest/snapshot |
| Drift/refresh/resume | IDE/Agent drift after load, no silent switch, explicit idempotent refresh at safe boundary, old requests retain old epoch, checkpoint restores active revision, missing/corrupt snapshot rejects exact resume, new Turn/rebind rediscovers current sources |
| Required envelope | current intent/steering/reply/instructions/resolver outcome/protocol group retained atomically, tool-schema/framing/output cost included, overflow fails explicitly, no provider call or silent truncation |
| Deterministic allocation | identical inputs reproduce bytes/digests/selection; reservations and lending; stable ties; bounded recent tail; stale/inactive/untrusted/superseded exclusions; map/filesystem order perturbation; Memory absent |
| Freeze atomicity | injected failure before/after every request, manifest, digest-binding, checkpoint/event, attempt-intent and precommit seam; no provider before commit; no half pair; concurrent composers cannot duplicate ordinals or publish stale frontiers |
| Retry/fallback | crash after commit-before-call; crash/unknown during call; byte and semantic equality across uncertain retry; unique attempts; current workspace/context deliberately changed but unread; known retry/fallback history; committed response reuse with zero recalls; digest corruption fails closed |
| Transcript/Summary | append-only Conversation entries across completed/failed/cancelled Turns; Runtime-only records excluded; contiguous prefix/raw tail; unsafe frontier blocked; typed claim provenance; summary-of-summary lineage; validation/generation failure; regeneration/supersession; affected code claim stales while user intent stays valid |
| Tool artifacts | complete versus prompt-truncated, capture-incomplete, unknown legacy completeness, stdout/stderr/structured channels, huge output, binary metadata, bounded observation, handle/range reread, removed/corrupt artifact makes dependency/recovery unavailable rather than complete |
| File/snippet evidence | path containment, binding/revision/range/digest, pre-freeze drift exclusion/reread, post-freeze drift preserves historical bytes, rebind incompatibility, old Conversation file bodies never become current code authority |
| Migration/reopen | empty/new and v1–v6 upgrades, future rejection, each v7 statement/provenance/commit fault, v6 usability after rollback, idempotent reopen, terminal/interrupted/retry/uncertain/legacy summary/Memory rows, in-flight uncertain import |
| Lifecycle/M2 compatibility | atomic admission, typed input ordering, wait/finalization races, immutable execution binding, rebind, writer/recovery barrier, read-only direct tree, no new wait FSM, one Turn/one RuntimeExecution, no coordinator authority growth |
| Compatibility/security | four semantic goldens; legacy one-shot/resume/admin/eval/copied workspace; IPC schemas/vectors/goldens; JSONL/replay; provider adapters; Memory-off; exact instruction trust cannot alter enforcement; no real-tree mutation under success/failure/crash/retry/resume |
| Metrics | model success/failure/retry/fallback/cancel/unknown; context success/overflow/stale rebuild/failure; compaction success/reject/failure; auxiliary lineage; per-section estimates; measured provider usage; estimated tokenizer usage; absent usage unknown; incomplete totals never exact; no success-only sampling |

Every durable path requires success, expected-failure, rollback, crash/restart, idempotency, and
reopen evidence appropriate to its state. Tests use disposable test-owned repositories under
temporary directories and never mutate the development checkout or another user tree.

## 11. Metrics identities and no-live-provider policy

Every new record carries applicable `Conversation`, `Turn`, `RuntimeExecution`, `ModelRequest`,
model-attempt, auxiliary-request, Context operation, Summary, and source/artifact identities.
Legacy samples retain legacy labels rather than fabricated Product provenance.

Required versioned projections are:

- `model_attempt_latency_ms`: every started attempt, with process-local monotonic segment plus
  durable wall anchors; missing end is censored/unknown, never omitted;
- `context_compaction_latency_ms`: separate context composition/selection and auxiliary compaction
  populations, including failures and fallbacks;
- `token_usage`: input/output/cache units per Agent or auxiliary attempt plus per-section local
  attribution, classified measured/estimated/synthetic/unknown;
- request/manifest counts, required-envelope overflow, omission/stale reason counts, compaction
  outcome, retry/fallback/response-reuse counts, and unknown-usage coverage as explanatory
  dimensions, not replacements for the frozen M0 metrics; and
- machine-active/retry-recovery components only where covered; overlapping spans use interval
  union, process-local clocks are never subtracted across restarts, and host gaps remain unknown or
  explicitly estimated.

Provider usage absence is not zero. An aggregate with any uncovered required component is marked
incomplete/unknown coverage. Failures, retries, cancellations, uncertain attempts, and auxiliary
calls remain in denominators/populations.

M3 acceptance is deterministic and credential-free. Use scripted/fake backends and injected
measured/absent usage. Do not run a live or paid Provider without a dated owner amendment naming a
new M3-specific hypothesis, model/configuration, budget, evidence handling, and stop condition.
Lack of a live run waives no deterministic gate and M3 makes no real-provider quality, cost, or
latency claim.

## 12. Quality gates and required evidence

Before proposing M3 complete, run and report actual results for:

- `PYTHONPATH=src python3 -m unittest discover -v` with no unexpected failure and with the former
  M3 uncertain-retry expected failure now passing normally;
- focused instruction, trust, context, transcript, Summary, artifact, migration, freeze, attempt,
  retry/recovery, observability, and real-tree closed-gate suites covering section 10;
- the four protected semantic goldens, Runtime IPC v1 producer/schema/vector/golden tests, and
  JSONL export/replay compatibility;
- Ruff, configured mypy plus targeted typing for every M3-owned source file, and compileall;
- the configured statement-coverage threshold, with no M3 critical freeze/retry/recovery/
  instruction-trust transaction omitted;
- wheel/sdist build and isolated install/import smoke for every new private Product module;
- calculator and todo copied-workspace scripted smokes;
- the existing Memory cold/warm regression only to prove Memory-off/default wiring did not change;
- a disposable direct-binding Product smoke plus before/after manifest proving zero file,
  directory, symlink, Git index/ref/lock, cache, instruction, artifact, or startup mutation in the
  bound tree across success, overflow, failure, retry, crash, and resume; and
- changed-document local-link/fence audit, repository-root artifact scan, and `git diff --check`.

Record commands, pass/fail/skip/expected-failure counts, coverage, platform limitations, schema/
artifact hashes, fault matrix, and metric coverage in
`docs/evidence/m3-implementation-verification-YYYY-MM-DD.md`. Historical M0–M2 counts are not new
evidence. No live Provider is part of this contract.

## 13. Exit checklist and exact stop condition

No item may be checked until its success, failure, rollback, crash/recovery, idempotency, and reopen
evidence passes where applicable.

- [x] M3 physical design and additive v7 representation are documented and ADR-consistent.
- [x] v7 empty/v1–v6/future/fault/reopen migration passes without changing historical meaning.
- [x] Bounded instruction discovery, trust, immutable snapshots, precedence/conflict, activation,
      drift/refresh, exact resume, and exact-review classification pass.
- [x] Hybrid ContextComposer, non-evictable required envelope, deterministic allocation,
      revision-bound sources, and explanatory ContextManifest pass with Memory absent.
- [x] Transcript remains append-only authority; SummaryArtifact/claims, ToolResultArtifact/
      NormalizedObservation, and FileContextItem boundaries pass provenance/staleness/failure tests.
- [x] Exact ModelRequest plus ContextManifest commit before provider execution is atomic; request and
      attempt histories are immutable/explainable across every crash seam.
- [x] Unknown-outcome retry replays the exact frozen request with a new attempt; committed response
      reuse performs zero provider calls; the M0 expected failure is now a normal pass.
- [x] M0 model/context/compaction metrics cover every outcome and classify measured, estimated,
      synthetic, unknown, censored, and incomplete totals correctly.
- [x] M1/M2 lifecycle, read-only real-tree gate, legacy one-shot/eval/copied workspace, provider
      adapters, IPC v1, JSONL/replay, Memory-off, and four semantic goldens pass unchanged.
- [x] Full deterministic quality evidence and implementation-status documentation are ready for
      owner review, with no M4–M7 behavior or real-provider claim.

When all items are green, Terra High stops, reports the exact diff and evidence, and requests Sol
High owner review. Terra High must not commit, push, mark M3 Accepted/complete, activate M4, or open
real-tree mutation unless the owner separately authorizes those actions. If Terra High encounters
an execution barrier or a problem it cannot safely solve, it stops with concrete evidence so the
owner can transfer only the remaining contract scope to Sol Medium.

Stop earlier and request owner direction if an Accepted authority conflicts, v7 cannot remain
additive, the exact request/manifest cannot share one pre-provider SQLite boundary, instruction
trust would become enforcement authority, a supported legacy/IPC behavior would break, the
real-tree read-only gate cannot be proven closed, or any acceptance criterion requires M4–M7, a
new dependency, or a live Provider. Any material scope change requires a dated contract amendment;
never silently broaden or weaken this contract.

## 14. Downstream status

The owner accepted M3 as **ACCEPTED / COMPLETE** on 2026-09-23 after the deterministic exit
matrix in [`../evidence/m3-implementation-verification-2026-09-23.md`](../evidence/m3-implementation-verification-2026-09-23.md)
passed. The publication-turn and executor-stop instructions above remain the historical issuance
record; this dated closure does not silently amend their scope. M0, M1, and M2 remain
**ACCEPTED / COMPLETE**. M4, M5, optional M6, and M7 remain **inactive**. M3 completion changes no public Runtime IPC, does not enable
interactive CLI or default Memory, and does not permit any real-user-working-tree mutation.
