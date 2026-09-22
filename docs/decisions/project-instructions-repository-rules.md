# ADR: Project Instructions and Repository Rules

Date: 2026-09-20
Status: **Accepted architecture decision; implementation inactive**

## Context

The product is a local, interactive Coding Agent that works in a concrete WorkspaceBinding while
preserving a long-lived Conversation and one recoverable RuntimeExecution per Turn. Repository and
project instructions must let the Agent follow durable conventions, build commands, architecture
constraints, and path-specific rules without turning repository prose into executable policy or
permission authority.

The accepted product model remains:

```text
RepositoryIdentity
└── ProjectScope
    └── WorkspaceBinding

Conversation
└── Turn
    └── RuntimeExecution
```

The current implementation has a fixed system prompt, task/runtime and repository Context
sections, a bounded repository snapshot, and a deterministic Context manifest. It does not yet
discover repository instruction files, resolve hierarchical rules, freeze instruction revisions,
or record an instruction-specific manifest. These remain implementation facts; this ADR does not
activate a source, database, schema, CLI, or Runtime change.

This decision builds on:

- [`project-repository-workspace-conversation.md`](./project-repository-workspace-conversation.md),
  which defines RepositoryIdentity, ProjectScope, WorkspaceBinding, explicit Conversation rebind,
  external drift, and writer authority;
- [`conversation-turn-runtime-execution-lifecycle.md`](./conversation-turn-runtime-execution-lifecycle.md),
  which defines Turn input types, immutable RuntimeExecution identity, wait states, retry, and
  execution recovery;
- [`tool-capability-command-permission.md`](./tool-capability-command-permission.md), which defines
  capability claims, immutable ToolInvocation, permission modes, hard boundaries, and ToolHarness
  as the sole real-side-effect gateway; and
- [`diff-code-checkpoint-undo.md`](./diff-code-checkpoint-undo.md), which defines Turn-start code
  checkpoints, controlled mutation evidence, external drift, and code rewind.

Project instructions are explicit configuration and model context. They are not Memory, permission
rules, sandbox policy, a replacement for user intent, or a second Runtime state machine.

## Decision

### 1. Instruction records are subordinate configuration and audit evidence

The architecture uses three related concepts:

```text
InstructionSource
        +
InstructionScope
        ↓
InstructionManifest
```

`InstructionSource` identifies the origin and revision of explicit instruction content. A source
may be user-global, repository-shared, ProjectScope-specific, path-specific, workspace-local, or an
explicit ConversationInstruction. A repository filename is one possible source locator; it is not
the domain identity of an instruction.

`InstructionScope` describes where a source applies. Depending on source type, it may reference:

- RepositoryIdentity;
- ProjectScope;
- WorkspaceBinding;
- a repository-relative base directory;
- a path or file matcher;
- inheritance and path specificity; and
- any declared applicability category.

`InstructionManifest` is immutable audit evidence for the instruction set associated with a model
request. It records which sources were discovered, loaded, effective, overridden, conflicting,
inactive, or unavailable, and why.

These records are subordinate to existing identities. They do not add another top-level product
entity or change either accepted main model. In particular:

```text
RepositoryIdentity
└── ProjectScope
    └── WorkspaceBinding
        └── applicable InstructionSource / InstructionScope

RuntimeExecution
└── model request
    └── InstructionManifest reference
```

Instructions are not represented as Memory. Repository or project facts that are explicitly
declared in instruction sources retain their authority through their source, scope, revision, and
trust evidence.

### 2. Enforcement plane and instruction plane have different authority

The architecture has two separate planes.

The **enforcement plane** contains:

- System and Runtime policy;
- PermissionPolicy;
- sandbox policy and available isolation;
- ToolHarness invariants and preconditions;
- WorkspaceBinding writer authority;
- hard Runtime ceilings; and
- managed hard deny or unavailable capability decisions.

The **instruction plane** contains:

- current-Turn explicit user intent;
- explicit ConversationInstruction;
- repository, ProjectScope, path, and workspace-local instructions;
- user-global defaults; and
- historical or derived context.

An instruction can guide planning, code style, file selection, test conventions, or which operation
the model proposes. It can never:

- increase the ToolDefinition capability ceiling;
- grant a PermissionDecision or convert `ASK`, `DENY`, or `UNAVAILABLE` to `ALLOW`;
- bypass sandbox or containment requirements;
- bypass Plan or another enforceable read-only ceiling;
- acquire or bypass writer authority;
- weaken a ToolHarness validation or revision precondition;
- change an operation's recovery classification; or
- override System or Runtime hard policy.

If a behavior must hold regardless of model judgment, it belongs in the enforcement plane. Placing
the same sentence in a repository instruction does not provide enforcement.

### 3. The instruction plane has explicit authority and specificity rules

The model-visible instruction authority order is:

```text
current Turn explicit user intent
    > explicit ConversationInstruction
    > applicable path-specific Project or Workspace rules
    > repository or root Project rules
    > user-global defaults
    > historical or derived context
```

InitialRequest establishes current-Turn intent. Later SteeringInput and UserReply events modify
that intent by durable sequence. A later input supersedes an earlier one only where their meanings
conflict; unrelated intent remains additive.

Higher authority similarly overrides only a direct semantic conflict. It does not remove unrelated
additive rules. For example, a current request to run a specified test command may override the
repository's default test runner for that Turn while retaining unrelated repository formatting and
architecture rules.

Repository prose never overrides an explicit current user request. If the request is technically
impossible, unsafe under the enforcement plane, or inconsistent with observed project facts, the
Agent must report the conflict. It must not silently rewrite the user request using a lower-authority
repository instruction.

Within the Project and Workspace rule layers, a rule applicable to a more specific target path has
precedence over a conflicting broader rule. Specificity affects only overlapping semantics. A
nested formatter rule does not erase unrelated test or architecture rules inherited from the
repository root.

Workspace-local instruction content is bound to its WorkspaceBinding. It may supply a more specific
personal override at the same path scope, but it does not become a repository-shared rule and does
not follow the Conversation to another binding.

Historical and derived context may inform reasoning but has no instruction authority. A summary,
code-derived fact, tool observation, or historical pattern cannot override an explicit instruction.

### 4. ConversationInstruction is explicit and optional in the first product version

The domain allows an explicit `ConversationInstruction` with these semantics:

- only the user may explicitly establish it;
- it is never inferred from ordinary transcript content;
- it is not written into the repository;
- it does not cross `/new` into another Conversation;
- it may be explicitly revoked;
- current-Turn explicit intent has higher authority; and
- project defaults and user-global defaults have lower authority.

An ordinary InitialRequest, SteeringInput, or UserReply does not become a persistent
ConversationInstruction merely because it remains visible in transcript history. Establishing or
revoking one must be an auditable Conversation event.

This decision reserves the domain semantics. The first product version is not required to expose a
CLI or other product control for ConversationInstruction.

### 5. Instruction discovery is abstracted from filenames

Instruction discovery is performed by source providers that produce `InstructionSource` and
`InstructionScope` records. The domain model does not depend on one fixed filename or physical
storage scheme.

Potential providers include:

- user-global configuration;
- repository-root instruction files;
- ProjectScope instruction files;
- nested directory or path-scoped rules;
- workspace-local configuration; and
- explicit product configuration.

Concrete default filenames, configurable names, and exact file syntax are deferred. Whatever
syntax is selected must preserve the source, scope, revision, trust, and manifest semantics in this
ADR.

Discovery remains bounded:

- a Git workspace does not search above its RepositoryIdentity boundary for repository rules;
- a non-Git workspace stops at its explicit project or workspace root;
- a ProjectScope rooted in a repository subdirectory may inherit applicable repository-root rules
  and the ancestor chain down to the current working directory;
- discovery does not cross into an unrelated nested repository automatically; and
- a repository source cannot expand the discovery boundary before trust is established.

### 6. A Turn starts with a base instruction snapshot

Every accepted Turn establishes a base instruction snapshot before the first model request and
before any code side effect. Because one Turn has one RuntimeExecution in the first-version model,
the snapshot becomes that execution's initial InstructionManifest revision.

The base set includes applicable:

- user-global instructions;
- repository-root instructions;
- ProjectScope instructions;
- instructions in the current working directory's ancestor chain within the accepted boundary; and
- workspace-local sources.

