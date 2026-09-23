# M3 Instruction, Context, and Frozen Request Design

Status: Product-Layer M3 implementation design; **M3 ACCEPTED / COMPLETE** (2026-09-23).

## Authority and boundary

This design implements the accepted Project Instructions and Context/Compaction ADRs without
changing the Runtime FSM, public Runtime IPC v1, ToolHarness, provider wire adapters, M2
admission/rebind/writer semantics, or real-tree read-only gate.  `sessions` remains Runtime FSM
authority; `conversation_semantic_events` remains the append-only Conversation transcript;
`model_calls` remains a compatible legacy projection.  SQLite is the sole durable authority.

Trust affects only soft model-context eligibility.  It never grants a capability, permission,
writer claim, filesystem exception, ToolHarness bypass, or M4 approval.  M3 implements no
real-tree write, command, test, cache, Git action, prompt UI, Memory serving, or public IPC field.

## Schema v7

V7 is one additive `BEGIN IMMEDIATE` migration.  Each DDL/index and the migration provenance row
is fault-injected; failure rolls back wholly to a usable v6 database.  The following private
tables are owned by the indicated Product boundary:

| Table | Durable invariant |
|---|---|
| `instruction_trusts`, `instruction_sources`, `instruction_snapshots`, `instruction_manifests`, `instruction_manifest_entries`, `instruction_override_edges` | Trust is exactly repository/scope (and optionally binding) scoped; snapshots are content-addressed immutable bytes; manifests and entries are immutable/revisioned. |
| `frozen_model_requests`, `context_manifests`, `model_attempts`, `model_attempt_outcomes` | A canonical normalized request SHA-256 and non-cyclic manifest digest bind in one pre-provider transaction. Attempts are append-only and refer to one request. |
| `context_operations`, `context_selection_items` | Composer policy/counter/budget, selection/omission/staleness and measured/estimated/unknown attribution are durable. |
| `conversation_summary_artifacts`, `conversation_summary_claims` | A summary is a derived contiguous prefix with typed source claims; it never replaces transcript authority. |
| `tool_result_artifacts`, `normalized_observations`, `file_context_items` | Capture completeness, prompt truncation, provenance, ranges, binding and revision are separate, truthful facts. |
| `product_operation_receipts` | Trust, refresh, activation and compaction operations are idempotent by `(operation_id, payload_digest)`. |

All identities are UUID strings. Canonical payloads use UTF-8 `json.dumps(sort_keys=True,
separators=(',', ':'), allow_nan=False)` and SHA-256.  Immutable rows are insert-only; mutable
fields are limited to a current trust disposition, staleness/compatibility status, and explicit
operation completion markers.  Every table carries its owning Conversation/Turn/RuntimeExecution
or repository/scope/binding foreign key as applicable.  Indexes cover manifest revision ordering,
attempt ordinal per request, transcript/summary source range, and idempotency keys.

### Physical v7 record families

The migration is additive and retains all v1–v6 tables. This is the complete physical v7 surface;
column order follows `migrations.py` and `TEXT` identities are opaque UUIDs unless noted.

