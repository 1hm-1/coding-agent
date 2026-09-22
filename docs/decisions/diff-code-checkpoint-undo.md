# ADR: Diff Authority, Code Checkpoints, and Undo

Date: 2026-09-20
Status: **Accepted architecture decision; implementation inactive**

## Context

The product normally operates directly on the user's working tree. A Conversation has a default
WorkspaceBinding, each Turn has one RuntimeExecution, and every real side effect must pass through
ToolHarness. Controlled file edits can use content or revision preconditions, while commands,
formatters, generators, package managers, Git operations, and external processes can change files
in ways the Runtime cannot fully attribute.

The current implementation has a Runtime recovery checkpoint and tool-call journal. The Runtime
checkpoint records FSM and continuation state. Reconciliable file edits record pre-edit and
planned post-edit hashes, and sandbox commands record workspace fingerprints before and after
execution. The implementation does not retain user-facing file checkpoints, full before/after
images, Turn-scoped change provenance, or an undo operation. Its bounded Git diff summary is
context evidence from the current isolated workspace, not product authority for a user's real
working tree.

The architecture must therefore keep three meanings separate:

```text
Runtime recovery checkpoint
    ≠ Code checkpoint or rewind authority
    ≠ Git history
```

This ADR builds on:

- [`project-repository-workspace-conversation.md`](./project-repository-workspace-conversation.md),
  which defines direct working-tree use, immutable RuntimeExecution workspace binding, external
  drift, and one coordinated writer per WorkspaceBinding;
- [`conversation-turn-runtime-execution-lifecycle.md`](./conversation-turn-runtime-execution-lifecycle.md),
  which defines one Turn to one RuntimeExecution, terminal history, cancellation, and recovery;
  and
- [`tool-capability-command-permission.md`](./tool-capability-command-permission.md), which defines
  ToolHarness as the sole side-effect gateway, revision preconditions, capability claims, hard
  policy boundaries, and the distinction between permission risk and recovery mode.

This ADR does not activate implementation, modify a schema, define an implementation plan, or
turn the current Runtime checkpoint into a code-history mechanism.

## Decision

### 1. User-visible diff has three independent authorities

The product distinguishes three views with different questions and guarantees.

#### Current Workspace Diff

This view answers:

> What is the current workspace's total difference from Git or another explicit reference
> baseline?

In a Git workspace, Git is the authority for repository-relative state and the view distinguishes:

```text
HEAD → index
index → working tree
untracked paths
```

The view may include changes that existed before the Turn, changes made by the Agent, changes made
by the user, and changes made by other processes. It is not an authorship view. In a non-Git
workspace, the reference baseline must be named explicitly; there is no implicit HEAD equivalent.

#### Turn Workspace Delta

This view answers:

> What net workspace changes have been observed since this Turn began?

Its authority is the TurnStartCodeCheckpoint and current compatible workspace observations. It is
a temporal delta. It does not claim that every change was caused by the Agent or by a specific
ToolInvocation.

#### Agent-Controlled ChangeSet

This view answers:

> Which file mutations are confirmed to have been performed by ToolHarness-controlled file-edit
> operations?

Its authority is the controlled change journal with exact before/after evidence and originating
ToolInvocation provenance.

Changes observed around an Agent-invoked command use the distinct provenance kind
`OBSERVED_COMMAND_DELTA`. They must not be silently promoted to Agent-Controlled Change. Changes
that appear in the Turn delta without controlled or command-window provenance remain concurrent or
unattributed changes.

When a user asks what the Agent changed in one Turn, the product presents, in order:

1. confirmed controlled edits;
2. changes observed during Agent-invoked command windows;
3. concurrent or unattributed changes; and
4. an optional Current Workspace Diff.

The three views may overlap in paths but never share authority merely because their rendered
patches look identical.

### 2. Every accepted Turn starts with a CodeCheckpoint

Every durably accepted Turn establishes a `TurnStartCodeCheckpoint` before any code side effect is
allowed. The checkpoint is associated with the Turn, RuntimeExecution, RepositoryIdentity,
ProjectScope, and immutable WorkspaceBinding.

The checkpoint and its evidence remain available when the Turn ends in `COMPLETED`, `FAILED`, or
`CANCELLED`. A terminal failure or cancellation does not imply that earlier file effects vanished.

The checkpoint uses a hybrid baseline strategy:

- clean Git-tracked content may reference an existing Git object rather than duplicate its bytes;
- dirty tracked content retains a content-addressed Turn-start before image;
- untracked content within checkpoint coverage retains a content-addressed Turn-start before
  image;
