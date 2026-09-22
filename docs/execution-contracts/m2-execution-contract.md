# Product-Layer M2 Execution Contract — Workspace, Lifecycle, and Turn Admission

Date issued: 2026-09-22
Date accepted: 2026-09-23
Status: **ACCEPTED / COMPLETE**
Milestone owner: Sol High planning/review owner
Executor: Terra High
Closure: This document governed **Product-Layer M2 only**. The owner accepted M2 as complete on
2026-09-23. M1 remains Accepted and complete. M3–M7 remain inactive; this closure does not issue or
activate M3.

**Publication-turn stop boundary:** this issuance turn publishes the contract and synchronizes
status/navigation documentation only. It authorizes no M2 production code, schema, migration,
fixture, test, or behavior change in this turn. M2 execution starts only in a later, separately
started Terra High turn working against this contract.

## 1. Baseline, authority, and objective

The mandatory implementation baseline is clean commit
`4fb03ccb9448fdec3d7031531bff24a59f329263`, where Product-Layer M1 is Accepted/complete, SQLite
Schema v5 is authoritative, and the legacy Runtime compatibility boundary remains intact. If the
execution worktree does not start from that content plus this contract-publication patch, stop and
request owner direction before editing implementation code.

Authority, in precedence order:

1. the eight Accepted product ADRs indexed by
   [`../architecture-consistency-audit.md`](../architecture-consistency-audit.md), especially the
   Repository/Workspace, Conversation lifecycle, diff/checkpoint, project-instruction, and
   interactive-workflow decisions;
2. [`../target-architecture-snapshot.md`](../target-architecture-snapshot.md);
3. the M2 scope and exit criteria in
   [`../coding-agent-v1-implementation-roadmap.md`](../coding-agent-v1-implementation-roadmap.md);
4. the frozen real-tree rollout gate in
   [`../v1-development-capability-matrix.md`](../v1-development-capability-matrix.md);
5. the completed [`m1-execution-contract.md`](./m1-execution-contract.md),
   [`../m1-product-persistence-design.md`](../m1-product-persistence-design.md), and
   [`../evidence/m1-implementation-verification-2026-09-22.md`](../evidence/m1-implementation-verification-2026-09-22.md);
   and
6. the existing Runtime IPC producer authority in
   [`../protocol/runtime-ipc-v1.md`](../protocol/runtime-ipc-v1.md),
   [`../protocol/compatibility.md`](../protocol/compatibility.md), and `protocol/v1/*.schema.json`.

M2's sole objective is to add the Product application/lifecycle boundary around the retained
RuntimeExecution kernel: direct WorkspaceBinding discovery, durable multi-Turn Conversation
continuity, atomic Turn Admission, typed input/control semantics, explicit between-Turn rebind,
cooperative writer coordination, and a durable recovery barrier. M2 must leave every product path
that targets a real user working tree strictly read-only.

## 2. Closed implementation scope

M2 may implement only the following.

1. A thin, attachment-neutral `ConversationApplicationCoordinator` and narrow use-case handlers.
   The coordinator may load authorities, establish application transaction boundaries, dispatch
   use cases, and translate outcomes. It owns no durable or in-memory aggregate truth, writer
   claim, Runtime transition, tool execution, prompt, filesystem state, or policy decision.
2. Read-only startup/application discovery ports for RepositoryIdentity, ProjectScope, the current
   direct WorkspaceBinding, trust observations, active coordinated writer claims, unfinished
   RuntimeExecutions, and recovery barriers. Startup discovery creates no Conversation or Turn and
   writes no file, directory, cache, marker, lock, index, or other artifact inside the checkout.
3. Real Product Conversation and Turn lifecycle over the M1 identity spine: at most one open Turn
   per Conversation; exactly one RuntimeExecution per Turn; immutable execution WorkspaceBinding;
   Turn status projected from RuntimeExecution; terminal Turns leave their Conversation open.
4. Typed, durably ordered `InitialRequest`, `SteeringInput`, `UserReply`, and cancel
   `ControlRequest` semantics. A `UserReply` must correlate to one durable outstanding ordinary
   input request. Permission and reconciliation decisions remain separate typed resolver domains;
   ordinary text never resolves them.