Creating the base instruction snapshot and creating the TurnStartCodeCheckpoint are separate
authorities. Both occur before code side effects; neither substitutes for the other. The code
checkpoint records workspace content authority, while the instruction snapshot records the model's
instruction input and resolution evidence.

### 7. Nested instructions activate at safe boundaries

The loading model is:

```text
Turn-start base snapshot
        +
path-aware safe-boundary activation
```

Reading a file may discover an applicable nested instruction source. Before the next model request,
the controller resolves that source, durably creates a new InstructionManifest revision, and makes
the newly active instruction visible to the model.

A controlled write has a stronger admission rule: before any write side effect, the model decision
that requested the write must have been made under an InstructionManifest covering the target
path's applicable instruction chain.

If a committed write ToolInvocation targets a new scope that its model request's manifest did not
cover:

1. the invocation is not executed;
2. no PermissionRequest for that unexecutable invocation grants authority to proceed;
3. the invocation is durably resolved without side effect using a structured
   `instruction_scope_required` observation;
4. the newly discovered scope is activated at the safe boundary;
5. a new immutable InstructionManifest revision is committed; and
6. the next model call decides whether to submit a new ToolInvocation under the new instruction
   set.

The old invocation's name, arguments, digest, and history are never changed. It is not retained as
a deferred write that silently executes after scope activation. If the model still wants the write,
it commits a new ToolInvocation.

Instruction-scope admission is a controller and context precondition. It does not weaken or replace
the final ToolHarness validation, permission, sandbox, writer, or file-revision checks.

### 8. Loaded instruction content is frozen within a RuntimeExecution

Once an InstructionSource revision has been loaded into an execution manifest, its effective
content is immutable for that manifest revision.

If the source file is changed by the Agent, the user's IDE, a command, or another external process:

- the loaded source is marked stale against the current workspace;
- the effective manifest does not silently switch to the new content;
- already committed ToolInvocations retain their original manifest association;
- the next Turn rediscovers the current workspace revision by default; and
- the user may explicitly request an instruction refresh at a safe boundary.

An explicit refresh creates another immutable InstructionManifest revision and records why the
instruction epoch changed. A refresh is a dedicated product/control event. It is not a
PermissionDecision, does not by itself grant a tool capability, and is not silently inferred from
ordinary transcript content. If the user also supplies semantic direction, that direction retains
its normal InitialRequest, SteeringInput, or UserReply meaning.

Material instruction drift may require clarification through `WAITING_USER_INPUT`. Non-material
work can continue under the frozen manifest while the drift remains visible. There is no automatic
mid-model-request refresh.

### 9. Agent modification of an instruction source always requires exact approval

A recognized mutation of an instruction source is a persistent-behavior mutation. It is a special
case within controlled workspace file editing.

Even in `Accept Edits` mode, mutation of a recognized instruction or rule source evaluates to
`ASK` unless a stronger policy denies or makes it unavailable. This includes recognized:

- repository instruction files;
- ProjectScope instruction files;
- workspace-local instruction files; and
- rule or configuration sources that can affect future model behavior.

The PermissionRequest is bound to the exact normalized file-edit ToolInvocation digest, path,
before revision, and proposed mutation. Approval authorizes only that invocation after normal
revalidation. It does not approve future instruction edits and does not activate the new content in
the current execution.

This rule does not conflict with the `Accept Edits` mode defined by the permission ADR. That mode
makes precisely controlled workspace edits eligible for automatic approval; it does not guarantee
that every controlled edit is auto-approved. PermissionPolicy can classify a sensitive subset as
`ASK`, and explicit deny, hard boundaries, and invocation-specific policy remain stronger than a
mode default.

An approved instruction-file edit is still an ordinary ToolHarness-controlled file mutation for
change provenance and CodeCheckpoint purposes. Its before/after content evidence does not replace
the separate immutable instruction snapshot already associated with the execution.

If a CodeRewindOperation would restore an instruction source, the exact target set and revisions
must be visible in the user's reviewed rewind authorization. A digest-bound rewind request that
already discloses those exact targets can satisfy this exact-approval requirement; the product need
not present a duplicate prompt for the same mutation. A generic rewind request that does not reveal
the instruction-source target cannot auto-authorize it.

### 10. Discovery and activation are separate trust decisions