- unsupported, oversized, ignored, binary, linked, special, or otherwise uncovered paths record
  an explicit coverage result; and
- the first version is not required to take a complete snapshot of an entire large workspace.

A Git object reference is useful only while the referenced content remains available and the
checkpoint remains compatible with the WorkspaceBinding. If the object or an external snapshot is
no longer available, the coverage state reflects that loss instead of silently weakening the
guarantee.

### 3. CodeCheckpoint coverage is explicit authority

Every CodeCheckpoint declares coverage. Coverage identifies at least:

- the WorkspaceBinding and project scope examined;
- which paths or path classes have restorable before content;
- which paths have metadata or hash evidence only;
- exclusions and their reasons;
- the Git reference and index facts observed at checkpoint creation, when applicable;
- retention availability; and
- compatibility or staleness state.

Coverage controls the product guarantee:

- a covered path with available before/after authority may qualify for strong controlled rewind;
- a metadata-only path may contribute to a change list but not a reconstructable patch or rewind;
- an excluded, expired, unavailable, or stale path receives no strong diff or undo claim; and
- absence from an incomplete manifest is never interpreted as proof that the path did not change.

The UI must disclose incomplete coverage before presenting a Turn-level diff or rewind as
complete. It must not reduce coverage to an ambiguous success flag.

### 4. Controlled file mutations retain exact evidence

Every ToolHarness-controlled file mutation retains at least:

- normalized workspace-relative path;
- path and file type;
- supported file metadata;
- before revision or content hash;
- after revision or content hash;
- a content-addressed before image;
- a content-addressed after image;
- originating ToolInvocation;
- RuntimeExecution;
- Turn;
- ordered edit sequence; and
- provenance kind.

The images are the content authority. Hashes and revisions provide identity, stale detection,
preconditions, and reconciliation evidence. Origin and sequence provide audit and composition.

A patch or diff hunk is a derived UI representation. It is not the sole restoration authority.
Patch generation may be repeated from retained images without changing the source evidence.

For a supported create, delete, rename, type change, or metadata change, the evidence must be
sufficient to state and verify the corresponding before and after conditions. If it is not, that
mutation cannot receive the strong controlled-rewind guarantee.

### 5. Command observations remain weaker provenance

For an Agent-invoked command, the system may record an observation window with:

- the ToolInvocation and execution identity;
- declared write and resource scope;
- pre-command workspace or scoped manifest;
- post-command workspace or scoped manifest when execution returns normally;
- paths and revisions observed to differ; and
- observation coverage and uncertainty.

This produces `OBSERVED_COMMAND_DELTA`, not controlled edit provenance. Writer authority prevents a
second cooperating product writer but does not exclude an IDE, external shell, formatter, hook, or
other uncoordinated process. Even when a command is the likely cause, the provenance wording must
remain observational in a direct user workspace.

The following changes may appear in Turn Workspace Delta or `OBSERVED_COMMAND_DELTA` but receive no
default automatic strong undo guarantee in direct-working-tree mode:

- formatter changes;
- code-generator output;
- package-manager changes;
- arbitrary command effects;
- Git command effects; and
- external-process changes.

The product does not offer an undifferentiated “Undo everything” claim.

### 6. Direct-working-tree undo has a conditional strong guarantee

In ordinary direct-working-tree mode, undo applies only to Agent-Controlled file mutations covered
by retained evidence.

For each target, the strong guarantee is conditional on the current path, type, supported
metadata, and content revision exactly matching the recorded after state. When the precondition
holds, the operation restores the exact recorded before state within the supported metadata
scope.

This means a controlled edit to an already dirty file restores the exact state recorded immediately
before the selected controlled edit chain, not Git HEAD. When the chain begins from the Turn-start
state, this preserves the user's pre-existing dirty content. If an external change occurred before
the controlled chain, the external change is part of that recorded before state and is preserved.
Undo does not reset unrelated files.

When a target does not match:

```text
revision or identity mismatch
    → refuse automatic rewind
    → surface the conflict and coverage
    → do not overwrite the current file
```

The first version does not perform an automatic three-way rollback or inverse-patch merge. Such a
merge would be a best-effort conflict-resolution feature and requires a separate decision.

### 7. Turn-level rewind composes fine-grained evidence

The checkpoint model is layered:

```text
Product layer
    TurnStartCodeCheckpoint
    Turn-level diff
    Turn-level rewind entry

Internal evidence
    per-controlled-file-mutation before/after authority
    per-command-invocation observation window
    Turn-start workspace and Git baseline
```