5. One logical Turn Admission publication transaction, defined precisely in section 3.
6. Distinct `CANCELLED`, `WAITING_USER_INPUT`, `WAITING_PERMISSION`,
   `WAITING_RECONCILIATION`, and `INTERRUPTED` meanings in Runtime/lifecycle representation.
   M2 may add the minimum FSM states or typed wait reasons needed for those semantics, but M4 still
   owns PermissionPolicy, PermissionRequest/Decision behavior, capability claims, and tool
   authorization. Existing `WAITING_APPROVAL` rows remain readable only as legacy uncertain-effect
   reconciliation; they must never be reinterpreted as ordinary permission prompts.
7. `resume_conversation`, inspect-only, and `resume_execution` as distinct internal/application
   use cases. Conversation resume never starts an execution, dispatches a tool, substitutes the
   current cwd, or resolves an unknown effect.
8. Explicit, audited Conversation rebind between Turns only. Rebind validates RepositoryIdentity
   and ProjectScope compatibility, changes the Conversation default for future Turns, and never
   moves an existing RuntimeExecution, checkpoint, pending call, or recovery barrier.
9. Cooperative WorkspaceBinding writer-claim acquisition, renewal/revalidation, safe release, and
   contention results, plus a separate durable recovery barrier tied to the binding and uncertain
   invocation. Read-only and Plan work does not acquire writer authority merely by existing.
   Writer-claim expiry never clears a recovery barrier.
10. A direct-working-tree Product composition usable through internal/application APIs and tests
    only. Its executable tool ceiling is read-only: any edit/create/delete, command/test/build,
    cache, Git write or optional index refresh, hook/helper, startup artifact, indirect effect, or
    rewind request fails before side-effect dispatch. Bounded reads still pass through
    ToolHarness and WorkspaceGuard. The existing one-shot, eval, headless, and IPC v1 compositions
    continue using copied workspaces.
11. Additive persistence, migration, compatibility adapters, tests, design/evidence records, and
    status documentation needed to prove this closed scope.

M2 does not deliver the interactive CLI merely because these application use cases exist.

## 3. Turn Admission Bundle contract

### 3.1 Publication contents

One admission transaction must publish all of the following, or none of them:

- a Conversation for the first request from an ephemeral draft, or the expected existing idle
  Conversation for a later Turn;
- the `InitialRequest` semantic event and its caller-supplied idempotency identity;
- the next ordered Turn;
- exactly one RuntimeExecution mapping and its existing `sessions` physical Runtime record,
  initial Runtime checkpoint/message/events, without a second FSM record;
- one immutable direct or explicit compatibility WorkspaceBinding reference for the execution;
- a `TurnStartCodeCheckpoint` identity with observation frontier, explicit M2 coverage state, and
  baseline references;
- a base `InstructionManifest` identity explicitly marked as an M2 placeholder with no claim of
  instruction discovery, source content, path coverage, or M3 protection; and
- the effective M2 read-only mode/policy epoch identity used to keep the real-tree gate closed.

The TurnStartCodeCheckpoint is an observation/coverage shell only. It may record bounded hashes,
workspace/Git observations, exclusions, and baseline references, but M2 must not retain M4
before/after mutation authority, implement diff authorship, enable rewind, or claim restorable
coverage. The base InstructionManifest is an identity/snapshot boundary only; it must expose an
explicit `m3_protection_unimplemented` or equivalent disposition and must not discover, interpret,
or freeze repository instruction content.

The `sessions` row remains the physical Runtime FSM authority. Conversation and Turn acquire no
state column or duplicate FSM. Product semantic events remain distinct from Runtime/tool audit
events; a Runtime message may be a projection for the retained kernel, not a second Conversation
transcript authority.

### 3.2 Atomicity and idempotency

- Publication occurs in one SQLite `BEGIN IMMEDIATE` transaction using the existing journal's
  connection and transaction primitives. No external model, tool, subprocess, or filesystem write
  runs inside that transaction.