Before trust is established, repository instruction sources may be discovered sufficiently to
record:

- source path or locator;
- InstructionScope;
- revision or content hash; and
- content digest.

Their content is not injected into model context and cannot become effective instruction input.

Repository instruction trust is scoped to:

```text
RepositoryIdentity + ProjectScope
```

Trust means only that repository content in the accepted boundary may act as soft model
instruction. It does not mean the content is safe, correct, immutable, or endorsed. It grants no
tool capability or PermissionDecision and does not modify any enforcement-plane outcome.

Repository-shared trust may be reused among WorkspaceBindings that belong to the same
RepositoryIdentity and ProjectScope, including Git worktrees. Every binding still discovers and
hashes the concrete source revision it contains. Trust in one independent clone does not transfer
to another RepositoryIdentity merely because remotes or history match.

Workspace-local sources remain bound to one WorkspaceBinding. Their content and activation do not
follow a Conversation rebind automatically.

Trust classification is recorded in InstructionManifest. An inactive, untrusted source may appear
in audit metadata, but its instruction content is not part of the model-visible effective set.
Repository instructions that propose a dangerous command remain subject to ordinary command
normalization, permission, sandbox, and recovery policy after trust.

### 11. External instruction imports are unavailable in the first version

The first version does not support instruction imports or symlink targets outside the trusted
workspace or project boundary.

After canonical path resolution:

- a source fully contained in the accepted boundary may be handled as an ordinary scoped source;
- a source that escapes the boundary is recorded as `INACTIVE` or `UNAVAILABLE` with an explicit
  reason;
- discovery must not read and inject the escaped target as instruction content; and
- the product must not widen trust, workspace containment, read access, or tool capability merely
  to load it.

Future support for external imports requires a separate trust decision. It cannot be inferred from
repository instruction trust.

### 12. Conflict handling does not use file order as authority

The first version does not require repository authors to provide structured rule keys or
categories. Resolution uses available authority class, path specificity, source scope, source
revision, and explicit conflict evidence.

Compatible additive rules remain active together.

When rules contain a clear structured key, a higher-authority or more path-specific rule may
deterministically override a conflicting broader rule. The overridden rule remains in audit
evidence with the override relationship and reason.

When natural-language rules at the same authority and specificity conflict and the system cannot
reliably resolve them:

- all source records are retained;
- the conflict is recorded explicitly;
- neither last-loaded order nor filename order decides the winner;
- only decisions affected by the conflict are blocked; and
- a material conflict uses the existing `WAITING_USER_INPUT` lifecycle and a correlated UserReply.

Unrelated work can continue. Optional structured rule keys may be added later without changing
these authority rules.

### 13. Every model request identifies its InstructionManifest

Every model request must reference one exact, immutable InstructionManifest. The manifest records
at least:

- manifest ID and version;
- Conversation, Turn, and RuntimeExecution identity;
- source ID, type, and path or locator;
- source revision or hash;
- InstructionScope;
- authority and path specificity;
- trust classification;
- effective, inactive, unavailable, stale, or overridden disposition;
- inactive reason;
- conflict and override relationships;
- loaded durable sequence; and
- effective instruction-set digest.

The ContextManifest, as governed by
[`context-composition-compaction.md`](./context-composition-compaction.md), references the
InstructionManifest ID, version, and digest. It does not redefine instruction authority or infer
precedence from Context section order.

The manifest distinguishes audit-visible discovery metadata from model-visible content. An
inactive or untrusted source can be listed for audit without its content entering the model
request.

### 14. Effective instruction content has immutable reproduction authority

For every source revision whose content is actually loaded or effective, the product retains:

```text
content digest
    +
immutable content-addressed snapshot
```

A digest alone is not sufficient because it cannot reconstruct the text after source drift. The
snapshot is the content authority for instruction-related Runtime recovery, audit, reproduction,
and historical model-input reconstruction.

The full content of every discovered but inactive repository file need not be retained. Discovery
metadata and digest are sufficient until the source becomes loaded or effective.

Sensitive or local instruction snapshots require later decisions for encryption, redaction,
retention, export, and garbage collection. Those decisions may limit how long old executions remain
reproducible, but they do not change the identity or authority model in this ADR.