A model-call batch is not a primary code-checkpoint boundary. Provider scheduling and batching are
not stable user semantics.

In direct-working-tree mode, a Turn-level rewind entry means “reverse the eligible controlled
mutations associated with this Turn.” It does not mean “restore the entire workspace to the
TurnStartCodeCheckpoint.” TurnStart remains the temporal-diff reference, while controlled mutation
evidence defines the direct-mode restore set.

When one path has multiple contiguous controlled mutations in a Turn, Turn-level rewind uses the
ordered chain: the current state must match the latest covered after state, and the target is the
earliest covered before state selected by the rewind. An intervening unsupported mutation,
external drift, incompatible path transition, or missing image breaks the strong chain.

Successful edits remain eligible even when later edits were denied by permission policy. A denied
ToolInvocation contributes no file mutation evidence.

### 8. Multi-file rewind validates all targets before writing

The first version uses all-preflight-before-write:

1. acquire writer authority for the exact WorkspaceBinding;
2. identify the complete target set and required evidence;
3. validate coverage, retention, topology compatibility, path identity, metadata, and current
   revision for every target;
4. if any target fails, write nothing and report every known conflict;
5. only after all targets pass may restore side effects begin through ToolHarness.

The operation retains writer authority through its active restore phase. Each actual file restore
also revalidates its target immediately before writing. This is necessary because external
processes are not controlled by the writer guard.

All-preflight prevents a known initial conflict from causing a partial restore. It does not create
a cross-file filesystem transaction. If an external process changes a target after preflight, or
the host crashes after some writes, the operation stops rather than overwrite a mismatch and uses
its durable progress for reconciliation. The resulting partial physical progress is reported as
an interrupted or reconciliation condition, never as a successful atomic rewind.

Interactive partial rewind and conflict resolution are deferred to a separate decision.

### 9. CodeRewindOperation is a durable product operation

`/undo` is neither a ControlRequest for the original RuntimeExecution nor a new model-driven Turn.
It creates a durable `CodeRewindOperation` that:

- references the Conversation;
- references the original WorkspaceBinding;
- references the target Turn and CodeCheckpoint;
- acquires the WorkspaceBinding writer authority;
- passes every restore side effect through ToolHarness;
- uses path, metadata, and revision preconditions;
- durably records target selection and per-target progress;
- reconciles an uncertain restore after host crash; and
- never rewrites the old Turn or RuntimeExecution history.

The user's explicit rewind action supplies the product intent, but it does not bypass the hard
boundaries from the tool and permission ADR. Containment, writer authority, sandbox or platform
requirements, managed hard deny, and an enforceable Plan/read-only ceiling still apply. If a mode
forbids writes, the user must leave that mode through a distinct product action rather than use the
rewind as an implicit bypass.

The operation may have its own narrow durable progress and reconciliation statuses. It is not a
RuntimeExecution because it has no model loop, context lifecycle, provider attempts, Agent tool
selection, or Turn outcome. It is a deterministic compensating workspace mutation coordinated by
the product layer.

A CodeRewindOperation cannot overlap another coordinated writer on the same WorkspaceBinding. V1
rejects `/undo` while the Conversation has an open Turn or active RuntimeExecution, as finalized in
[`interactive-cli-product-workflow.md`](./interactive-cli-product-workflow.md). The user must first
complete, cancel, or otherwise resolve that execution. This product precondition does not change
either lifecycle model.

### 10. Managed snapshot-backed rewind is a WorkspaceBinding capability

A future WorkspaceBinding may explicitly declare snapshot-backed rewind capability. Within its
declared coverage, it may provide a stronger Turn-level workspace rewind, including file changes
that were observed after commands but were not controlled edits.

The capability and each checkpoint must state exactly what the snapshot covers. By default it does
not cover:

- Git shared refs;
- Git HEAD;
- Git index;
- remote or network side effects;
- external directories;
- environment or credential state; or
- any path outside snapshot coverage.

Managed rewind must not be implemented through a hidden commit, automatic branch, or other
undisclosed Git history mutation. A snapshot capability can strengthen workspace-file restoration;
it cannot convert external or shared-system effects into reversible file changes.

### 11. Git is reference and history, not checkpoint authorship

Git remains the authority for:

- repository history;
- HEAD, branches, and refs;
- index state;
- staged, unstaged, and untracked views; and
- worktree semantics.

Git is not the authority for:

- Agent authorship;
- controlled edit provenance;
- non-Git code checkpoints; or
- automatic Turn checkpoint creation.

The architecture prohibits hidden checkpoint commits, per-Turn automatic commits, and treating an
Agent checkpoint as a Git commit. If the Agent later executes an explicit `git commit`, it is an
ordinary permission-controlled Git operation with the applicable shared-state coordination. It is
not an internal CodeCheckpoint implementation detail.

### 12. Controlled undo preserves index intent

The Current Git view distinguishes:

```text
HEAD → index       staged state
index → worktree   unstaged state
untracked paths
```

Controlled undo restores working-tree file state only. It does not automatically execute
`git reset`, `git restore --staged`, unstage files, or roll back the index.

If a user had staged state before the Turn, the Turn-start checkpoint records the corresponding
Git facts and actual working-tree content. Undo restores the covered Turn-start working-tree
content while leaving the original index unchanged. A Git command that later changes the index is
outside the controlled file-edit undo guarantee.

### 13. Code checkpoints survive Conversation resume subject to retention and compatibility

CodeCheckpoint identity and metadata remain available after Conversation resume. Every checkpoint
has independently visible:

- retention state;
- coverage state; and
- compatibility or staleness state.

The physical retention duration, content-store layout, and garbage-collection policy are not
decided here. Expired or missing images make the associated strong rewind unavailable; the product
must not reconstruct an authority claim from transcript text or hashes alone.

A checkpoint remains bound to its original WorkspaceBinding. Conversation rebind does not move the
checkpoint or make it applicable to another working tree. If the original workspace is missing,
replaced, or incompatible, the checkpoint may remain inspectable while automatic rewind is
unavailable.

### 14. Git topology drift makes old checkpoints stale for automatic rewind

If, after checkpoint creation, the workspace undergoes a branch switch, checkout, rebase, merge
topology change, or incompatible HEAD transition, the checkpoint enters stale or conflict state
for automatic rewind.

The first version does not attempt to prove that individual file hashes happen to be compatible
across the topology transition. Automatic rewind remains unavailable even when some paths look
unchanged. Inspection and historical evidence may remain available.

Cross-topology restore or a future compatibility validator requires a separate architecture
decision.

### 15. Runtime recovery, tool calls, code history, and Git remain separate

The architecture keeps four authorities separate:

| Subsystem | Authority |
|---|---|
| Runtime recovery checkpoint | FSM, pending calls, retry, lease, recovery, and execution continuation |
| Tool-call journal | ToolInvocation, attempts, result, and side-effect reconciliation evidence |
| Code checkpoint and change journal | Turn baseline, before/after file authority, diff provenance, and rewind eligibility |
| Git history and index | repository history, refs, branch topology, staging, and worktree relationships |

They may share RepositoryIdentity, WorkspaceBinding, Conversation, Turn, RuntimeExecution,
ToolInvocation, durable sequence, and checkpoint or change identifiers. Shared provenance does not
merge their lifecycle or authority.

Before a controlled file side effect begins, the restorable before image and change intent must be
durable. After the side effect, the after evidence is recorded. Runtime recovery may use the shared
ToolInvocation and revision evidence to reconcile a crash, while the code journal retains the
content authority needed for user rewind.

Runtime resume never rewinds code automatically. Code rewind never changes an old
RuntimeExecution's terminal outcome, checkpoint, or transcript history.

## Architectural invariants

1. Current Workspace Diff, Turn Workspace Delta, and Agent-Controlled ChangeSet are distinct
   views with distinct authorities.
2. `OBSERVED_COMMAND_DELTA` is never silently classified as a controlled edit.
3. Every accepted Turn establishes a TurnStartCodeCheckpoint before code side effects.
4. Failed and cancelled Turns retain their code-checkpoint and change evidence.
5. Every checkpoint has explicit coverage, retention, and compatibility state.
6. A path outside checkpoint coverage receives no strong diff or undo guarantee.
7. Controlled mutations retain exact before and after content authority; patches are derived.
8. Direct-working-tree undo is limited to covered controlled file mutations whose current state
   matches the recorded after state.
9. A preflight mismatch causes zero restore writes for that CodeRewindOperation.
10. External drift detected after preflight is never overwritten silently and may require
    reconciliation of partial physical progress.
11. The first version performs no automatic three-way or inverse-patch merge.
12. Managed snapshot rewind is capability- and coverage-scoped and does not imply external or Git
    shared-state rollback.
