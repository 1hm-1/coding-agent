# M2 Product Lifecycle Design

Status: accepted implementation design for Product-Layer M2; **M2 ACCEPTED / COMPLETE** on
2026-09-23. M3–M7 remain inactive.

## Boundary

M2 adds a Product application projection around the retained Runtime Kernel.  `sessions`,
`checkpoints`, and legacy event records remain the only Runtime FSM authority.  A Conversation
therefore has ordering and continuity, never a second state machine; a Turn has one immutable
RuntimeExecution and the Session remains its compatibility linkage.

The design follows [project/repository/workspace/conversation](decisions/project-repository-workspace-conversation.md)
and [conversation/turn/runtime-execution lifecycle](decisions/conversation-turn-runtime-execution-lifecycle.md).
It does not discover instructions, freeze ModelRequests, grant permissions, create diffs, or offer
an interactive CLI; those remain M3--M5 work.

## Schema v6

V6 is one additive transaction. It adds nullable `conversations.open_turn_id` and non-null
`conversations.product_version INTEGER DEFAULT 0`; it does not add Turn state.

| Table | Columns and constraints |
|---|---|
| `product_admissions` | `operation_id` PK; `payload_digest`; `conversation_id` FK; unique `turn_id` FK; unique `runtime_execution_id` FK; unique `legacy_session_id` FK; `checkpoint_id`; `instruction_manifest_id`; `policy_epoch_id`; `created_at` |
| `product_inputs` | `input_id` PK; unique `operation_id`; `payload_digest`; `conversation_id` FK; nullable `turn_id` FK; `sequence`; `input_kind`; nullable `correlation_id`; `payload_json`; `status`; `created_at`; unique `(conversation_id, sequence)` |
| `turn_admission_artifacts` | `turn_id` FK/PK; unique `checkpoint_id`; `checkpoint_json`; unique `instruction_manifest_id`; `instruction_manifest_json`; `policy_epoch_id`; `policy_json`; `created_at` |
| `workspace_binding_observations` | `observation_id` PK; `workspace_binding_id` FK; `observation_digest`; `observation_json`; `observed_at`; unique `(workspace_binding_id, observation_digest)` |
| `conversation_rebinds` | `rebind_id` PK; unique `operation_id`; `payload_digest`; `conversation_id` FK; `previous_workspace_binding_id` FK; `workspace_binding_id` FK; `expected_version`; `resulting_version`; `observation_json`; `created_at` |
| `workspace_writer_claims` | `workspace_binding_id` FK/PK; unique `claim_id`; unique `operation_id`; `payload_digest`; `owner_id`; nullable `runtime_execution_id` FK; `claim_epoch`; `expires_at`; `status`; `observation_digest`; `observation_json`; `created_at`; nullable `released_at` |
| `workspace_writer_operation_receipts` | `operation_id` PK; `payload_digest`; `operation_kind`; `claim_id`; `result_json`; `created_at` |
| `workspace_recovery_barriers` | `barrier_id` PK; `workspace_binding_id` FK; nullable `claim_id`; `reason`; nullable `uncertain_invocation_id`; nullable `resolver_kind`; nullable `evidence_digest`; nullable `resolved_by_operation_id`; `status`; nullable `reconciliation_token`; `created_at`; nullable `cleared_at` |
| `turn_finalizations` | `turn_id` FK/PK; unique `runtime_execution_id` FK; `outcome`; `finalized_at`; `repair_provenance` |

Indexes are `product_inputs_conversation_order(conversation_id, sequence)`,
`workspace_observations_binding(workspace_binding_id, observed_at)`,
`recovery_barriers_binding(workspace_binding_id, status)`,
`writer_operation_receipts_claim(claim_id)`, and the partial unique
`recovery_barriers_uncertain_invocation(workspace_binding_id, uncertain_invocation_id)` for
non-null invocations. Existing v1–v5 columns, keys, indexes, and payloads retain their meaning.

Every ALTER/table/index/provenance step runs inside one `BEGIN IMMEDIATE`. Fault injection covers
each statement plus provenance and commit seams; failure rolls all v6 DDL back and leaves v5 usable.

## Canonical identities and transitions