### 15. RuntimeExecution recovery preserves the original instruction epoch

The Runtime recovery checkpoint identifies the InstructionManifest revision associated with the
saved continuation boundary. The manifest in turn references immutable snapshots for the loaded
instruction content.

`resume_execution` therefore:

- keeps the original Turn and RuntimeExecution identity;
- keeps the RuntimeExecution's immutable WorkspaceBinding;
- restores the referenced InstructionManifest revision;
- verifies the manifest and content snapshots against their digests;
- reports current source drift without substituting current file contents; and
- applies existing workspace, writer-authority, pending-call, and reconciliation validation.

If an original manifest or required content snapshot is missing, corrupt, or fails digest
verification, automatic execution resume is rejected. The product may still expose inspect or
cancel choices, but it must not rebuild the old execution using current instruction files and claim
exact recovery. This is a failed resume precondition; it does not require a new top-level entity or
make the current repository content authoritative for the old execution.

Conversation resume has different semantics. When a terminal Conversation receives a new request,
the new Turn discovers the current WorkspaceBinding's latest applicable instruction revisions.

### 16. Command side effects cannot receive a complete instruction-compliance guarantee

For a command ToolInvocation, applicable project instructions can be resolved only from known
facts such as:

- the invocation's working directory;
- declared CapabilityClaims;
- known target paths or resources; and
- the InstructionManifest visible to the model that requested the command.

An arbitrary command, formatter, generator, package manager, or external process may affect files
outside its declared or observed set. Project/path instructions therefore do not prove that every
command side effect complied with every nested rule.

Unknown file effects remain governed by the existing permission, sandbox, command supervision,
Workspace Delta, command observation, and recovery architecture. An instruction such as “run this
formatter” cannot promote observed command changes into Agent-Controlled ChangeSet evidence or a
strong undo guarantee.

## Architectural invariants

1. InstructionSource, InstructionScope, and InstructionManifest are subordinate records, not new
   top-level product entities.
2. Instructions are explicit configuration or context evidence and are not Memory.
3. The enforcement plane always dominates the instruction plane.
4. No instruction grants capability, permission, sandbox access, writer authority, or a ToolHarness
   bypass.
5. Current-Turn explicit user intent has the highest authority in the model instruction plane.
6. Repository instructions never silently rewrite an explicit user request.
7. ConversationInstruction is explicit, auditable, revocable, and never inferred from transcript.
8. Every accepted Turn establishes a base instruction snapshot before model execution and code
   side effects.
9. A write cannot execute unless its originating model request's InstructionManifest covered the
   target path's applicable instruction chain.
10. Scope activation never mutates or later executes an uncovered committed ToolInvocation.
11. Loaded instruction content does not change silently within a RuntimeExecution.
12. Agent mutation of a recognized instruction source requires exact invocation approval even in
    Accept Edits mode.
13. Repository instruction trust activates only soft instruction context and grants no capability.
14. External instruction imports outside the accepted boundary are unavailable in the first
    version.
15. Natural-language conflict resolution never uses last-loaded or filename order as authority.
16. Every model request references one immutable InstructionManifest.
17. Loaded instruction content has a digest and immutable content-addressed snapshot.
18. RuntimeExecution resume restores the original instruction epoch or fails closed.
19. Conversation rebind does not carry WorkspaceBinding-local instructions into the new binding.
20. Project instructions do not prove complete compliance for unknown command side effects.

## Consistency review with prior ADRs

### Workspace, ProjectScope, and rebind

The instruction model uses, rather than changes, the accepted repository and workspace identities.

- Repository-shared sources use RepositoryIdentity and ProjectScope authority.
- Concrete revisions are rediscovered in each WorkspaceBinding.
- Workspace-local sources remain binding-specific.
- A RuntimeExecution never changes WorkspaceBinding when a new path scope activates.
- An explicit Conversation rebind occurs only between Turns and causes instruction rediscovery in
  the new binding.
- Two worktrees can share repository trust while retaining different instruction revisions and
  manifests.

External instruction-file changes are ordinary workspace drift plus instruction-specific stale
evidence. The product writer guard still does not claim to prevent IDE, shell, or other-process
changes.

### Conversation, Turn, and RuntimeExecution lifecycle