13. Hidden checkpoint commits and automatic per-Turn commits are prohibited.
14. Controlled undo modifies working-tree state only and preserves the Git index.
15. CodeRewindOperation is a durable product operation, not a ControlRequest, Turn,
    RuntimeExecution, or Task.
16. Every rewind side effect passes through ToolHarness and requires writer authority and current
    preconditions.
17. Conversation rebind never moves a CodeCheckpoint to another WorkspaceBinding.
18. Incompatible Git topology drift disables automatic rewind from the old checkpoint.
19. Runtime recovery checkpoint, tool-call journal, code journal, and Git history never substitute
    for one another.
20. V1 rejects CodeRewindOperation admission while the Conversation has an open Turn or active
    RuntimeExecution.

## Consistency review with the prior ADRs

The accepted decisions are consistent with all three prior architecture clusters.

### Workspace and repository consistency

- **Direct working tree:** Turn baselines and controlled before images preserve the user's actual
  dirty content. Rewind targets that content, not HEAD.
- **Immutable execution binding:** Tool changes and TurnStartCodeCheckpoint refer to the
  RuntimeExecution's immutable WorkspaceBinding. Later Conversation rebind cannot relocate an old
  checkpoint or rewind operation.
- **External drift:** temporal Turn delta may include external changes, while exact per-target
  preconditions prevent silent overwrite. A global workspace fingerprint is not the sole gate.
- **Writer authority:** CodeRewindOperation is another coordinated workspace writer and cannot run
  concurrently with a write-capable RuntimeExecution or another rewind on the same binding.
- **Shared Git state:** controlled file rewind does not mutate refs, HEAD, or index, so it does not
  introduce a repository-wide writer lock.

### Conversation and Runtime lifecycle consistency

- **Turn boundary:** one Turn-start checkpoint corresponds to the accepted InitialRequest.
  Steering and UserReply within that Turn do not create extra product checkpoints, although
  controlled mutations continue to receive fine-grained evidence.
- **Terminal outcomes:** code effects and evidence remain after `COMPLETED`, `FAILED`, or
  `CANCELLED`; terminal Runtime history remains immutable.
- **Recovery:** an uncertain original ToolInvocation is reconciled by Runtime recovery. A later
  user-requested rewind is a separate compensating operation and never masquerades as recovery of
  that invocation.
- **Conversation resume:** restoring a Conversation exposes retained checkpoint availability but
  does not execute a rewind or resume a RuntimeExecution automatically.
- **One Turn to one RuntimeExecution:** CodeRewindOperation contains no Agent loop and creates no
  second execution attempt inside the target Turn.

### Tool, command, and permission consistency

- **ToolHarness:** controlled edits and restore actions share the sole side-effect gateway.
- **Capability and hard boundaries:** a user-requested rewind cannot bypass containment, policy,
  Plan/read-only mode, writer authority, or unsupported filesystem behavior.
- **Revision preconditions:** exact after-state validation is the undo admission condition, while
  before images provide restoration content.
- **Command uncertainty:** command-window observations remain weaker than ToolHarness-controlled
  file edits, matching the accepted distinction between claims, enforcement, and recovery.
- **Permission versus recovery:** the user's request authorizes the intended rewind product action;
  a crash during restoration still follows reconciliation rules and cannot be treated as a fresh
  approval question.

No decision requires changing RepositoryIdentity, ProjectScope, WorkspaceBinding, Conversation,
Turn, or RuntimeExecution identity relationships.

## Why CodeRewindOperation is not a second RuntimeExecution

The two operations have different purposes and machinery:

| RuntimeExecution | CodeRewindOperation |
|---|---|
| Executes one user coding request through an Agent loop | Applies a deterministic selected compensation |
| Builds model context and invokes a provider | Has no model context or provider call |
| Selects tools through model output | Uses a precomputed restore target set |
| Owns one Turn outcome | Does not create or alter a Turn |
| Handles model/tool attempts and steering | Handles preflight, per-target restore progress, and reconciliation only |
| May finish completed, failed, or cancelled | Reports rewind success, conflict, interruption, or reconciliation status without projecting a Turn state |

Durability does not make every operation a RuntimeExecution. CodeRewindOperation has identity
because a multi-file side effect can crash and must be audited, not because it is an Agent task.

## Dirty and staged state review

The accepted rules preserve existing user work:

- a dirty tracked file receives a Turn-start before image within coverage;
- a controlled edit records the dirty file as its exact before state;
- successful rewind restores that dirty state, not the repository's HEAD version;
- unrelated pre-existing dirty files are not targets and remain untouched;
- the Git index is observed for diff presentation but not mutated by controlled rewind;
- staged content remains staged when working-tree content is restored; and
- a later user or external edit to a target changes its revision and blocks automatic rewind.

This is conditional on explicit checkpoint coverage and retained images. The UI cannot claim that
an ignored, oversized, expired, special, or otherwise uncovered path is protected.

## Consequences

### Positive

- Users can distinguish total workspace state, Turn-time changes, and confirmed Agent edits.
- Dirty working trees remain first-class and are not reset to Git HEAD by undo.
- Exact before/after images support durable diffs and safe conditional restoration in Git and
  non-Git directories.
- Command effects remain visible without receiving false authorship or reversibility claims.
- Runtime recovery assets and code-history assets can share provenance without becoming one
  subsystem.
- A deterministic rewind does not consume a new Agent Turn or reopen terminal execution history.
- Managed workspaces can add stronger snapshot guarantees without weakening the direct-workspace
  contract.

### Negative and risks

- Checkpoint coverage can be incomplete in large, ignored, binary, linked, or unusual workspaces.
- Content-addressed before and after images require local storage and retention management.
- Direct-mode undo intentionally excludes common formatter, generator, package-manager, and shell
  effects from its strong guarantee.
- A conservative revision mismatch refusal may require the user to resolve even non-overlapping
  later edits manually.
- All-preflight is not a filesystem transaction; late external drift or host crash can leave
  durable partial progress requiring reconciliation.
- Git topology changes make otherwise plausible old checkpoints unavailable for automatic rewind.
- Separating three diff views adds UX complexity but is necessary to avoid false attribution.

## Alternatives rejected for the target model

- **Use `git diff HEAD` as Turn and Agent authority:** it includes pre-existing and external changes,
  omits non-Git workspaces, and cannot prove authorship.
- **Use only a Turn-start workspace snapshot:** it captures temporal change but cannot distinguish
  controlled edits from concurrent drift.
- **Use only the edit journal:** it misses formatter, generator, command, and external changes that
  users still need to see.
- **Restore the entire direct working tree to Turn start:** it can erase later user work and cannot
  reverse external or shared-system effects.
- **Automatically merge an inverse patch on revision mismatch:** it weakens the first-version
  non-overwrite guarantee and requires a distinct conflict-resolution contract.
- **Treat command fingerprints as undo authority:** aggregate before/after fingerprints neither
  identify changed paths nor retain restoration content.
- **Create a hidden Git commit for every checkpoint:** it mutates user history and shared refs,
  changes staged-state semantics, and fails for non-Git directories.
- **Drive undo through a new Agent Turn:** it converts deterministic compensation into model
  behavior and can introduce new edits.
- **Send undo as a ControlRequest to the old execution:** terminal Runtime history is immutable and
  the old execution does not own future workspace state.
- **Merge code checkpoints into RuntimeSnapshot:** execution continuation and user code restoration
  have different retention, authority, and lifecycle requirements.

## Deferred decisions

This ADR intentionally does not define:

- database tables, migrations, serialized events, or public schemas;
- content-addressed storage layout, compression, encryption, retention duration, or garbage
  collection;
- exact path, file-size, binary, ignored-file, link, metadata, or special-file coverage limits;
- patch renderer, diff UI details, and interaction beyond the accepted layered default view;
- interactive partial rewind, target selection, or conflict-resolution UX;
- automatic three-way or inverse-patch merge;
- cross-topology checkpoint compatibility validation;
- platform-specific atomic file replacement and metadata restoration details;
- a snapshot-backed WorkspaceBinding implementation;
- implementation sequencing, milestones, or acceptance tests.

These decisions must preserve the authority, coverage, non-overwrite, writer, ToolHarness, and
provenance invariants above.

## Reference product behavior

The product distinction is informed by public Claude Code behavior:

- [Checkpointing](https://code.claude.com/docs/en/checkpointing) documents per-turn checkpoints,
  pre-edit file snapshots, session-resumable rewind, the exclusion of Bash and external changes,
  and the separation from version control.
- [Review changes with `/diff`](https://code.claude.com/docs/en/interactive-mode#review-changes-with-diff)
  documents a Git-derived current view and file-edit-derived Turn views, with shell-command changes
  visible only in the current view.

These references establish observable behavior only. They do not establish Claude Code's internal
snapshot format, conflict algorithm, multi-file atomicity, staged-state handling, retention
implementation, or recovery architecture.
