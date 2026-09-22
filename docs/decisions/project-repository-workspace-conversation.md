# ADR: Project, Repository, WorkspaceBinding, and Conversation

Date: 2026-09-18
Status: **Accepted architecture decision; implementation inactive**

## Context

The product target is a local, interactive Coding Agent whose normal entry point is:

```text
cd repo
agent
```

The product model already distinguishes the long-lived `Conversation`, one user-facing `Turn`,
and one recoverable `RuntimeExecution`. The existing Phase 1 `Session` is interpreted as the
current implementation of `RuntimeExecution`, not as the future long-lived Conversation.

The remaining question for this decision was which object identifies the codebase, which object
owns the concrete writable directory, and whether a Conversation or RuntimeExecution may move
between working trees. The existing one-shot Runtime creates an execution-scoped isolated copy.
That behavior remains an implementation fact, but it is not the target ownership model for local
interactive use.

This ADR supersedes the workspace-ownership assumptions in
[`conversation-runtime-refactor-plan.md`](../conversation-runtime-refactor-plan.md), including the
claims that every Conversation owns an isolated source copy and that the source checkout is
universally read-only. It does not activate that refactor or authorize implementation work.
Until implementation is explicitly activated, the current Runtime continues to use its isolated
workspace and must not write to the source checkout.

The later interactive workflow decision in
[`interactive-cli-product-workflow.md`](./interactive-cli-product-workflow.md) refines the initial
writer-authority lifetime below. The one-writer and drift invariants remain accepted, while writer
authority is now acquired for write paths and may be released independently of RuntimeExecution
lifetime.

## Decision

### 1. Domain terms and identity boundaries

The architecture uses four distinct concepts:

```text
RepositoryIdentity
├── RepositoryDescriptor
├── ProjectScope
└── WorkspaceBinding

Conversation
├── repository identity
├── project scope
└── default workspace binding

Turn
└── RuntimeExecution
    └── immutable workspace binding
```

`RepositoryIdentity` is the stable internal identity and logical authority for one local
repository family. Two independent clones have different Repository identities by default, even
when they have the same remote or commit history.

`RepositoryDescriptor` contains evidence used to describe, locate, and compare repositories. It
may include the Git common-dir, canonical paths, remotes, and commit or history hints. Descriptor
fields are not identity authority and must not automatically merge two Repository identities.

`ProjectScope` identifies the product or subdirectory scope within a Repository. Repository and
Project are not synonyms: one monorepo may contain multiple Project scopes. A Project scope may
be represented relative to its Repository, but this ADR does not freeze its persisted form.

`WorkspaceBinding` identifies one concrete directory tree available to the Agent. It may refer to
the main working tree, a linked Git worktree, or a non-Git directory. It also carries the working
directory or project root and observed Git/workspace facts. A WorkspaceBinding is distinct from
RepositoryIdentity because one Repository may have multiple worktrees.

Git-specific descriptor fields are optional for a non-Git directory. How its stable
RepositoryIdentity is discovered remains part of the deferred persistence decision.

The physical storage location and discovery mechanism for the stable Repository identity remain
undecided. The domain model must not depend on a repository marker, a Git common-dir marker, or an
external registry.

### 2. Local interactive workspace

Local interactive startup binds directly to the user's current working tree and permits changes
there. The working tree remains user-owned filesystem and version-control state. Conversation and
RuntimeExecution reference it through a WorkspaceBinding; neither owns the files.

The product must preserve the normal development experience in which Agent edits, IDE edits,
tests, Git status, and later Turns observe the same working tree.

### 3. Managed worktrees

A managed worktree is not the default for ordinary local startup. It is an isolation mode for:

- an explicit user request;
- parallel development;
- a high-risk or background scenario.

The first version must not silently create a branch or worktree. If a write-capable execution is
requested while the same WorkspaceBinding already has an active writer, the product must present
the user with these choices:

- wait for the writer;
- continue with capabilities restricted to read-only;
- create an isolated worktree.

Read-only continuation is valid only when the effective tool capabilities make writes and other
write-equivalent side effects impossible.

### 4. Conversation workspace binding

A Conversation stores its RepositoryIdentity, ProjectScope, and a default WorkspaceBinding.
Resume first attempts to use that same workspace. The product must not silently choose another
working tree or substitute the process's current directory.

Between Turns, a user may explicitly rebind the Conversation to a compatible WorkspaceBinding.
The rebind must be recorded as an auditable Conversation event. All context and facts derived
from the prior workspace or its code must then be treated as requiring revalidation.

If the prior workspace no longer exists, the transcript remains readable. Coding cannot continue
until the user explicitly selects or confirms a compatible workspace and the rebind succeeds.

This ADR does not freeze the compatibility algorithm. Repository descriptors may assist the user
and validator, but they cannot authorize an automatic identity merge or rebind.

