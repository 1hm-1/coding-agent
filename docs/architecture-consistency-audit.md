# Cross-ADR Architecture Consistency Audit

Date: 2026-09-20
Status: **Architecture frozen; Product-Layer M0 Accepted and complete; M1 ACTIVE; M2–M7 inactive; this publication turn has not implemented target behavior**

## Purpose

This audit compares the accepted Coding Agent product ADRs with current source behavior, current
architecture documents, historical milestone evidence, and superseded product plans. Its purpose
is to prevent the implementation roadmap from preserving two conflicting target semantics.

The audit does not treat every difference between current code and target architecture as a defect.
Current one-shot Session behavior, isolated workspace copies, and Runtime IPC v1 are implemented
compatibility facts. They become migration inputs. A conflict exists when an older document or
future-facing statement still claims that one of those facts is the target product model.

## Authoritative decision set

The target product authority is, in dependency order:

1. [`project-repository-workspace-conversation.md`](./decisions/project-repository-workspace-conversation.md);
2. [`conversation-turn-runtime-execution-lifecycle.md`](./decisions/conversation-turn-runtime-execution-lifecycle.md);
3. [`tool-capability-command-permission.md`](./decisions/tool-capability-command-permission.md);
4. [`diff-code-checkpoint-undo.md`](./decisions/diff-code-checkpoint-undo.md);
5. [`project-instructions-repository-rules.md`](./decisions/project-instructions-repository-rules.md);
6. [`context-composition-compaction.md`](./decisions/context-composition-compaction.md);
7. [`memory-product-positioning.md`](./decisions/memory-product-positioning.md); and
8. [`interactive-cli-product-workflow.md`](./decisions/interactive-cli-product-workflow.md).

[`target-architecture-snapshot.md`](./target-architecture-snapshot.md) is a concise projection of
those ADRs. It does not override them. [`coding-agent-v1-implementation-roadmap.md`](./coding-agent-v1-implementation-roadmap.md)
orders implementation work and likewise cannot change their product decisions.

## Audit classifications

- **Current legacy fact:** implemented and tested behavior that remains valid until its migration
  milestone. It is not target product authority.
- **Compatible retained asset:** current behavior already consistent with the target and intended
  for reuse.
- **Superseded target assumption:** an older design or roadmap statement that conflicts with an
  accepted ADR. It must not guide new implementation.
- **Deferred representation:** product semantics are closed while schema, class, enum, UI, or
  storage representation remains an implementation decision.

## Evidence inspected

The source audit covered the current domain/FSM in `src/coding_agent/domain.py`, application and
resume flows in `src/coding_agent/application.py`, model/tool recovery in
`src/coding_agent/runtime.py`, SQLite request and execution records in
`src/coding_agent/persistence.py` and `src/coding_agent/migrations.py`, copied workspace behavior
in `src/coding_agent/workspace.py`, ToolHarness and sandbox boundaries under
`src/coding_agent/tools/` and `src/coding_agent/sandbox/`, and current command routing in
`src/coding_agent/cli.py`. The documentation audit covered the current-state, Runtime architecture,
Phase 2 architecture and implementation records, Runtime IPC v1, the old CR-R1/P2-R1 proposals,
roadmap, handoff, and release evidence.

This is a semantic audit. Physical names such as `Session`, `WAITING_APPROVAL`, and
`runtime_session_id` are not themselves defects while compatibility remains active; their current
meaning and any future-facing claims determine whether they conflict with the accepted model.

## Conflict matrix

