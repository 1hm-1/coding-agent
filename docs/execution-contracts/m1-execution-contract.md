# Product-Layer M1 Execution Contract — Domain Spine and Persistence Migration

Date issued: 2026-09-22
Status: **ACTIVE**
Milestone owner: the coordinating/reviewing session
Executor: Terra High
Activation: This document formally activates **Product-Layer M1 only**. It replaces no Accepted
ADR and authorizes no M2–M7 work. Publication of this contract is not evidence that an M1
implementation, schema migration, or target product behavior already exists.

**Publication-turn stop boundary:** this issuance turn is limited to publishing this contract and
synchronizing status/navigation documentation. It deliberately starts no M1 code, schema,
migration, test, fixture, scaffold, or semantic change. A later, separately started execution turn
may work only within this contract; until then the current code and SQLite schema remain the legacy
baseline characterized by M0.

## 1. Authority, objective, and activation boundary

This contract authorizes the first Product-Layer implementation milestone after the accepted M0
architecture freeze and characterization. Its sole objective is to establish the durable domain
spine and an additive, recoverable SQLite migration path while retaining the existing reliable
Runtime kernel and every supported legacy surface.

Authority, in precedence order:

1. the eight Accepted product ADRs, indexed in
   [`../architecture-consistency-audit.md`](../architecture-consistency-audit.md), especially the
   Repository/Workspace/Conversation and Conversation/Turn/RuntimeExecution ADRs;
2. [`../target-architecture-snapshot.md`](../target-architecture-snapshot.md);
3. the M1 scope, acceptance criteria, and migration seams in
   [`../coding-agent-v1-implementation-roadmap.md`](../coding-agent-v1-implementation-roadmap.md);
4. the frozen V1 delivery and direct-tree gate in
   [`../v1-development-capability-matrix.md`](../v1-development-capability-matrix.md); and
5. the M0 current-to-target evidence and authoritative migration input:
   [`../coding-agent-v1-m0-characterization.md`](../coding-agent-v1-m0-characterization.md) and
   [`../evidence/v1-m0/m1-migration-inventory-v1.json`](../evidence/v1-m0/m1-migration-inventory-v1.json).

The M0 contract is historical and closed. M1 must preserve its characterization conclusions; it
does not reopen ADR design, rewrite M0 evidence, or treat M0 testability permission as continuing
authorization.

## 2. Closed implementation scope

M1 may implement only the following.

1. Durable domain identities and repository/port boundaries for `RepositoryIdentity`,
   `ProjectScope`, `WorkspaceBinding`, `Conversation`, `Turn`, a `RuntimeExecution` mapping, and
   ordered Conversation semantic events.
2. The explicit separation of product transcript/semantic-event authority from Runtime and tool
   audit authority. Runtime FSM state remains on exactly one execution-owned record; Conversation
   and Turn do not acquire a duplicate FSM.
3. An additive SQLite migration and compatibility adapters for the Product records. Exact table
   names, keys, indexes, migration version, mapping cardinality, and additive-backfill sequencing
   are M1 design outputs, but must remain consistent with the accepted ADR invariants.
4. Legacy import/mapping: every legacy `Session` maps to one `RuntimeExecution`; where needed for
   inspection, create a clearly marked synthetic historical Conversation and one synthetic Turn.
   Synthetic provenance must never claim that a historical one-shot Session had new interactive
   semantics. After activation, a new legacy one-shot run must likewise receive its synthetic
   Product mapping when the M1 path is enabled, in the same SQLite authority. The M1 mapping write
   itself must be atomic, idempotent, recoverable, and retryable without a half-created mapping;
   it must retain existing one-shot semantics and must not implement M2 Turn Admission or change
   the legacy `run_task()` lifecycle/admission atomicity.
5. The hybrid `RepositoryIdentity` registry/marker/discovery strategy, provided descriptor facts
   never automatically merge independent clones and non-Git directory identity remains supported.
6. Tests, fixtures, migration evidence, documentation, and compatibility adapters required to
   prove this scope.

M1 ends at durable identities, mappings, and migration compatibility. It does not deliver the
interactive product workflow merely because its records exist.

## 3. Explicitly allowed changes

Within the closed scope, the executor may:

- add Product-layer domain/repository modules and additive SQLite records, migrations, repository
  methods, and private compatibility adapters;
- add atomic transaction composition needed to commit M1-owned product records and legacy mappings
  without creating a second durable authority;
- add versioned read/projection support where required to preserve old and new records, including
  synthetic historical provenance;
- add deterministic success, expected-failure, crash/recovery, rollback, migration, replay, and
  compatibility tests and disposable database fixtures;
- update current-state, handoff, roadmap/checklist, architecture navigation, and migration evidence
  to report verified M1 behavior accurately; and
- make a separately reviewable M1 design choice among deferred physical representations only when
  the choice is documented, tested, rollback-safe, and does not contradict an Accepted ADR.

## 4. Prohibited changes and retained gates