Instruction loading does not add a Turn or RuntimeExecution:

- the base manifest belongs to the accepted Turn's one RuntimeExecution;
- path activation creates another manifest revision within that execution;
- a material semantic conflict uses existing `WAITING_USER_INPUT` and UserReply;
- a PermissionDecision remains distinct from instruction refresh or trust;
- resume of the same execution restores its original manifest epoch; and
- a new Turn after Conversation resume uses current instruction revisions.

First-open repository trust should be resolved before the first ordinary request is durably
accepted where possible. Sources discovered later inside the already trusted boundary inherit that
soft-instruction trust and activate at safe boundaries. External imports remain unavailable, so the
first version does not require an instruction-specific Runtime wait state.

### Tool, permission, and Accept Edits semantics

The exact-approval requirement for instruction-source mutation is consistent with the permission
ADR:

- Accept Edits is an auto-approval candidate for controlled file edits, not an unconditional grant;
- PermissionPolicy may return `ASK` for a sensitive controlled edit;
- the request remains bound to the immutable normalized invocation digest;
- Plan mode, deny rules, containment, writer authority, and ToolHarness checks still apply; and
- approving the edit does not activate the resulting instruction content in the current execution.

Path-aware activation also preserves ToolInvocation immutability. The uncovered invocation is
resolved without side effect and retained in audit; the model must commit a new invocation after it
has seen the new manifest. No invocation arguments or approval digest are rewritten.

### Diff, CodeCheckpoint, and undo semantics

Instruction-source files remain ordinary workspace files for code-diff and checkpoint purposes.

- A controlled instruction edit appears in Agent-Controlled ChangeSet with normal before/after
  evidence.
- IDE or command changes may appear in Turn Workspace Delta or observed command evidence.
- InstructionManifest snapshot content remains a separate authority from CodeCheckpoint images.
- Code rewind can restore a file revision but never rewrites an old RuntimeExecution's instruction
  history or manifest.
- Rewinding an instruction file requires an exact, reviewable target authorization and the normal
  all-preflight-before-write behavior.
- Git topology drift and WorkspaceBinding compatibility rules remain unchanged.

No instruction decision turns CodeRewindOperation into a RuntimeExecution or merges Runtime
recovery checkpoints, code checkpoints, instruction snapshots, tool-call journal, or Git history.

### Resume reproduction

An exact instruction component of model input is recoverable because the Runtime checkpoint refers
to a manifest revision and the manifest refers to immutable content snapshots. Current workspace
files are drift evidence, not replacement authority. Missing or invalid snapshot content prevents
automatic resume instead of silently changing the execution's instructions.

Exact reproduction of an entire provider request also depends on the existing model request,
system-policy revision, transcript input, tool schema, repository/context facts, and adapter
records. This ADR establishes instruction-input authority; it does not claim those other components
are automatically complete merely because an InstructionManifest exists.

### Trust remains non-capability-granting

Instruction trust is intentionally separate from PermissionDecision and capability availability.
Trusting a repository cannot:

- make a hidden tool visible;
- turn `UNAVAILABLE`, `DENY`, or `ASK` into `ALLOW`;
- establish a repository command grant;
- relax Plan mode, sandbox, network, credential, or external-path constraints; or
- authorize the instruction source to modify itself.

The same dangerous operation receives the same PermissionPolicy and ToolHarness treatment whether
it was suggested by the user, repository instruction, model reasoning, or observed project text.

## Effect on the product domain model

The accepted main models remain unchanged:

```text
RepositoryIdentity
├── ProjectScope
└── WorkspaceBinding

Conversation
└── Turn
    └── RuntimeExecution
```

The added relationships are subordinate:

```text
InstructionSource
└── InstructionScope
    ├── RepositoryIdentity / ProjectScope
    └── optional WorkspaceBinding / path matcher

RuntimeExecution
└── ModelRequest
    └── InstructionManifest revision
        └── immutable content snapshots
```

ConversationInstruction, when a future product surface exposes it, is an auditable Conversation
configuration event. It does not add a Conversation FSM or persistent Task.

## Consequences

### Positive

- Repository conventions become explicit, scoped, and auditable without acquiring security
  authority.
- Monorepo rules can activate only when relevant rather than filling every request with unrelated
  instructions.