| Area | Current or older assumption | Authoritative decision | Disposition |
|---|---|---|---|
| Legacy `Session` | `Session` owns task, Runtime FSM, messages, workspace, retry, result, and persistence identity; `run_task()` creates one per request | Lifecycle and interactive workflow ADRs: Conversation is the long-lived transcript; Turn is one user request; old Session is reinterpreted as RuntimeExecution | **Current legacy fact.** Preserve the kernel and compatibility APIs during migration. Supersede every claim that Session is the future product container. |
| Workspace ownership | `PREPARING_WORKSPACE` copies source into a per-Session workspace; old CR-R1 proposed a Conversation-owned isolated copy | Workspace ADR: local interactive default binds the user's current working tree; managed worktree is explicit isolation; WorkspaceBinding owns concrete location, not Conversation or execution | **Current legacy fact plus superseded target assumption.** Keep copy mode for eval/headless/compatibility, but do not use it as the default interactive target. |
| Writer lifetime | Early design treated one write-capable RuntimeExecution as holding writer authority for its lifetime | Interactive workflow ADR: authority is acquired and revalidated around write paths and may release at safe waits; unresolved effects retain a separate recovery barrier | **Superseded.** Execution lease, workspace writer authority, and recovery barrier are different capabilities. |
| Startup creation | One-shot `run` creates Session and initial message together; older plans discussed explicit create-Conversation APIs as the startup path | Interactive workflow ADR: plain `agent` creates only an ephemeral draft; first accepted InitialRequest publishes Conversation, Turn, RuntimeExecution, checkpoint, manifest, binding, and policy epoch | **Superseded for interactive startup.** One-shot compatibility may still create its synthetic product wrapper only when a task is submitted. |
| Permission and sandbox | Current `RunPolicy.allowed_permissions` is a static allow set and Harness check; OS sandbox determines executable/resource isolation | Tool/permission ADR: policy returns UNAVAILABLE/ALLOW/ASK/DENY; permission answers authorization, sandbox answers enforceability; ToolHarness revalidates both | **Current incomplete implementation.** Never treat consent as a sandbox override or sandbox availability as user authorization. |
| Natural-language approval | Current product has no ordinary interactive permission flow; `WAITING_APPROVAL` is used for uncertain-effect resolution and can be mistaken for user permission | Lifecycle and tool ADRs: PermissionDecision is structured and digest-bound; ordinary text in WAITING_PERMISSION is SteeringInput; reconciliation has a different resolver | **Name/semantic conflict.** Reinterpret or migrate `WAITING_APPROVAL` as legacy reconciliation. Natural-language `yes` never authorizes an invocation. |
| Resume semantics | `resume_session()` directly resumes a non-terminal old Session; CLI `resume` means execution recovery | Lifecycle and interactive workflow ADRs: product `/resume` means resume Conversation, then explicitly choose resume execution, stop/cancel, or inspect only | **Current compatibility command.** Preserve an explicit execution-resume use case during migration, but supersede its use as product `/resume`. |
| Host loss and cancellation | Current SIGINT records interrupt intent; Runtime IPC v1 distinguishes requested cancellation from signal/process failure | Lifecycle and interactive workflow ADRs: explicit Esc/Ctrl+C/`/stop` is cancel request; terminal or process loss is recovery/host loss unless cancel intent was durably committed | **Compatible retained asset.** Do not collapse process exit, SIGHUP, expired lease, or missing terminal result into user `CANCELLED`. |
| Undo | Current code has no user code-undo operation; generic Git commands could otherwise be mistaken for rewind | Diff/undo ADR: `/undo` creates a durable CodeRewindOperation, uses exact revision preconditions and ToolHarness, and never performs implicit `git reset`, index rollback, or history rewrite | **No current implementation conflict; future guard.** Any raw Git-reset implementation of `/undo` is prohibited. |
| Project instructions | No current InstructionManifest subsystem; repository prose could otherwise be confused with trusted policy | Project-instructions ADR: repository instructions are soft model context after trust activation and cannot grant permission, capability, sandbox access, writer authority, or containment exceptions | **No current implementation conflict; authority frozen.** Instruction trust is not security authorization. |
| Summary authority | Current SummaryRecord has event lineage and stale/supersede semantics; old Session-oriented plans attached summary to execution history | Context ADR: Conversation transcript remains append-only authority; SummaryArtifact is a range-addressed derived cache with source references and staleness | **Current mechanism mostly compatible; ownership target refined.** Reuse lineage/validation, migrate product scope, and never replace transcript authority. |
| Uncertain provider retry | SQLite already persists normalized model `request_json`, but Runtime resume rebuilds `_model_input`; an uncertain retry can construct and overwrite a different request under the same request ID | Context ADR: retry of a durably committed request with unknown outcome must reuse the exact Frozen ModelRequest and create a new attempt record | **Concrete correctness conflict.** M3 must replay the stored request, preserve attempt history, and never reconstruct that same request from current workspace/context. |
| Memory default serving | Earlier P2-R1 and roadmap text proposed default Core Snapshot and broader automatic serving; current ContextBuilder only retrieves when explicitly injected and default Application/headless are memory-off | Memory ADR: M2-lite; explicit UserPreference only for V1; Core Snapshot and generic auto top-k OFF; Memory optional and lower authority | **Earlier product assumption superseded; current default compatible.** Existing governance remains dormant and reusable. |
| Direct-tree delivery order | A path-only M2 implementation could expose real-tree writes before instruction, permission, diff, and rewind protection exists | Workspace target remains direct-tree, but the implementation roadmap gates every real-tree product mutation until both M3 and M4 pass | **Delivery clarification, not an ADR change.** M2 may bind and read; placeholder manifests/checkpoints do not establish protection. Isolated fixtures remain fully subject to Harness and safety semantics. |
| Command/product promise | Current `python_project` and trusted profiles can be misread as either the whole future command universe or already-complete development support | Tool ADR keeps profiles as envelopes and permits a future explicit shell form; V1 matrix narrows delivery to structured, enforceable Python workflows and makes shell form unavailable | **Scope projection, not a domain conflict.** The ADR future capability remains possible; V1 does not promise it. |
| Interactive responsiveness | Current Runtime/backend calls are synchronous and the ADR leaves attachment implementation details deferred | V1 roadmap requires a responsive attachment and durable received/applied input distinction even without token streaming | **Acceptance refinement.** It adds no entity or FSM decision and preserves durable steering/finalization ordering. |