Operation digests are SHA-256 over canonical sorted compact JSON. Admission binds request,
repository/scope/binding, immutable Session identity/policy, Conversation, and expected version.
Input binds kind, Conversation, payload, correlation, and expected version. Rebind binds
Conversation, binding, version, and observation. Writer acquire binds binding/owner, expected
epoch/observation, declared observation, lease duration `0 < seconds <= 300`, and optional execution;
renew/release bind exact claim/owner/epoch/observation. Unknown-effect resolution also binds the
invocation, exact resolver, and server-derived evidence. Equal operation/digest replays its receipt;
altered reuse conflicts.

`product_version` advances for admission, input/control, rebind, and finalization.
`open_turn_id` is set only by admission and cleared only by finalization. Runtime state remains
`sessions.state`; M2 calls the shared state machine and journal. A mapped terminal journal mutation
atomically writes the Runtime event/checkpoint, finalization, semantic event, pending-control
closure, open-Turn clear, and writer release. Live `INTERRUPTED`, `WAITING_USER_INPUT`, and
`WAITING_PERMISSION` commits release their execution writer in the same transaction. Release never
clears a recovery barrier. Expiry cannot transfer authority: it creates a barrier requiring the
exact recorded claim/observation reconciliation digest, and epoch advances only after release and
reconciliation.

## Admission and recovery

`SQLiteRunJournal.admit_turn` is the M2 repository transaction boundary.  It checks the operation
id/digest first, validates RepositoryIdentity/ProjectScope/WorkspaceBinding compatibility, then
in one SQLite transaction publishes a new or idle Conversation, initial ProductInput, Turn,
RuntimeExecution, normal legacy Session/checkpoint/session-created and message events, checkpoint
shell, manifest placeholder, read-only policy epoch, admission record, and semantic event.  A
retry with equal digest returns the same IDs; a different digest conflicts.  `BEGIN IMMEDIATE`,
the open-Turn marker, and `product_version` serialize competing callers.  No filesystem, provider,
ToolHarness, Runtime transition, command, or Git action occurs in that transaction.

The checkpoint is `m2_observation_only`, explicitly excludes M4 content/diff/undo coverage, and
the instruction manifest is `m2_placeholder_no_discovery`.  Thus no v5 synthetic import is ever
treated as an admitted Turn.  Failed publication rolls every bundle member back.  Post-admission
initialization is outside this transaction and must terminally fail the accepted Session rather
than delete it. Before/after fault seams cover Conversation publication, the Turn,
RuntimeExecution, every internal Session member, admission artifacts, InitialRequest, admission
record, Conversation update, both semantic events, and precommit; new and existing idle
Conversation paths must preserve their complete prior database state on every injected failure.

Every M2 operation digest is recomputed from a canonical server-side payload before publication;
an operation ID therefore cannot be replayed against another Conversation, input kind, binding,
version, correlation, or payload.  Existing-Conversation admission requires both the current
`product_version` and its recorded default binding.  A later binding is possible only through an
audited, idle rebind.  Initial request, steering, ordinary-input request/reply, cancel intent,
admission, and finalization have distinct ordered semantic events.  A reply resolves exactly one
persisted ordinary-input request; it cannot resolve permission or reconciliation domains.

Every non-admission input/control digest includes the expected Conversation version. The acceptance
transaction checks it before allocating the durable sequence. At a Runtime safe boundary accepted
SteeringInput rows are selected and consumed as one current ordered batch in a single transaction,
then delivered as retained Runtime `Message` records. The internal boundary does not carry a stale
previously inspected Product version: a concurrent valid acceptance lands in this batch or a later
safe boundary and cannot fail Runtime. A matching UserReply appends its text in the same transaction
as its `WAITING_USER_INPUT` to `BUILDING_CONTEXT` FSM transition. This uses Runtime message
authority and does not create a second Product transcript.

`CANCELLED`, `WAITING_USER_INPUT`, `WAITING_PERMISSION`, and `WAITING_RECONCILIATION` are distinct
Runtime representations, with `WAITING_APPROVAL` retained only for legacy uncertain-effect rows.
The state machine owns legal transitions; Product persistence only projects terminal outcomes.
Post-admission initialization failure and normal M2-owned terminal Runtime mutations publish the
Runtime outcome and Product finalization in one SQLite transaction. Accepted but undelivered
steering/reply/cancel closes as `closed_terminal_unconsumed`; a finalization race is therefore
either ordered before the input or rejects it after Turn closure, never leaving a dangling accept.