- A caller-supplied admission operation ID and canonical payload digest are unique authority. A
  retry with the same ID and digest returns the same published identities without allocating a
  second Turn or RuntimeExecution. Reuse with a different digest fails as a conflict.
- For a new draft, any failure before commit leaves no visible Conversation, InitialRequest, Turn,
  RuntimeExecution, Session, checkpoint, manifest, or policy epoch. For an existing Conversation,
  the prior Conversation remains unchanged and no new Turn aggregate becomes visible.
- The one-open-Turn check, next ordinal/semantic sequence allocation, expected Conversation
  version, and publication inserts are serialized in that same transaction. Competing admissions
  yield one success and one typed stale/conflict result, never two open Turns.
- Checkpoint/workspace observations are captured read-only before the transaction and carry their
  observation frontier. Filesystem/Git cannot join the SQLite transaction; drift between capture
  and publication is concurrent or unattributed drift, never Agent-controlled change.
- No product code side effect may occur before the checkpoint shell is committed. At M2 the real
  user tree has no product mutation path at all.
- Context building and provider initialization follow admission. A failure there produces an
  accepted Turn whose RuntimeExecution is durably `FAILED`; it does not roll back the accepted
  Turn or leave a Turn without an execution.

### 3.3 Ordered inputs and finalization

- Every accepted semantic input/control operation has an idempotency ID, payload digest, durable
  Conversation sequence, expected-version check, and exactly-once acknowledgement after commit.
- `SteeringInput` committed before the terminal finalization event belongs to the open Turn and is
  consumed at a later Runtime safe boundary in sequence order. It never mutates or cancels an
  already committed ToolInvocation.
- A matching `UserReply` resolves only its identified outstanding ordinary-input request. An
  unsolicited input is SteeringInput even while that request exists unless explicitly correlated.
- Runtime terminal transition and the idempotent product `turn_finalized` semantic event must
  share one SQLite transaction where the M2 path controls both. If recovery sees a terminal legacy
  Runtime record without that event, it may repair only the missing projection idempotently; it
  must not rewrite the terminal outcome.
- The input/finalization race is decided by serialized durable order. Ordinary text accepted after
  finalization is a candidate InitialRequest for the next Turn and cannot be appended retroactively.

## 4. Workspace, writer, and recovery invariants

1. Repository descriptors remain evidence. Canonical path or Git common-dir may rediscover an M1
   identity; remote/history equality never merges independent clones. Non-Git directories remain
   supported. M2 writes no repository marker inside a checkout.
2. A direct WorkspaceBinding is idempotently rediscovered from an explicitly supplied concrete
   directory and remains distinct from RepositoryIdentity. Missing or incompatible bindings fail
   closed; cwd substitution and automatic rebind are forbidden.
3. Git/workspace discovery must be demonstrably observation-only: no optional index refresh,
   locks, hooks, pager, external diff/textconv, aliases, helper execution, repository config write,
   or process that executes repository code. If the host cannot enforce the read-only probe, the
   fact is `unavailable` rather than guessed.
4. A RuntimeExecution's WorkspaceBinding cannot change. Conversation rebind affects only a later
   Turn and invalidates prior workspace-derived facts for future use.
5. Writer authority is a cooperative, versioned lease/claim on one WorkspaceBinding and is
   separate from the execution lease. A claimant must provide binding, owner, operation, epoch,
   expected observations, and bounded expiry; acquisition/reacquisition validates compatibility,
   drift inputs, and absence of an active recovery barrier.
6. M2 chooses conservative safe release: any writer claim used by M2-owned test operations is
   durably released on terminal outcome and stable `INTERRUPTED`; stable/detached
   `WAITING_USER_INPUT` or `WAITING_PERMISSION` also releases it. Re-entry into a write path would
   require reacquisition and drift revalidation, although real-tree writes remain disabled in M2.
7. An unknown-effect invocation creates or retains a binding-scoped recovery barrier before
   another coordinated writer can acquire authority. Process death, execution-lease expiry, and
   writer-claim expiry do not clear it. Only matching durable reconciliation evidence may resolve
   it.