## Additional compatibility findings

### Runtime kernel and persistence

The following are compatible retained assets and must not be rewritten solely to fit Product-layer
names:

- deterministic `RuntimeState` transition table and handler loop;
- SQLite as recovery authority;
- atomic state/snapshot/event/message/model-call/tool-call mutations;
- optimistic session version and execution lease;
- normalized model request and response journal;
- tool intent/result/recovery journal;
- checkpoint and reconciliation behavior;
- ToolHarness, WorkspaceGuard, sandbox executor, trusted profiles, and structured argv;
- provider adapters and error normalization;
- Context section attribution, Summary lineage, and required-content overflow failure;
- JSONL projection/export and semantic replay;
- Eval harness and four semantic goldens; and
- Runtime IPC v1 producer contract.

The physical word `session` may remain in legacy tables, JSON fields, tests, and Runtime IPC v1
during compatibility. It must be interpreted as an execution-scoped legacy identifier unless the
specific historical document is describing the old product.

### Runtime IPC v1 workspace behavior

Runtime IPC v1 requires a read-only source and an isolated copied workspace. That remains the
authoritative behavior of the existing headless protocol version. It does not define the new local
interactive default and must not be silently changed in place. A future protocol version or
explicit product composition can expose a different WorkspaceBinding contract after compatibility
review.

### Historical documents

The M1/M2/M3/M4 implementation plans, v0.1 release notes, test fixtures, and current-state sections
that describe per-session copies or Session persistence remain accurate historical/current
evidence. They are not rewritten to pretend the target architecture was already implemented.

The following former target documents now carry explicit supersession boundaries:

- [`conversation-runtime-refactor-plan.md`](./conversation-runtime-refactor-plan.md): superseded as
  an implementation plan because it gives Conversation an isolated workspace and predates the
  accepted instruction, context, permission, undo, Memory, and CLI decisions;