## Workspace and gate

Direct binding discovery receives an explicit concrete directory, resolves it without cwd fallback,
discovers any enclosing repository root, and records only local filesystem/Git-layout observations.
It never invokes Git, executes project code, or writes checkout metadata. Observations carry a
bounded-content frontier, exclusions, topology, directory-entry/file-content and symlink-target
digest, plus readable symbolic HEAD, branch-tip, and index hashes. Dirty, staged, and untracked
facts are explicitly unavailable when no observation-only probe can establish them; they are never
guessed. A nested binding uses the enclosing repository identity and its actual repository-relative
ProjectScope.
Repository IDs remain generated persisted authorities;
canonical path/common-dir are discovery evidence and remote/history only evidence.  A direct
binding is immutable for a RuntimeExecution.  `rebind_conversation` accepts a compatible explicit
binding only when no Turn is open and records the observation/version audit.

M2 Product direct-tree composition is an explicit read-only capability.  All mutations, commands,
tests/builds/caches, Git operations, hooks/helpers, startup artifacts, indirect effects, and undo
are rejected before dispatch.  Writer claims remain coordination evidence only: expiry creates a
durable barrier and only matched reconciliation can clear it.  M2 does not open the M3+M4 gate.

Linked-worktree `HEAD` comes from its worktree gitdir while loose or packed branch refs come from
the common directory. Any lstat/content/symlink failure or entry/byte bound produces an explicit
incomplete frontier and bounded exclusion, never a false `complete` label. The 10,000-entry bound
counts directories, files, and directory symlinks. The standalone no-mutation manifest records
directory lstat mode/type metadata in addition to files, empty directories, symlink targets, and
the Git surface, so directory chmod drift is observable.

The usable read surface accepts only `read_file`/`search_files`, routes them through the existing
`ToolHarness` and `WorkspaceGuard`, and grants only `READ`.  Direct observations use bounded,
in-process filesystem/Git-layout inspection: no Git process, config, alias, hook, filter, pager,
or repository code may run. Admission captures outside the database transaction, persists the
actual observation, and records its real frontier, exclusions, Git facts, and counts in the
checkpoint. Rebind, resume, and writer operations freshly re-observe. Ordinary content/index drift
is persisted as invalidation; identity/topology and branch/HEAD drift reject silent resume. The
checkpoint is not a
content snapshot, diff authority, or restorable mutation checkpoint.

Writer expiry and unknown-effect recovery are separate. Acquire, renew, and release have
server-canonical operation digests and durable receipts: equal replays return their first result,
while altered reuse conflicts. Acquire, renew, and release re-observe direct trees, preventing
caller-supplied stale observations from hiding drift. Execution-owned claims are automatically
released in the live terminal/stable-wait/interrupt transaction; journal-open repair is an
idempotent crash fallback. A release never clears
a barrier. An unknown-effect barrier persists
a real uncertain Runtime invocation and server-derived evidence. New rows use `call_resolved`; the
earlier exact tuple `legacy_waiting_approval_reconciliation` plus
`legacy_uncertain_call_resolver` remains supported.
The legacy Runtime recorder owns its own commit, so the barrier remains active until a matching
durable `CALL_RESOLVED` event is present; immediate and journal-open repair close only that exact
tuple. A crash in that compatibility seam retains the barrier, never opening a writer window.
Legacy `WAITING_APPROVAL` remains `legacy_waiting_approval_reconciliation`, never an M4 permission.
Missing or contradictory active-call/journal/event evidence creates one idempotent
`legacy_waiting_approval_incomplete_evidence` fail-closed binding sentinel. It blocks resume and
writer acquisition and has no fabricated permission/text resolver.

## Deferred representation

Instruction discovery and resolved instruction sources, frozen context/model requests and
compaction (M3), permission requests and decisions, changes/diffs/checkpoints/undo and any real
tree mutation (M4), interactive rendering (M5), default Memory/history retrieval (M6), and
cleanup/retirement (M7) have no M2 implementation representation beyond explicit placeholders or
exclusions above.