| Family | Physical columns and constraints |
|---|---|
| Trust | `instruction_trusts(trust_id PK, repository_id FK, project_scope_id FK, workspace_binding_id FK nullable, source_kind, disposition active/revoked, operation_id UNIQUE, payload_digest, created_at, revoked_at)`; active/revoked timestamps are checked. |
| Source/snapshot | `instruction_sources(source_id PK, repository_id/project_scope_id/workspace_binding_id FKs, provider_kind, locator, normalized_path, scope_kind, authority_rank, specificity, revision_digest, disposition, reason, discovered_at)` has scope/provider/locator/revision uniqueness. `instruction_snapshots(snapshot_id PK, content_digest UNIQUE, canonical_utf8 BLOB, byte_count <= 262144, created_at)` checks exact byte length and is immutable. |
| Manifest | `instruction_manifests(instruction_manifest_id PK, conversation_id/turn_id/workspace_binding_id FKs, parent_manifest_id self-FK, revision >= 1, effective_digest, refresh_operation_id UNIQUE nullable, refresh_reason, policy_version, load_sequence_frontier >= 0, status, created_at)` is unique by conversation/revision. Entries bind source/snapshot, order, precedence, trust, disposition and digest. Override edges bind winner/loser/key/resolution. Conflict and status-event tables are append-only history, so drift never rewrites an immutable manifest. |
| Frozen request/context | `frozen_model_requests(request_id PK, runtime_execution_id FK nullable, legacy_session_id FK, request_ordinal, request_kind, context_manifest_id UNIQUE deferred FK, request_digest, request_json, audit_json, legacy_model_call_id FK, runtime_event_sequence, runtime_version, created_at)` and `context_manifests(context_manifest_id PK, request_id UNIQUE deferred FK, request_digest, manifest_digest UNIQUE, manifest_json, policy/counter versions, provider capability JSON, context_window, output_reserve, framing_margin, tool_schema_tokens, required_tokens, optional_tokens, memory_status='absent', created_at)`. Mutual FKs are `DEFERRABLE INITIALLY DEFERRED`, making the pair insertable in one transaction. Repeated byte-identical requests may share a digest but never an identity. |
| Attempts | `model_attempts(attempt_id PK, request_id FK, ordinal, backend, intent_status, wall_started_at, monotonic_started, created_at)` is unique by request/ordinal. `model_attempt_dispatches(dispatch_id PK, attempt_id UNIQUE FK, dispatched_at, monotonic_started, created_at)` is the provider-gate fact. `model_attempt_outcomes(outcome_id PK, attempt_id UNIQUE FK, outcome_kind, response/error/usage JSON, usage_classification, wall_finished_at, monotonic_elapsed_ms, coverage_status, created_at)` is terminal/censored evidence. All three are append-only. |
| Context operations | `context_operations(context_operation_id PK, request/conversation FKs, operation_kind/status, policy_version, start/finish/elapsed, metrics_json, coverage_status)` and `context_selection_items(selection_id PK, operation FK, source class/id/sequence, disposition/reason, token_count/classification/source_digest)` explain every selection. |
| Summary | `conversation_summary_artifacts` carries Conversation, contiguous source range/digest, content/digest, policy/generator, parent, manifest/binding provenance, status/supersession and timestamp. `conversation_summary_claims` carries typed claim, source range, optional artifact/revision, status and timestamp. |
| Tool/file evidence | `tool_result_artifacts` separates bounded raw bytes, channel/type/encoding, digest/range/limit and completeness. `normalized_observations` separately records semantic JSON, excerpt/digest, prompt truncation and projection completeness. `context_excerpts` records reread ranges. `file_context_items` binds normalized path/type/content hash/revision/range digest to WorkspaceBinding and origin. |
| Operations/metrics | `product_operation_receipts(operation_id PK, payload_digest, operation_kind, result_json, created_at)` supplies exact idempotency. `m3_metric_samples` carries metric/population and nullable Conversation/Turn/Runtime/request/attempt/context identities, value/unit, classification, coverage, dimensions and timestamp. |

Indexes cover source lookup, manifest revision and entry order, status history, attempt ordinal and
dispatch identity, selection order, summary prefix, file revision and metric population. Immutable
snapshot, manifest, request, ContextManifest, attempt, dispatch and outcome triggers reject update;
frozen request/context and snapshot deletion is rejected; foreign keys reject partial provenance.

### Digests, atomic freeze, and compatibility

An instruction snapshot digest is SHA-256 over canonical UTF-8 bytes. An effective-manifest digest
covers ordered entries, trust/disposition, snapshot digests, override edges and unresolved conflict
keys. The normalized request digest covers exactly provider-independent ModelRequest JSON; audit
fields do not create a digest cycle. ContextManifest digest covers request binding, policy/counter,
causal frontier, numeric budget ledger and all included/omitted selections.

The Runtime `MODEL_CALL_STARTED` journal mutation inserts the request, ContextManifest, context
operation/selections and first attempt intent inside the same SQLite transaction as its event and
checkpoint. Provider dispatch is appended immediately before adapter invocation. A precommit fault
publishes no gate; commit-before-dispatch restart reuses the same attempt/request without
recomposition; dispatch without outcome appends censored unknown and starts a new attempt against
the same immutable request. A committed response is reused with zero provider calls.

V7 fault injection runs before/after every DDL statement plus provenance and commit seams. Rollback
leaves v6 readable and reopen is idempotent. Startup compatibility import projects each pre-v7
`model_calls.request_json`, including a running call, into immutable request/manifest/attempt lineage
before resume. `model_calls` remains a latest-status compatibility projection, never attempt-history
authority.

## Instruction provider and manifest

The initial provider recognizes only exact UTF-8 `AGENTS.md`. It is filename-independent behind
the provider port. Discovery starts only from explicit repository/scope/binding paths; never cwd,
subprocess Git, project code, or a checkout write. It has 256 KiB/source, 64 source and 1 MiB total
text bounds. Lstat/containment/decode/read failures become stable unavailable entries. Contained
symlinks may load; escaping, broken or cyclic targets are recorded inactive without reading target
content. Source ordering is normalized locator order.

The base manifest is prepared outside admission, revalidated, then published atomically with an
M3 admission. Its precedence is durable Turn intent, explicit conversation instruction,
path-specific rule, repository rule, user global, historical context. Specificity only resolves a
direct conflict; unrelated instructions remain additive. Unresolved persisted conflicts use
correlated ordinary-input waits, not new FSM states. Drift leaves the frozen snapshot
effective and marks it stale. Refresh is an idempotent safe-boundary operation that creates a new
manifest revision; resume verifies the old snapshots rather than reading current files.