- [`v2-product-architecture.md`](./v2-product-architecture.md): implemented P2-M1 IPC and P2-M2
  governance evidence remains valid, while LocalSessionController, coordinator-first product
  spine, terminal-last ordering, and old roadmap are superseded for the V1 Coding Agent path;
- [`p2-r1-governed-agent-memory-redesign.md`](./p2-r1-governed-agent-memory-redesign.md): governance
  background remains useful, while default Core Snapshot and automatic serving assumptions are
  superseded by the M2-lite ADR; and
- the older Phase 2 rows in [`roadmap.md`](./roadmap.md): historical status remains, while future
  implementation order is replaced by the new M0–M7 roadmap.

## Cross-ADR consistency conclusions

1. Conversation, Turn, RuntimeExecution, and ModelRequest identities have no unresolved ownership
   conflict.
2. RepositoryIdentity, ProjectScope, WorkspaceBinding, and explicit rebind remain compatible with
   direct working-tree operation and isolated compatibility modes.
3. Turn Admission Bundle can publish product identities atomically while filesystem observations
   remain revision-bound rather than falsely transaction-atomic.
4. Writer authority can safely release independently of RuntimeExecution identity because every
   later mutation revalidates, while recovery barrier preserves unknown-effect exclusion.
5. Permission, sandbox, instructions, and ToolHarness have distinct authority planes.
6. Conversation transcript, Runtime audit journal, filesystem/Git, InstructionManifest,
   ToolResultArtifact, SummaryArtifact, ContextManifest, and Frozen ModelRequest each retain one
   non-overlapping authority role.
7. Optional Memory has no dependency edge into the V1 critical path.
8. No accepted ADR requires a persistent Task, a second Agent loop, or a rewrite of the reliable
   RuntimeExecution kernel.
9. The M3+M4 real-tree mutation rollout gate is compatible with the direct-working-tree target: it
   sequences delivery rather than changing WorkspaceBinding ownership.
10. The frozen V1 capability matrix narrows release acceptance without deleting the command ADR's
    future explicit shell form or redefining permission precedence.

## Remaining non-blocking implementation decisions

- physical RepositoryIdentity storage and discovery registry;
- table names, migration numbers, compatibility views, and event serialization;
- exact writer-contention wait representation and safe-release timing;
- CLI toolkit, keybindings, literal slash escape, and rendering details;
- artifact retention, encryption, redaction, and garbage collection;
- platform-specific signal behavior; and
- exact search/index implementation for Conversation history.

These are implementation or persistence decisions governed by the ADR invariants. None requires
reopening the product domain model before M0 characterization or M1 domain/persistence work.

## Roadmap acceptance audit

This documentation acceptance pass leaves all eight ADR texts and their Accepted status unchanged.
It found no product-domain contradiction: the capability matrix, M3+M4 real-tree mutation rollout
gate, measurement contract, and interactive responsiveness targets narrow delivery and evidence
acceptance without changing the accepted identities, authorities, lifecycle, or permission
precedence. The patch activates neither M0 nor any code/schema/default change and makes no claim
that future behavior tests have already passed.

That final sentence records the acceptance patch's historical state when it was written. The later
explicit authorization activates only Product-Layer M0 architecture freeze and characterization;
M1–M7 and target product implementation remain inactive. This status update changes no ADR text or
Accepted status and does not claim that any M0 behavior test, fixture, baseline, or evaluation has
already run.

That activation paragraph is also historical. Product-Layer M0 was accepted and completed on
2026-09-21; M1–M7 remain inactive, and no product implementation milestone is active until a new
formal execution contract is issued and activated. All eight Accepted ADR texts remain unchanged.

**Later activation annotation (2026-09-22):** the formal
[`M1 execution contract`](./execution-contracts/m1-execution-contract.md) has now been issued and
M1 is ACTIVE. The preceding paragraph records the status at the end of M0 and is not rewritten as
though M1 had already been active. This contract-publication turn implements no M1 behavior;
current code and Schema v4 remain the legacy baseline, M2–M7 remain inactive, and all eight
Accepted ADR texts remain unchanged.