The following remain out of scope and must not be smuggled in as scaffolding, defaults, caches,
startup artifacts, or compatibility shortcuts.

- M2 workspace lifecycle, writer authority, rebind workflow, Turn Admission, coordinator, or
  direct working-tree product path. M1 may persist identity/mapping facts; it must not bind or
  mutate a real user tree through a new product flow.
- M3 Project Instructions, ContextManifest, compaction changes, model-input redesign, or repair of
  the uncertain-provider-retry defect. Exact frozen-request retry remains M3-owned.
- M4 capabilities, PermissionPolicy/PermissionRequest UX, command expansion, diff/undo,
  CodeCheckpoint behavior, or any general Shell. `restricted_test` and `run_command` remain
  trusted-profile/structured-argv legacy behavior.
- M5 interactive CLI commands, attachment/responsiveness, product `/resume`, `/new`, `/status`,
  `/stop`, or product admission UI; M6 Memory UX/default serving/retrieval; and M7 deletion,
  legacy removal, support-matrix closure, or release evaluation.
- A second Agent loop, coordinator-owned Runtime transitions, Runtime side effects outside
  ToolHarness, provider wire formats outside adapters, public exposure of private SQLite/Product
  schemas, new Runtime IPC v1 semantics, or a Platform consumer implementation.
- Any direct or indirect mutation of a real user working tree. The M3+M4 gate remains closed for
  file writes, commands/caches, Git side effects, startup/admission artifacts inside the checkout,
  indirect effects, and undo. Existing copied workspaces remain required for one-shot, eval,
  headless, and IPC v1 compatibility.
- Changes to Memory defaults, automatic serving, Dynamic Recall research, dependencies/lockfiles,
  accepted ADR text, historical evidence/goldens merely to pass, the sibling `../hermes-agent`
  repository, or a general Shell tool.

## 5. Non-negotiable authority and compatibility invariants

- SQLite is the single durable authority. JSONL stays a rebuildable export and semantic-replay
  input; no shadow JSON/Product store may participate in recovery or state decisions.
- Retain `sessions` as the physical legacy RuntimeExecution state record during the first
  compatibility window. Add before rename/remove; preserve legacy `Session`/`RuntimeSnapshot`
  serialization, existing migrations, and old readers.
- Preserve byte meaning and ordering of legacy messages, events, checkpoints, model/tool journals,
  summaries, leases, interrupts, reconciliation records, and dormant Memory governance/audit
  records. Preserve supported payload versions and legacy-unversioned payloads according to the M0
  inventory; do not infer a payload version from SQLite schema version.
- Keep `run_task()`, `resume_session()`, existing session CLI administration, JSONL export/replay,
  eval/oracle paths, and Runtime IPC v1 public `runtime_session_id` behavior compatible. Internal
  mappings are private and must not change IPC v1 schemas or claim that `v0.1.0` implements IPC v1.
- The deterministic FSM and one Agent execution loop remain execution-owned. `COMPLETED` remains
  Runtime completion, not task success. Provider-specific formats remain inside model adapters;
  all Runtime side effects remain routed through ToolHarness.
- Preserve all four original semantic goldens unchanged:
  `bugfix_success.json`, `test_failure_recovery.json`, `permission_denied.json`, and
  `runtime_failure.json`.
- A Conversation owns an append-only semantic transcript, not files, a workspace, or a Runtime
  FSM. A Turn has exactly one RuntimeExecution in V1 and projects its status; a RuntimeExecution
  has one immutable WorkspaceBinding. Repository descriptors never auto-merge independent clones.

## 6. Required deliverables

1. M1 domain and persistence design record, including the selected physical identity/registry and
   additive-migration representation decisions, their ADR mapping, and unresolved matters deferred
   to later milestones.
2. Versioned additive migration(s), with exact schema version and durable migration provenance;
   compatibility repositories/adapters; and no destructive cleanup.
3. A legacy-mapping specification and evidence demonstrating Session-to-RuntimeExecution linkage
   plus clearly labeled synthetic Conversation/Turn provenance for imported historical data and
   for newly created legacy one-shot runs. The latter must prove the mapping uses the same SQLite
   authority and its own transaction is atomic, idempotent, recoverable, and retryable without a
   half-created mapping; it must not alter legacy `run_task()` lifecycle/admission atomicity.
4. Disposable migration fixtures covering empty/new/current/future-version databases and terminal,
   failed, interrupted, retrying, uncertain model/tool, reconciliation, committed-response,
   summary, and Memory-containing legacy rows.
5. Test and evidence report linking each M1 acceptance criterion to success, expected failure,
   crash/recovery, and rollback behavior, with actual commands/counts and protected compatibility
   results.
6. Updated state, handoff, roadmap, active-checklist, and navigation documentation that
   distinguishes implemented M1 behavior from M2–M7 target behavior.

## 7. Verification and acceptance matrix