- A model cannot execute a controlled write in a newly discovered scope before seeing that scope's
  rules.
- Instruction drift is visible and reproducible instead of silently changing a recoverable
  execution.
- Accept Edits remains useful for ordinary code while instruction-source changes receive deliberate
  review.
- Worktrees can share project trust without pretending they have identical file revisions.
- Model-request audit can answer exactly which instruction content was supplied.
- Existing Runtime recovery, permission, ToolHarness, writer, checkpoint, and undo boundaries
  remain authoritative.

### Negative and risks

- Path-aware activation can add a model round trip when the model first attempts to write in an
  unseen nested scope.
- Natural-language conflict detection is incomplete without optional structured rule keys.
- Freezing a source within an execution can defer an IDE correction until explicit refresh or the
  next Turn.
- Repository trust can be misunderstood as a security endorsement unless the UI explains its
  narrow meaning.
- Branches and worktrees under one trusted RepositoryIdentity can contain materially different
  instruction revisions.
- Immutable content snapshots add storage, retention, privacy, and export obligations.
- Commands with unknown file effects cannot receive a complete path-rule compliance guarantee.
- Exact approval for instruction edits adds friction when a user intentionally asks the Agent to
  maintain project instructions.

## Alternatives rejected for the target model

- **Treat project instructions as Memory:** this loses explicit source, scope, revision, and
  project-configuration authority and makes repository correctness depend on a separate serving
  mechanism.
- **Treat repository instructions as policy:** prose is model context and cannot enforce permission,
  sandbox, writer, or ToolHarness boundaries.
- **Load every repository rule at startup:** this injects unrelated monorepo content, increases
  context cost, and broadens prompt-injection exposure.
- **Load only the startup directory chain:** it misses rules when one Turn crosses into another
  package or directory.
- **Reread all instructions before every model call:** this permits unaudited instruction changes
  inside one RuntimeExecution and weakens crash reproduction.
- **Execute an uncovered write after loading rules:** the original model decision did not see those
  rules, so deferred execution would silently change its semantic preconditions.
- **Let Accept Edits modify instruction files automatically:** instruction files alter persistent
  future behavior and require exact review.
- **Use last-loaded-wins for conflicts:** file order is not a reliable expression of semantic
  authority.
- **Store only content digests:** hashes prove identity but cannot reconstruct historical model
  input after source drift.
- **Let repository trust grant permission:** trust in soft instructions and authorization of real
  side effects are different decisions.
- **Automatically follow external imports:** repository content must not widen workspace or trust
  boundaries merely by naming another path.

## Deferred decisions

This ADR deliberately leaves unresolved:

- default instruction filenames and configurable filename syntax;
- concrete parsing and normalized-content rules;
- physical persistence schema and storage location;
- the first product UI for repository trust, instruction refresh, conflict inspection, or
  ConversationInstruction;
- optional structured rule key/category syntax;
- automatic semantic conflict-detection techniques;
- encryption, redaction, retention, export, and garbage collection for instruction snapshots;
- trust revocation and material source-set-change notification UX;
- support for external imports or symlink targets outside the trusted boundary;
- exact instruction snapshot retention required after a RuntimeExecution is no longer recoverable.

These decisions must preserve the authority, immutability, trust, ToolInvocation, recovery, and
audit invariants in this ADR. They do not authorize implementation work.

## Reference product behavior

The interaction model is informed by publicly documented Claude Code behavior:

- project and user instruction files provide persistent model context;
- ancestor instructions load at startup and nested instructions can load as work enters a
  subdirectory;
- path-scoped rules can limit instruction applicability;
- multiple instruction sources are additive and more specific instructions commonly take
  precedence; and
- instructions shape model behavior while permission and sandbox mechanisms remain the enforcement
  boundary.

References:

- [How Claude remembers your project](https://code.claude.com/docs/en/memory)
- [Extend Claude Code](https://code.claude.com/docs/en/features-overview)
- [Configure permissions](https://code.claude.com/docs/en/permissions)

Claude Code's internal instruction identity, manifest persistence, exact recovery representation,
mid-execution refresh behavior, and conflict-resolution implementation are not publicly specified.
This ADR defines those boundaries for this project rather than treating prompt order as architecture.