### 5. RuntimeExecution workspace binding

A RuntimeExecution binds to exactly one WorkspaceBinding when it starts. That binding is immutable
for the lifetime of the execution.

Conversation rebind and RuntimeExecution resume are different operations. A Conversation rebind
affects later work; it cannot move an existing execution, its checkpoint, pending tool calls, or
unknown side effects into another workspace.

The lifecycle decision for abandoning, cancelling, or finishing an execution before Conversation
rebind is intentionally left to the Conversation/Turn/RuntimeExecution lifecycle decision.

### 6. External changes and drift

Changes made through an IDE, formatter, Git command, user shell, or another process are normal in
a local development workspace. A global repository fingerprint must not be the sole gate for
continuing a Conversation or starting a new Turn.

External changes are handled according to their scope and the active lifecycle boundary:

| Change | Between Turns | During an active RuntimeExecution | Crash recovery or uncertain side effect |
|---|---|---|---|
| Ordinary file content change | Invalidate affected code/context facts and re-read as needed | Continue only where current operations remain valid; a conflicting write must fail its precondition | Reconcile against the persisted call and file revisions before replay or completion |
| File creation, deletion, or rename | Refresh repository observations and invalidate affected facts | Stop or refresh any operation whose target or assumptions changed | Reconcile pending effects; do not infer success from a global fingerprint |
| Branch or HEAD change | Treat as normal development, disclose the change, and rebuild code-derived context | Do not silently continue the old execution plan | Do not transparently resume across the topology change |
| Pull, merge, rebase, reset, or conflict-state change | Revalidate Git and code facts before later work | Do not silently continue when topology or current targets changed | Apply stricter reconciliation and require an unambiguous recovery decision |
| Repository/workspace identity, containment, trust, or root change | Require explicit validation or rebind | Refuse further side effects | Refuse automatic recovery into a different binding |

Specific file writes use optimistic content or file-revision preconditions. A write must not
overwrite a file that changed after the execution established the write's expected input without
an explicit re-read and new decision.

Conversation continuation is less strict than RuntimeExecution recovery. A Conversation may
continue with a new Turn after a branch change or other normal development activity, while an
older RuntimeExecution may no longer be safe to resume.

### 7. Writer authority and concurrency

One WorkspaceBinding may have at most one active writer coordinated by this product. Writer
authority is an explicit coordination capability acquired before a RuntimeExecution or durable
product operation enters a write path. Read-only and Plan execution do not acquire it merely by
existing. Authority is released at terminal outcome or an explicitly durable, side-effect-safe
boundary and must be reacquired with drift validation before later writes.

Writer authority is separate from a recovery barrier. An unknown side effect may continue to block
coordinated writers after the normal writer or process lease expires. The complete lifetime and
wait-state refinement is authoritative in
[`interactive-cli-product-workflow.md`](./interactive-cli-product-workflow.md).

Different WorkspaceBindings of the same Repository, including different Git worktrees, may each
have a writer. There is no repository-wide writer lock for ordinary file changes.

The writer guard coordinates cooperating product processes only. It does not claim to prevent
changes by an IDE, user shell, formatter, Git hook, background process, or another Agent. External
drift detection and per-file write preconditions remain required even while writer authority is
held.

Operations that modify shared Git state may require narrower repository-level coordination. This
does not create a general Repository writer lock. The classification of refs, branch, rebase,
reset, and similar operations belongs to the later tool and permission decision.

## Architectural invariants

1. RepositoryIdentity, ProjectScope, and WorkspaceBinding are distinct identities.
2. Independent clones do not become the same RepositoryIdentity from matching descriptors.
3. A Conversation does not own code or filesystem state.
4. A Conversation has a default WorkspaceBinding and may change it only through explicit,
   auditable rebind between Turns.
5. A RuntimeExecution has one immutable WorkspaceBinding.
6. Conversation rebind is never RuntimeExecution resume.
7. Missing workspaces never trigger silent fallback to the current directory.
8. Ordinary external file changes are expected drift, not automatic Conversation failure.
9. Global fingerprint equality is not the sole continue or recovery gate.
10. Conflicting file writes fail through optimistic file/content revision checks.
11. One WorkspaceBinding has at most one coordinated active writer.
12. Writer authority is cooperative and never substitutes for external drift detection.
13. Managed worktrees and Git branches are never silently created in the first version.
14. Ordinary file concurrency is workspace-scoped; shared Git operations may use narrower
    repository-scoped coordination without imposing a global Repository writer lock.

## Consistency review

The accepted decisions are internally consistent. The following apparent tensions are resolved by
the identity and lifecycle boundaries above:

- **Direct working-tree edits versus writer safety:** direct edits are the normal product path;
  the WorkspaceBinding writer guard prevents a second cooperating Agent writer and does not claim
  to lock out the user or IDE.
- **Normal external drift versus immutable execution binding:** the binding identity is immutable;
  the contents inside that workspace may change and are handled through invalidation,
  preconditions, and recovery reconciliation.
- **Conversation rebind versus execution recovery:** the Conversation may rebind only for later
  work. An existing RuntimeExecution remains attached to its original workspace.
- **Stable RepositoryIdentity versus undecided storage:** stable identity is a domain requirement;
  marker or registry placement is a persistence choice and is not part of this decision.
- **Parallel worktrees versus shared Git state:** file writers are isolated by WorkspaceBinding;
  operations affecting shared Git state can receive operation-specific coordination later.
- **Writer capability versus execution lifetime:** a RuntimeExecution owns its immutable workspace
  binding but does not hold writer authority merely by existing. Write paths acquire authority;
  stable waits may release it; unresolved effects retain a separate recovery barrier.

No accepted decision requires replacing the `Conversation → Turn → RuntimeExecution` model.
The model gains explicit references:

```text
RepositoryIdentity 1 ── * ProjectScope
RepositoryIdentity 1 ── * WorkspaceBinding

Conversation
├── RepositoryIdentity
├── ProjectScope
└── default WorkspaceBinding (explicitly rebindable between Turns)

Turn
└── RuntimeExecution
    └── WorkspaceBinding (immutable)
```

`Task` is not required to express any decision in this ADR.

## Consequences

### Positive

- Local interactive behavior matches ordinary terminal development: Agent and user operate on the
  same working tree.
- Conversation continuity is separated from filesystem ownership and exact execution recovery.
- Git worktrees remain available for isolation without burdening every normal conversation.
- Linked worktrees can share a RepositoryIdentity while preserving independent Workspace bindings.
- Monorepos can express multiple Project scopes without pretending they are separate repositories.
- External IDE and Git activity does not make every Conversation unrecoverable.
- The existing Runtime journal, call recovery, and revision evidence remain useful for stricter
  execution recovery.

### Negative and risks

- Direct working-tree edits can affect valuable user state immediately.
- Reliable recovery must account for changes made outside the product.
- Writer authority cannot prevent non-cooperating processes from changing files.
- A write-capable execution may hold workspace authority longer than its actual side-effect
  windows and temporarily reduce concurrency.
- Worktrees isolate files and indexes but do not isolate all Git repository state.
- Explicit rebind can carry conversation assumptions into a materially different branch or
  checkout, so code-derived context must be invalidated and surfaced carefully.
- Stable Repository identity still requires a later persistence and discovery decision.

## Alternatives rejected for the target model

- **Execution-scoped copied workspace as the normal local product path:** it breaks continuous
  development in the user's working tree and loses native Git state.
- **Managed worktree for every local conversation:** it hides current uncommitted work and imposes
  branch, environment, integration, and cleanup costs on ordinary use.
- **Absolute path as RepositoryIdentity:** it identifies a location, fails on moves, and treats
  linked worktrees as unrelated repositories.
- **Remote or commit history as identity authority:** independent clones and forks can share these
  descriptors without sharing local state or ownership.
- **Repository-only Conversation binding with automatic workspace selection:** it can resume work
  in the wrong checkout and weakens crash-recovery guarantees.
- **Permanent immutable Conversation-to-workspace binding:** it cannot support an explicit,
  auditable move after a worktree is removed or development intentionally changes checkout.
- **Multiple coordinated writers in one WorkspaceBinding:** per-file checks do not prevent
  inconsistent cross-file plans or conflicting Git operations.
- **Repository-wide writer lock for all changes:** it unnecessarily blocks independent worktrees
  and conflates ordinary file isolation with the smaller set of shared Git operations.

## Deferred decisions

This ADR intentionally leaves the following unresolved:

- physical storage and discovery of RepositoryIdentity;
- the persisted representation of RepositoryDescriptor, ProjectScope, and WorkspaceBinding;
- the exact compatible-workspace validation used for explicit rebind;
- physical writer lease, safe-release timeout, and recovery-barrier representation;
- steering, cancel, interrupt, retry, and resume semantics;
- detailed Git operation classification and permission policy;
- user-facing diff and undo behavior;
- schema, migration, CLI, and implementation sequencing.

## Reference product behavior

The target interaction is informed by the public Claude Code behavior documented in:

- [How Claude Code works](https://code.claude.com/docs/en/how-claude-code-works)
- [Manage sessions](https://code.claude.com/docs/en/sessions)
- [Run parallel sessions with worktrees](https://code.claude.com/docs/en/worktrees)

These references establish observable product behavior only. They do not establish Claude Code's
internal identity, persistence, locking, or recovery implementation.