8. For legacy `WAITING_APPROVAL`, an idempotent compatibility projection/backfill labels the wait
   and any barrier as legacy reconciliation. It preserves the stored legacy state and resolver.
   Missing or contradictory call evidence fails closed rather than fabricating a permission wait.
9. Where the same SQLite authority owns the Runtime resolution and barrier, resolving an uncertain
   call closes or retains the barrier in the same transaction. Any unavoidable compatibility seam
   must have an idempotent restart repair proving that a crash cannot admit a writer while the
   effect is still unknown.
10. Real-tree read-only Product executions do not acquire writer authority and must tolerate
    ordinary content drift by invalidating observations. Branch/HEAD or identity/topology drift
    prevents silent execution resume; it does not automatically destroy Conversation history.

## 5. Additive Schema v6 and migration strategy

M2 uses exactly one next additive SQLite migration: **Schema v6**. It may add tables, columns,
indexes, triggers, and private repository methods required by this contract. It must not rename,
drop, reinterpret, or destructively rewrite a v1–v5 table or payload.

The v6 representation must durably cover, whether in normalized tables or explicitly versioned
records:

- operation/idempotency identity and canonical payload digest;
- typed Product input/control events and their Conversation order/correlation;
- admission publication and policy epoch;
- checkpoint shell identity, observation frontier, baseline references, exclusions, and explicit
  non-restorable M2 coverage;
- placeholder InstructionManifest identity and explicit M3-unimplemented disposition;
- WorkspaceBinding discovery/observation and audited rebind;
- outstanding ordinary-input request correlation;
- workspace writer claims/epochs and safe release;
- recovery barriers and reconciliation linkage; and
- any product-finalization or repair provenance needed for deterministic recovery.

Physical table and index names are recorded in the required M2 design document, but the semantic
cardinalities above are not optional. `sessions` remains the RuntimeExecution FSM record and
`runtime_executions.legacy_session_id` remains the compatibility linkage; M2 must not introduce a
parallel Runtime state store.

Upgrade rules:

- empty/new and every supported v1–v5 database upgrade transactionally to v6;
- v5 Product mappings and every legacy Runtime, Summary, Memory, lease, call, event, and payload
  retain byte meaning and ordering;
- existing synthetic M1 Conversations/Turns are not backfilled with genuine InitialRequest,
  admission, checkpoint, or instruction semantics that never existed;
- only the explicit legacy reconciliation projection in section 4 may be derived, with truthful
  provenance and idempotent retry;
- failure at every v6 DDL/index/provenance boundary rolls back v6 as a unit and leaves the v5
  database usable by the pre-M2 implementation;
- a committed v6 open/reopen is idempotent; unknown future versions reject before M2 work; and
- there is no destructive downgrade. Rollback is application rollback against an untouched v5
  copy or forward repair of an additive v6 database.

SQLite remains the only recovery authority. JSONL remains a rebuildable Runtime audit export and
must not become a Product transcript, admission, writer, or recovery-barrier store.

## 6. Compatibility requirements

- Preserve legacy `AgentApplication.run_task()`, `resume_session()`, `interrupt`, `resolve-call`,
  session inspection, JSONL export/replay, eval/oracle, and CLI behavior. Their copied-workspace
  composition remains the default for all existing entry points.
- Preserve Runtime IPC v1 request, capability, event, result, ordering, cancellation, exit-code,
  schema digest, and read-only-source/copied-workspace semantics. M2 Product IDs and private v6
  schemas do not enter IPC v1; no new capability is advertised; `v0.1.0` still does not implement
  IPC v1.
- Preserve legacy Session and RuntimeSnapshot serialization, supported versioned and unversioned
  payloads, Runtime event meaning/order, model/tool call journals, Summary and Memory rows, and
  execution lease semantics.
- Preserve the deterministic Runtime kernel and one Agent loop. State transitions remain in the
  FSM; the coordinator cannot call a state handler, execute a tool, or update state directly.
- Preserve provider-specific wire isolation inside adapters and all real side effects behind
  ToolHarness.
- Preserve the four protected semantic goldens unchanged:
  `bugfix_success.json`, `test_failure_recovery.json`, `permission_denied.json`, and
  `runtime_failure.json`.