Trust matches the exact source kind and provider, not a provider-agnostic fallback. The bounded
deterministic prose classifier recognizes explicit positive/negative modal rules (`always`,
`never`, `must`, `must not`, `do not`, `don't`) over the same normalized action text, in addition
to structured `rule.*` keys. It records the two source/rule identities and reason; it does not
infer broader authority from vague or unrelated prose. A correlated UserReply resolves the
persisted conflict into a successor manifest revision and durable resolver outcome. Once a source
has drifted, its stale status remains until an explicit refresh even if file bytes later revert.

## Composition and freeze

`ContextComposer` is stateless. Product supplies Conversation/Turn inputs, manifest, binding and
eligible summaries; Runtime supplies causal frontier, protocol group, resolver outcome and tool
schemas. The required envelope is non-evictable: system/runtime instructions, current intent and
effective controls, active manifest content, indivisible protocol group, resolver outcome and
constraints. Tool schemas, framing margin and output reserve are charged before optional classes.
Overflow is `context_required_content_exceeds_budget`, with no provider call.

Optional allocation is deterministic: recent transcript, bounded observations, revision-bound file
evidence, valid rolling-prefix summary, then empty Memory. Stable class/source keys and explicit
reservations/lending generate reproducible bytes, selection and digests. Frozen context bytes are
historical request input: later drift never substitutes them.

Composition uses the Runtime's selected model capability and token counter. Each optional class
receives its minimum reservation before higher-priority classes can spend beyond theirs; unused
reservation is then lent in stable class order. The manifest retains the reservation, allocation,
lending and selection ledger. Typed v7 observations and file items require capture/revision
eligibility; a SummaryArtifact contributes only valid claims over a verified origin-contiguous
Conversation semantic prefix. A stale code claim does not invalidate unrelated conversational
claims.

One shared journal transaction persists `FrozenModelRequest`, `ContextManifest`, their digest
bindings, context operation/selection rows, Runtime checkpoint/event frontier, and first attempt
intent. A failed transaction invokes no provider. Unknown outcome appends an unknown terminal row
then starts a new attempt against byte-identical request bytes; committed response reuses its exact
payload without a provider call. A genuinely new decision allocates a new request.

The freeze carries Conversation/Turn/Execution identity, product version, semantic frontier and
active manifest revision. The same `BEGIN IMMEDIATE` that publishes it revalidates those facts.
If a concurrent valid SteeringInput advances the frontier, Runtime discards that candidate and
re-snapshots/recomposes within a bounded retry before dispatch. Concurrent instruction refresh is
permitted only at a safe Runtime boundary; one attempted during `CALLING_MODEL` is rejected.

## Summary, artifacts, and deferred work

Summary compaction is one safe-boundary attempt per source-range digest/policy. It creates a
separately attributed auxiliary frozen request/attempt and a source-provenanced rolling-prefix
artifact on success; failure preserves transcript and prior valid summary. Legacy SummaryRecord is
only an execution-scoped compatibility projection. Runtime separates auxiliary request preparation
from invocation: the exact request, manifest and attempt intent commit with `COMPRESSION_STARTED`,
dispatch is appended immediately before the summarizer, and terminal outcome/metrics append with
finish/rejection. A pre-dispatch crash reopens the exact pending request; a dispatched request with
no outcome is classified unknown and never silently called again. Post-Harness tool completion
projects bounded ToolResultArtifact/NormalizedObservation rows in the same journal transaction;
legacy capture completeness that cannot be reconstructed is `unknown`. Tool/file artifacts
distinguish durable capture completeness from prompt truncation; no new tool is added.

Native Conversation compaction verifies an exact origin-contiguous eligible semantic-event prefix
and derives conservative typed claims with per-claim event ranges and artifact/revision bindings.
Only validated auxiliary success publishes and supersedes; failure keeps the previous valid
artifact. After a valid summary, newly accumulated recent messages can cross the high-watermark
again and initiate a second auxiliary generation over a longer prefix; a summary by itself cannot
retrigger compression. ID/content collisions fail closed. M0 metric samples preserve absent provider usage as
unknown rather than zero, distinguish Agent and auxiliary attempts, and retain every available
Conversation/Turn/Execution/request/attempt identity and section attribution.
`FALLBACK_SELECTED` also emits a separate one-count explanatory metric bound to the failed
request/attempt and backend pair; it does not duplicate attempt latency or token usage.
Auxiliary compaction operations remain `started` at freeze and transition to the actual
success/failure/unknown/cancelled outcome; a rejected attempt without any auxiliary request has
its own rejection operation. Unmeasured compaction timing remains null with incomplete or censored
coverage, rather than borrowing provider-attempt latency as whole-operation time.

Deferred: M4 permissions/ASK and all mutations/diff/undo, M5 CLI, M6 serving/RAG, M7 removal, and
all public IPC changes. A recognized instruction source mutation has only the pure
`exact_review_required` classification seam for M4.