| Acceptance criterion | Required proof |
|---|---|
| Product identities survive restart and round-trip | repository-level persistence tests for each M1 identity, independent-clone descriptor cases, non-Git identity, and no Runtime FSM duplicated into Conversation/Turn |
| Every legacy Session is inspectable without loss | v1–v4 fixture upgrade and old Session/new mapping round trips preserving ordered messages/events, checkpoint, model/tool journal, summary, lease, and Memory facts |
| New one-shot compatibility mapping is recoverable | a post-activation `run_task()`/one-shot compatibility test proves synthetic Product mapping uses the same SQLite authority; its own transaction is atomic and idempotent, an injected failure leaves no half-created mapping and can retry, and legacy `run_task()` lifecycle/admission atomicity remains unchanged |
| Migration is additive and recoverable | empty/new/current/future-version matrix; idempotent rerun; injected failure at every migration transaction/statement boundary; rollback leaves the pre-M1 DB usable; already committed earlier versions remain valid |
| SQLite remains sole authority | recovery/export tests prove no Product shadow store and JSONL remains derived; old JSONL/replay readers remain usable |
| Runtime and public compatibility are unchanged | full legacy regression suite, four semantic goldens, old CLI/session commands, Runtime IPC v1 schemas/vectors/producer tests, and source/copy-workspace invariants |
| M1 does not open later gates | expected-refusal tests or equivalent coverage prove no product direct-tree mutation, no product command/permission UX, no interactive product command, no Memory default activation, and no frozen-retry repair |
| Failure, recovery, and rollback are explicit | durable journals/provenance plus observable error results for migration failure, unsupported future version, interrupted/backfill recovery, and retry/reconciliation fixture cases; no silently skipped or rewritten failures |

Before M1 can be proposed complete, run the full
`PYTHONPATH=src python3 -m unittest discover -v` suite; focused M1 migration/compatibility tests;
the four semantic goldens; Runtime IPC v1 schemas/vectors/producer tests; JSONL export/replay
tests; Ruff; mypy; compileall; the configured coverage threshold; wheel/sdist build plus an
independent installation or import smoke; calculator/todo scripted smoke; and the existing Memory
regression benchmark. Do not run a live Provider without a new hypothesis and explicit need; its
absence is not a waived gate. Report actual commands, pass/fail/skip/expected-failure counts,
coverage, and every unavailable environment. Historical M0 counts are not substitutes. Validate
documentation links/fences and `git diff --check`.

## 8. Success, failure, and rollback/recovery semantics

Success is not merely a database upgrade. It requires durable round-trip identity, exact legacy
preservation, surviving restart, old-reader compatibility, and all acceptance-matrix evidence.

Expected failures must remain observable and classified: unknown future schema version rejects
before M1 migration work; an incomplete or failed migration rolls back that version atomically;
unsupported/corrupt mappings do not fabricate target semantics; and no data-loss or authority
fallback may be hidden behind a synthetic mapping. Existing M3-owned uncertain-request behavior
may be characterized but is not repaired or normalized as M1 behavior.

Rollback/recovery must be proven on disposable copies. A failure before the M1 transaction commits
leaves the original supported database usable by legacy readers. A restart after a committed
additive migration must be idempotent and retain both old and new views. M1 must not offer a
destructive downgrade or delete legacy rows as its rollback strategy.

## 9. Exit gate, checklist, and stop conditions

The M1 checklist is intentionally open on issuance. No item may be checked until success, expected
failure, and rollback/recovery evidence required above all pass.

- [ ] Physical identity, registry/discovery, schema, key, and provenance decisions documented and
      ADR-consistent.
- [ ] Additive migration and compatibility adapters implemented; v0–v4 starts and future-version
      rejection verified.
- [ ] Legacy Session mappings and synthetic historical provenance are lossless and inspectable.
- [ ] Newly created legacy one-shot runs receive a synthetic Product mapping in the same SQLite
      authority; the mapping transaction is atomic/idempotent/recoverable without a half-created
      mapping and does not change legacy `run_task()` lifecycle/admission atomicity.
- [ ] Product transcript/semantic events are distinct from Runtime/tool audit without duplicate FSM
      authority.
- [ ] Migration statement/transaction failure, restart, rollback, and mixed historical-data
      recovery evidence pass.
- [ ] Existing one-shot/headless copied-workspace, JSONL/replay, CLI, IPC v1, Memory-off, and four
      semantic-golden compatibility evidence pass unchanged.
- [ ] Full M1 verification report and state/navigation updates are complete; no M2–M7 behavior is
      claimed implemented.

Stop immediately and request owner direction if an Accepted ADR conflicts with another authority,
if a required migration cannot preserve a supported legacy reader/payload, if an implementation
would require a second durable authority or a change to Runtime IPC v1, or if satisfying an M1
criterion would enter M2–M7 scope. Record a material amendment as a new, dated amendment section;
do not silently broaden or rewrite this issued contract.

## 10. Downstream status

M1 is **ACTIVE**. M2, M3, M4, M5, M6 (optional), and M7 remain **inactive**. M1 completion does
not autoactivate any of them, does not open the M3+M4 real-working-tree mutation gate, and does
not authorize legacy removal.