- Preserve `tests.test_v1_m0_uncertain_retry`'s single M3-owned expected failure exactly as an
  expected failure. M2 must not freeze ModelRequest/ContextManifest, rewrite retry request
  persistence, repair the defect, mark it skipped, alter its assertion, or normalize it away.
- `COMPLETED` remains Runtime completion, not task success. Usage and timing data retain the M0
  measured/estimated/unknown classifications and all failure populations.

## 7. Explicitly prohibited M3–M7 and unrelated work

The following are out of scope, including as scaffolding, defaults, caches, compatibility helpers,
or opportunistic cleanup.

- **M3:** InstructionSource discovery/precedence/trust activation, real instruction snapshots,
  path-aware instruction coverage, ContextComposer redesign, ContextManifest, Frozen
  ModelRequest, Summary ownership migration, token/compaction changes, or uncertain-provider-retry
  repair.
- **M4:** CapabilityClaims, PermissionPolicy, PermissionRequest/Decision execution flow,
  ALLOW/ASK/DENY/UNAVAILABLE implementation, command expansion, filesystem mutation,
  Turn/Current/Agent-controlled diff, controlled change evidence, CodeRewindOperation, or a
  general Shell. Existing trusted-profile/structured-argv behavior remains legacy-only.
- **M5:** interactive CLI attachment, local CommandRouter, `/new`, product `/resume`, `/status`,
  `/diff`, `/undo`, `/stop`, `/mode`, `/permissions`, `/exit`, `/help`, responsiveness UX,
  background host, or daemon.
- **M6:** UserPreference UX, Memory identity migration, automatic extraction, default Memory,
  Core Snapshot, Dynamic Recall, History Search, embedding/vector/RAG, or any Memory dependency in
  admission/context/resume.
- **M7:** destructive legacy removal/rename, compatibility cleanup, support-matrix/release closure,
  or end-to-end release claims.
- Any real-tree mutation through file tools, commands/caches, Git, startup/admission/checkpoint
  artifacts, hooks/helpers, indirect processes, or rewind. A path switch, placeholder manifest,
  checkpoint shell, writer claim, user approval, or isolated-fixture test cannot open this gate.
- New dependencies or lockfile changes unless the owner records a material M2 amendment proving
  they are indispensable. Skills, MCP, multi-Agent, UI frameworks, and Platform consumer work
  remain deferred. Do not modify accepted ADR text, historical evidence merely to pass, protected
  goldens, or the sibling `../hermes-agent` repository.

## 8. Implementation sequence and file ownership

Terra High executes in the following order and stops at each failed gate rather than building
later layers on an unverified one.

1. **M2 design freeze:** create `docs/m2-product-lifecycle-design.md` recording the v6 physical
   schema, state/wait representation, admission command, event taxonomy, discovery facts,
   idempotency keys/digests, writer lease parameters, recovery-barrier transitions, and every
   deferred M3–M7 item. This document may choose representation; it may not alter this scope.
2. **Domain and migration:** extend Product value objects/ports, add v6, and prove the full
   migration/fault matrix before application behavior.
3. **Discovery and admission:** implement observation-only direct binding plus the atomic admission
   command and read-only Product composition. Prove zero pre-publication visibility and zero
   real-tree mutation before adding later lifecycle use cases.
4. **Lifecycle and ordering:** add steering/reply/cancel/finalization, wait/cancel representation,
   separate Conversation/execution resume, and between-Turn rebind with race/recovery tests.
5. **Writer and recovery coordination:** add writer claim/safe release, legacy reconciliation
   projection, recovery barrier, lease-expiry/host-loss and restart repair tests. Do not add a
   mutation consumer.
6. **Compatibility and evidence closure:** run every gate in section 10, write the verification
   report, and update status docs. M2 remains active until owner acceptance; Terra must not mark it
   Accepted/complete independently.

Expected ownership:

- `product_domain.py`: M2 Product value objects, typed inputs/control, checkpoint/manifest shells,
  and invariants; no Runtime FSM or SQL.
- `product_persistence.py`: narrow M2 repository/transaction port and compatibility adapters; no
  second authority or public IPC.
- `migrations.py` and `persistence.py`: v6 and its SQLite implementation/atomic commands; no
  lifecycle policy hidden in SQL helpers.
- a focused Product application/coordinator module may be added; it must remain stateless and must
  not turn `application.py` into a Conversation/Runtime/workspace God object.
- `workspace.py` or a focused Product workspace module: direct-binding discovery, observation-only
  facts, compatibility/rebind validation, writer/recovery coordination; retain
  `WorkspaceManager.create()` unchanged for legacy copies.
- `domain.py` and `runtime.py`: only the minimum M2 state/wait/cancel and pre-admitted-execution
  seams. Do not change model input, provider retry, tool capability, permission, diff, or undo.
- `application.py`: compatibility wiring only where unavoidable; legacy public behavior remains
  unchanged.
- `cli.py`, `protocol/`, `memory/`, model adapters, Context/compression, tool schemas/handlers,
  command/test profiles, and sandbox implementation are not M2 feature owners and should remain
  behaviorally unchanged.
- focused M2 tests belong in a new Product-layer test module plus migration/persistence tests;
  protected legacy/golden tests are regression evidence, not rewrite targets.

## 9. Acceptance and adversarial test matrix

| Acceptance area | Required proof |
|---|---|
| Atomic admission | new and existing Conversation success; injected failure before/after every publication component and pre-commit seam; no half aggregate; post-admission initialization failure leaves one accepted failed execution |
| Admission idempotency/concurrency | same key+digest returns identical IDs; changed payload conflicts; two simultaneous admissions produce one open Turn; ordinal and semantic sequences remain gap-free and unique |
| Product continuity | two consecutive terminal Turns share one Conversation, ordered transcript, RepositoryIdentity/ProjectScope and current default binding; each owns one distinct RuntimeExecution; no claim that M3 composed prior history into a Frozen ModelRequest |
| Typed input and race | multi-steering order, exactly-once consumption at safe boundaries, correlated UserReply, unsolicited-input distinction, finalization race, duplicate delivery, crash/reopen, and cancel-intent versus terminal-cancel separation |
| Workspace discovery | dirty tracked, staged, untracked, non-Git, monorepo scope, linked worktree, independent clone, moved/missing/incompatible workspace, symlink/containment, branch/HEAD and IDE drift; descriptors never merge clones |
| Checkpoint/manifest shells | stable identities and truthful observation/coverage; dirty/staged/untracked facts preserved as observations; explicit non-restorable/M3-unimplemented labels; no content snapshots, diff authorship, instruction interpretation, or mutation claim |
| Rebind/resume | explicit compatible rebind between Turns with audit; active-Turn rejection; missing workspace inspect-only; no cwd fallback; Conversation resume performs no execution; execution resume retains Turn and immutable binding |
| Writer coordination | one active writer per binding, independent bindings, read-only concurrency without a claim, stale owner/epoch rejection, safe release, reacquisition drift failure, and execution-lease independence |
| Recovery barrier | unknown effect blocks writer after process/writer lease expiry; exact reconciliation clears it; mismatched resolver fails; crash at each barrier/resolution seam repairs safely; legacy `WAITING_APPROVAL` remains reconciliation |
| Real-tree closed gate | snapshot/fingerprint and sentinel tests cover edit/create/delete, command/test/build/cache, Git index/ref/lock, hooks/helpers, startup/admission artifacts, indirect child effects, and undo across success, failure, crash, restart, and rollback; zero mutation is required |
| Isolated fixtures | any mutation-oriented negative fixture uses disposable test-owned trees and still routes through the real Harness/Guard/policy/sandbox/journal/recovery boundaries; no destructive test targets user data |
| Migration/recovery | empty/new plus v1–v5 upgrade, future rejection, every v6 statement/provenance failure, reopen/idempotency, legacy terminal/interrupted/retry/uncertain/reconciliation/summary/Memory rows, and v5 usability after rollback |
| Compatibility | legacy one-shot/resume/admin/export/replay/eval, copied workspaces, Runtime IPC v1 schemas/vectors/goldens, Memory-off/default behavior, four semantic goldens, and the one M3 expected failure remain unchanged |
| Coordinator boundary | tests/review prove no durable mutable aggregate, direct FSM update, tool execution, prompt composition, policy decision, writer ownership, or filesystem authority in the coordinator |

Every durable path requires success, expected-failure, crash/restart recovery, and rollback evidence.
Tests may use disposable direct-binding repositories under temporary directories; they must never
write to the repository under development or another user checkout.

## 10. Quality gates and required evidence

Before M2 can be proposed complete, run and report actual results for:

- `PYTHONPATH=src python3 -m unittest discover -v`;
- focused M2 domain, migration, admission, lifecycle, workspace, writer, recovery, and gate-closure
  tests, including the full fault matrix;
- the four protected semantic goldens and explicit assertion that the M3 uncertain-retry test is
  still one expected failure;
- Runtime IPC v1 schema/vector/producer tests and JSONL export/replay compatibility;
- Ruff, configured mypy plus targeted typing for every M2-owned source file, and compileall;
- the configured coverage threshold, with no M2-owned critical transaction/recovery path omitted;
- wheel/sdist build and isolated install/import smoke including the new private Product modules;
- calculator and todo copied-workspace scripted smokes;
- the existing Memory cold/warm regression benchmark, only to prove default Memory wiring did not
  change;
- an isolated direct-binding read-only Product smoke plus a before/after manifest proving no file,
  directory, Git index/ref, cache, or startup artifact changed in the bound tree;
- changed-document local-link/fence audit and `git diff --check`.

Do not run a live Provider without a new M2-specific hypothesis and explicit owner need. Its
absence waives no deterministic gate. Record command lines, pass/fail/skip/expected-failure counts,
coverage, platform/capability limitations, and artifact hashes in
`docs/evidence/m2-implementation-verification-YYYY-MM-DD.md`. Historical M0/M1 counts are not
substitutes for new results.

## 11. Exit checklist and exact stop condition

No item may be checked until its success, expected-failure, crash/recovery, and rollback evidence
passes.

- [x] M2 physical design and v6 representation are documented and ADR-consistent.
- [x] Schema v6 migration/fault/reopen/future-version matrix passes without changing v1–v5 meaning.
- [x] Direct WorkspaceBinding discovery and explicit rebind pass all identity/drift cases without
      writing to a real tree.
- [x] Turn Admission publishes the complete bundle atomically/idempotently and has no half-Turn
      state under injection or concurrency.
- [x] Conversation/Turn/RuntimeExecution continuity, typed input/control ordering, wait semantics,
      cancellation, finalization races, and separate resume use cases pass.
- [x] Writer authority, safe release/reacquisition, recovery barrier, lease independence, legacy
      reconciliation compatibility, and restart repair pass.
- [x] Real-tree Product execution is demonstrably read-only; M3/M4 gates remain closed.
- [x] Legacy one-shot/headless/eval/copied-workspace, IPC v1, JSONL/replay, Memory-off, four goldens,
      and the M3 expected failure pass unchanged.
- [x] Full quality evidence and implementation-status documentation are complete, with no M3–M7
      behavior claimed.

The executor stop condition was satisfied with all checklist evidence green and the implementation
and verification report ready for owner review. The owner accepted M2 on 2026-09-23. M3–M7 remain
inactive; M2 closure does not start M3.

Stop earlier and request owner direction if an Accepted authority conflicts, v6 cannot remain
additive, atomic admission cannot share the single SQLite authority, a supported legacy reader or
IPC v1 behavior would break, the real-tree gate cannot be proven closed, or an acceptance criterion
would require M3–M7. Any material scope amendment must be recorded as a dated amendment to this
contract; never silently broaden it.

## 12. Downstream status

M2 is **ACCEPTED / COMPLETE** under this contract as of 2026-09-23. M1 remains
**ACCEPTED / COMPLETE**. M3, M4, M5, M6 (optional), and M7 remain **inactive**. M2 completion does
not by itself open real-working-tree
mutation, activate an interactive CLI, enable default Memory, change Runtime IPC v1, or authorize
legacy removal.
