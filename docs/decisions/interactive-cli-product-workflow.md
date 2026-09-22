# ADR: Interactive CLI, Conversation Coordination, and Product Workflow

Date: 2026-09-20
Status: **Accepted architecture decision; implementation inactive**

## Context

The accepted product architecture now distinguishes:

```text
RepositoryIdentity
└── ProjectScope
    └── WorkspaceBinding

Conversation
└── Turn
    └── RuntimeExecution
        └── ModelRequest*
```

It also defines supporting durable product operations and records, including PermissionRequest,
PermissionDecision, CodeRewindOperation, workspace rebind, code checkpoints, instruction and
context manifests, and optional governed Memory operations. These supporting objects do not create
a persistent Task or a second execution hierarchy.

The current implementation still exposes one-shot `run`, `resume`, `interrupt`, inspection, and
replay commands around the old `Session`. That `Session` combines one initial request, Runtime FSM,
messages, workspace, pending calls, and execution outcome. It remains an implementation fact and
is interpreted as the current RuntimeExecution, not as the target long-lived Conversation or as a
reason to introduce another Session manager.

This ADR closes the final horizontal product-architecture cluster by composing the decisions in:

- [`project-repository-workspace-conversation.md`](./project-repository-workspace-conversation.md);
- [`conversation-turn-runtime-execution-lifecycle.md`](./conversation-turn-runtime-execution-lifecycle.md);
- [`tool-capability-command-permission.md`](./tool-capability-command-permission.md);
- [`diff-code-checkpoint-undo.md`](./diff-code-checkpoint-undo.md);
- [`project-instructions-repository-rules.md`](./project-instructions-repository-rules.md);
- [`context-composition-compaction.md`](./context-composition-compaction.md); and
- [`memory-product-positioning.md`](./memory-product-positioning.md).

This ADR defines target product behavior only. It does not activate implementation, change the
database or public Runtime IPC schema, define a migration, modify an implementation plan or
roadmap, or add a background supervisor.

## Decision

### 1. Application coordination is thin and stateless with respect to durable authority

The application layer uses:

```text
thin ConversationApplicationCoordinator
    +
use-case handlers
```

The coordinator may:

- dispatch product use cases;
- establish application transaction boundaries;
- load and validate versioned durable authority;
- select the appropriate use-case handler;
- call the Runtime, workspace, instruction, context, permission, code-change, and history
  subsystems; and
- translate domain outcomes into attachment-neutral product responses.

The coordinator has no independent durable authority. It must not:

- retain an in-memory Conversation as truth;
- directly mutate the Runtime FSM;
- execute a ToolInvocation;
- decide PermissionPolicy;
- act as filesystem or Git authority;
- own a WorkspaceBinding writer lease;
- construct a prompt that bypasses ContextManifest and Frozen ModelRequest; or
- infer durable state from what the CLI last rendered.

An implementation may use a facade object, command handlers, or functions. The architectural
boundary is the absence of hidden mutable authority, not the number of classes. Shared loading,
validation, error mapping, and application-transaction helpers may be centralized without moving
subsystem decisions into the coordinator.

### 2. Application services retain narrow authority and side-effect boundaries

The logical application boundaries are:

| Boundary | Responsibility | Authority and side-effect rule |
|---|---|---|
| Interactive attachment and local CommandRouter | input, dialogs, rendering, shortcuts, local command parsing | ephemeral UI state only |
| ConversationApplicationCoordinator | dispatch and transaction orchestration | no independent authority |
| Conversation store or repository | Conversation event order, transcript, Turn relationships, display metadata | Conversation semantic authority |
| RuntimeExecution service and kernel | create, attach, resume, request control, execute FSM transitions | Runtime journal and FSM remain execution authority |
| Workspace service | identity discovery, binding, compatibility, trust input, drift observation, writer coordination, recovery barrier | filesystem and Git remain current code authority; file mutations still pass through ToolHarness |
| Instruction resolver | discovery, activation, immutable snapshots, InstructionManifest | instruction authority defined by the instruction ADR |
| ContextComposer | deterministic per-ModelRequest projection and freeze | stateless projection; no product-state authority |
| Permission service | policy evaluation, PermissionRequest, PermissionDecision, grant lookup and revalidation | ToolHarness still performs final validation and execution |
| Code-change service | checkpoint, diff projections, CodeRewindOperation orchestration | every restore side effect passes through ToolHarness |
| History-search service | locate and return Conversation and Turn evidence references | no Memory copy and no transcript replacement |
| Optional Memory service | governed explicit UserPreference support | never a dependency of the core workflow |

These boundaries do not require one heavyweight class each. A simple use case may remain a small
handler as long as it preserves the authority and side-effect rules.

### 3. The CLI is an attachment, not a domain owner

One CLI attachment may retain only ephemeral presentation state, including:

- the selected Conversation ID or an ephemeral draft handle;
- a display reference to the current WorkspaceBinding;
- observed active Turn and RuntimeExecution IDs;
- input buffers, locally stashed drafts, command history, and rendering state;
- a permission, clarification, reconciliation, or selection dialog;
- transcript and diff viewport state; and
- an execution-host attachment token or other transport-local handle.

Observed IDs and states are caches. Every control action re-reads the durable state and validates
its expected version. Terminal closure, lease expiry, another attachment, or recovery may make the
rendered state stale.

Destroying an attachment does not delete, close, cancel, or archive a Conversation. The first
version may colocate the CLI attachment and foreground RuntimeExecution host in one process, but
that is an implementation arrangement rather than a domain identity relationship.

### 4. Normal startup creates only an ephemeral draft

The normal local entry point remains:

```text
cd repo
agent
```

It performs the following product discovery without creating a Conversation:

1. discover RepositoryIdentity descriptors;
2. determine ProjectScope;
3. bind the current concrete WorkspaceBinding;
4. evaluate workspace trust and discover instruction-source metadata under the existing trust
   rules;
5. inspect active coordinated writer authority, unfinished RuntimeExecutions, and workspace
   recovery barriers;
6. initialize the user-default permission mode; and
7. create an attachment-local empty draft and present resume or recovery suggestions.

Plain `agent` does not automatically attach to the most recent Conversation. Future explicit
startup shortcuts may continue the most recent compatible Conversation or open the resume picker,
but they do not change the default new-draft behavior.

The empty draft is not a durable Conversation. Exiting before an ordinary request is accepted
creates no empty Conversation record.

### 5. Turn admission is one logical durable publication boundary

The first InitialRequest from an empty draft, and every later InitialRequest in an idle
Conversation, enters a Turn admission use case. Durable acceptance publishes or references the
following as one logical unit:

- Conversation, when this is the draft's first accepted request;
- InitialRequest;
- Turn;
- RuntimeExecution;
- TurnStartCodeCheckpoint and its explicit coverage;
- base InstructionManifest and its immutable source snapshots;
- the immutable RuntimeExecution WorkspaceBinding; and
- effective permission mode and policy epoch.

Checkpoint and instruction artifacts may be prepared before publication, but they are not visible
product authority until the admission transaction references them. A failed publication exposes
no Conversation, open Turn, or RuntimeExecution. Unreferenced staged artifacts are not accepted
state and may later be collected according to storage policy.

The filesystem cannot participate in the same transaction as a durable store. The
TurnStartCodeCheckpoint therefore records its observation frontier, revisions, and coverage.
External changes between capture and publication are concurrent or unattributed drift, not
Agent-controlled changes. Before any later write, the system still performs the accepted
WorkspaceBinding and per-resource revision validation.

Every code side effect occurs after successful checkpoint publication. First ContextManifest and
Frozen ModelRequest creation may occur after Turn acceptance. Context required-envelope overflow,
provider initialization failure, or another post-admission failure therefore produces a valid:

```text
accepted Turn
    → FAILED RuntimeExecution
```

It never leaves a Turn without a RuntimeExecution or a code-changing Turn without a published
TurnStartCodeCheckpoint.

### 6. Turn admission does not require unconditional writer authority

Read-only and Plan executions may be admitted without WorkspaceBinding writer authority. Creating
a Turn, reading code, building context, asking a clarification question, or producing a plan does
not reserve a writer merely because the RuntimeExecution might later request a write-capable tool.

Before a RuntimeExecution crosses into a path that can dispatch a coordinated workspace write, it
must:

1. acquire writer authority for the exact WorkspaceBinding;
2. revalidate WorkspaceBinding identity and compatibility;
3. revalidate the relevant workspace and file revisions;
4. confirm that no recovery barrier blocks the workspace; and
5. satisfy the permission, mode, sandbox, containment, and ToolHarness preconditions already
   defined by prior ADRs.

A write-capable ToolInvocation may not execute first and acquire authority afterward. If a
PermissionRequest is created before authority is acquired, approval remains only authorization;
writer acquisition and final revalidation are still mandatory before dispatch.

Writer authority is an explicit, releasable coordination capability. It is not an intrinsic
property held from RuntimeExecution creation until terminal outcome.

### 7. Writer authority and recovery barrier have distinct lifecycles

The first-version safe-release model is:

| Runtime condition | Writer-authority treatment |
|---|---|
| RUNNING while performing or immediately coordinating writes | acquire and retain as required by the active write path |
| RUNNING on a purely read-only or Plan path | no writer authority required |
| WAITING_PERMISSION | a short foreground wait may retain already-acquired authority; a durable stable detach or long wait may release it; continuation reacquires and revalidates before a write |
| WAITING_USER_INPUT | a short foreground wait may retain already-acquired authority; a durable stable detach or long wait may release it; continuation reacquires and validates drift |
| INTERRUPTED | release writer authority after the stable checkpoint |
| WAITING_RECONCILIATION | normal process or writer lease may expire, but a durable workspace recovery barrier continues blocking every coordinated writer until reconciliation completes |
| terminal RuntimeExecution | release writer authority; unresolved recovery barriers cannot be hidden by a terminal projection |

`writer authority` and `recovery barrier` are separate concepts. Writer authority grants one
cooperating operation the right to perform coordinated writes now. A recovery barrier records that
the workspace cannot safely admit another coordinated writer because an earlier effect remains
unknown. Process death or lease expiry can end the former and must not silently clear the latter.

The timeout, detach threshold, and physical representation of safe release are implementation
parameters. Each release is durable and occurs only at a side-effect-safe boundary.

### 8. Ordinary input is routed by durable execution state

The local input router first handles registered application commands. Remaining ordinary text is
classified from the current durable state:

| Durable product state | Ordinary text meaning |
|---|---|
| Empty draft or idle Conversation | `InitialRequest`, starting a new Turn |
| RUNNING | ordered `SteeringInput` in the current Turn |
| WAITING_USER_INPUT | matching `UserReply` in the current Turn |
| WAITING_PERMISSION | `SteeringInput`; it does not resolve the permission request |
| WAITING_RECONCILIATION | not a resolver; the UI rejects or locally stashes the draft without creating a reconciliation outcome |
| INTERRUPTED | no implicit input routing; the user first selects resume execution, cancel, or inspect |
| Conversation whose last Turn is terminal | `InitialRequest`, starting the next Turn |

SteeringInput is durably committed immediately and consumed only at a safe boundary. Multiple
inputs are ordered by durable sequence. The durable steering/finalization race decides whether an
input joins the open Turn or starts the next Turn.

SteeringInput never changes, cancels, or rewrites a committed ToolInvocation. Ordinary natural
language such as `yes` in WAITING_PERMISSION is not a PermissionDecision. Permission and
reconciliation outcomes use their typed resolver controls.

### 9. Slash and control commands are local application operations

The local CommandRouter recognizes registered slash commands before ordinary model input. The V1
core command surface is:

- `/new`;
- `/resume`;
- `/status`;
- `/diff`;
- `/undo`;
- `/stop`;
- `/mode`;
- `/permissions`;
- `/exit`; and
- `/help`.

These commands are not sent to the model to decide whether the requested application operation
should occur. Unknown slash commands produce a local error and are not silently converted to model
input. Literal slash-prefixed user content must have an explicit escape mechanism; the exact
keystroke or syntax is a CLI implementation UX decision.

Read-only commands such as `/status` and an observation-only `/diff` may run while an execution is
active subject to a clearly identified observation frontier. Commands that change lifecycle,
mode, permission, or code state use their structured product operations and state preconditions.

`/permissions` is the local permission inspection and management surface. At minimum it shows the
effective mode, applicable durable grants or command rules, and any pending PermissionRequest. An
allow, deny, grant, revoke, or scope change is submitted as its typed permission/configuration use
case; opening `/permissions` never approves or executes an invocation.

`/help` renders locally registered commands, current-state availability, and literal-input help
from application metadata. It does not call the model, append transcript content, or create a
durable product event merely because help was viewed.

### 10. `/new` creates a new draft without changing code

When the current Conversation is idle:

```text
/new
    → detach current Conversation
    → create attachment-local empty draft
    → retain RepositoryIdentity, ProjectScope, and WorkspaceBinding
    → retain the current working tree
    → leave the old Conversation resumable
```

The new draft initializes from the user-default mode, which is Manual unless explicitly configured
otherwise. It does not inherit a temporary or durable mode selected in the old Conversation.

When a RuntimeExecution is active and V1 has no background host, `/new` offers only:

- stop or cancel the current execution, complete any required reconciliation, and then create the
  draft; or
- stay in the current Conversation.

It does not silently abandon, detach, or hide the active execution.

### 11. `/resume` restores a Conversation before offering execution recovery

`/resume` always means `resume_conversation`. Its product flow is:

```text
Conversation picker
    → select Conversation
    → load transcript and product metadata
    → validate RepositoryIdentity and ProjectScope
    → validate the saved WorkspaceBinding
    → attach for inspection
```

If the currently attached Conversation has an active RuntimeExecution and V1 has no background
host, `/resume` uses the same stop-or-stay gate as `/new`. It cannot switch the attachment and leave
the current execution running without a host.

If the original WorkspaceBinding is missing or incompatible, the transcript remains inspectable.
Coding cannot continue until the user explicitly selects and confirms a compatible workspace
rebind between Turns. The current cwd never silently replaces the saved binding.

If the selected Conversation has a recoverable RuntimeExecution, the UI offers:

- Resume execution;
- Stop or cancel execution; or
- Inspect only.

Inspect-only leaves the open Turn intact and therefore cannot start another Turn in that
Conversation. WAITING_PERMISSION, WAITING_USER_INPUT, WAITING_RECONCILIATION, and INTERRUPTED each
receive resolver-specific UI. `/resume` never automatically dispatches an unfinished
ToolInvocation or interprets an unknown effect as safe to retry.

### 12. Permission mode has user, Conversation, and policy-epoch scopes

Mode ownership is:

```text
user default
    +
durable Conversation effective mode
    +
RuntimeExecution / PermissionRequest policy epoch
```

The user default is Manual. A resumed Conversation restores its durable effective mode, and the
picker and `/status` display that mode prominently. `/new` uses the user default rather than the
old Conversation mode.

A mode change is a structured application control or configuration operation. It:

- is durably audited;
- becomes effective only at a safe boundary;
- does not mutate a committed ToolInvocation;
- does not automatically approve a pending PermissionRequest; and
- cannot revive a write invocation rejected or blocked under Plan mode.

After a mode change, a blocked operation requires a newly committed invocation based on a later
model decision. A pending PermissionRequest still requires an explicit PermissionDecision and
final revalidation. Noninteractive mode remains primarily a headless or startup configuration and
need not participate in the normal interactive mode-cycle shortcut.

Permission UI submits decisions only. It never executes the invocation. The Runtime controller
persists request and decision state, PermissionPolicy computes authorization, and ToolHarness
performs the final validation and side effect.

### 13. `/diff` defaults to a layered Turn review

The default `/diff` view for the current or most recent Turn presents:

1. Agent-Controlled ChangeSet;
2. `OBSERVED_COMMAND_DELTA`;
3. concurrent or unattributed Turn Workspace Delta; and
4. checkpoint coverage and unsupported paths.

A separate view may show Current Workspace Diff. Neither view is reduced to `git diff HEAD`, and
temporal or command-window observation is never mislabeled as confirmed Agent authorship.

### 14. `/undo` is unavailable while a Turn remains open

V1 rejects `/undo` while the Conversation has an open Turn or active RuntimeExecution. The user
must first complete the Turn, cancel or stop it, and resolve every uncertain effect.

An accepted `/undo` request creates a durable CodeRewindOperation. It:

- does not create a Turn or call the model;
- does not execute `git reset` or rewrite Git history;
- acquires writer authority for the exact WorkspaceBinding;
- confirms that no recovery barrier blocks the workspace;
- validates every target before the first restore write;
- passes each restore through ToolHarness;
- records progress and supports reconciliation after host loss; and
- does not modify old Turn or RuntimeExecution history.

The coverage, revision-conflict, index-preservation, and no-partial-start guarantees remain those
of the diff and code-checkpoint ADR.

### 15. `/status`, title, and Conversation search expose metadata without creating authority

V1 `/status` reports at least:

- current empty draft or Conversation;
- open Turn, if any;
- RuntimeExecution state and wait reason;
- WorkspaceBinding;
- observed branch and HEAD, observation time, and drift state;
- writer authority and recovery barrier;
- effective permission mode;
- InstructionManifest status;
- pending permission, user-input, or reconciliation resolver;
- current or most recent Turn diff availability; and
- rewind availability and checkpoint coverage.

Detailed ContextManifest lineage may be added through a future `/context` surface.

The V1 Conversation title is a deterministic truncated excerpt of the first InitialRequest. It is
display metadata, may be non-unique, and is never identity authority. User rename and
model-generated titles are deferred.

V1 provides a searchable Conversation picker over at least title, first request,
RepositoryIdentity/ProjectScope, and state. Full `/history <query>` retrieval may follow the core
interactive flow, but Conversation history search remains product-prioritized over automatic
Memory extraction or ProjectExperience recall. Search results reference original Conversation,
Turn, and transcript evidence rather than copying it into Memory authority.

### 16. Stop, exit, and host loss have different meanings

During an active response, `Esc`, `Ctrl+C`, and `/stop` produce a durable `cancel_requested`
ControlRequest. The Runtime reaches a safe boundary, resolves any uncertain side effect, and only
then enters terminal `CANCELLED`.

Idle `/exit` detaches the UI and leaves the Conversation unchanged.

Active `/exit` in foreground-only V1 offers:

- Interrupt and exit;
- Cancel and exit; or
- Stay.

Interrupt and exit waits for a durable stable `INTERRUPTED` boundary and releases writer authority
before reporting a clean exit. If an in-flight effect cannot yet reach that boundary, the choice
remains pending until the effect settles or reconciliation becomes available. Cancel and exit
follows the normal cancellation and reconciliation rules; it cannot claim success merely because
the process is leaving.

SIGHUP, terminal closure, process kill, machine reboot, or foreground-host crash are host loss, not
user cancellation. The process makes a best-effort stable interruption when the signal and
platform permit it. If no such transition commits, lease expiry and the last durable Runtime and
tool journals drive recovery. Host loss never fabricates `CANCELLED`.

### 17. Foreground hosting does not change the domain model

V1 may use:

```text
CLI process = foreground RuntimeExecution host
```

It must not infer:

```text
CLI attachment = RuntimeExecution identity
```

A future supervisor, daemon, desktop host, or managed background facility may attach to the same
RuntimeExecution without changing Conversation, Turn, RuntimeExecution, workspace, input, or
recovery semantics. This ADR neither requires nor designs that facility.

### 18. End-to-end product flow preserves the accepted authorities

The normal interactive sequence is:

```text
startup discovery
    → ephemeral draft
    → Turn admission publication
    → ContextManifest + Frozen ModelRequest
    → model and durable ToolInvocation loop
    → structured permission / clarification / reconciliation as needed
    → controlled workspace effects through ToolHarness
    → terminal RuntimeExecution and Turn projection
    → read-only diff review or independent CodeRewindOperation
    → later Turn, /new, /resume, or UI detach
```

At no point does the coordinator, CLI renderer, ContextComposer, Summary, or optional Memory become
the authority for transcript, execution, code, instructions, tool results, or permission.

## Failure and recovery semantics

| Failure | Product result |
|---|---|
| Checkpoint, base instruction snapshot, or admission publication fails | request is not accepted; no visible Conversation/open Turn/RuntimeExecution is published |
| Context required envelope overflows after admission | RuntimeExecution fails explicitly; the accepted Turn remains in history |
| Provider outcome is unknown | retry uses the exact Frozen ModelRequest and a new attempt in the same RuntimeExecution |
| Permission prompt host crashes | durable PermissionRequest remains; resume shows the same resolver and revalidates before execution |
| Writer acquisition fails | no write executes; the product may wait, remain read-only, select an explicit worktree flow, or reach a stable interruption without changing the Turn identity |
| External drift occurs before write | revision or workspace revalidation fails; the old write intent does not execute |
| Tool outcome is unknown | enter WAITING_RECONCILIATION and retain the recovery barrier even after process lease expiry |
| User cancels during a write | record cancel intent; settle or reconcile the write before terminal CANCELLED |
| Instruction source drifts | the current RuntimeExecution keeps its immutable InstructionManifest and reports staleness under the instruction ADR |
| Workspace topology becomes incompatible | stop silent continuation; the Conversation may later continue through an explicit compatible state or new Turn |
| CodeRewindOperation host crashes | reconcile the operation's durable progress; do not alter the target Turn's history |
| CLI or terminal disappears | recover from the execution journal and lease; do not record user cancellation |
| Memory service is absent or unavailable | core startup, Conversation, Turn, Runtime, context, tools, diff, undo, and recovery continue unchanged |

## Architectural invariants

1. The CLI is an attachment and never a durable domain-state owner.
2. A Conversation survives Turn completion, CLI exit, and execution-host loss.
3. An empty draft is not a durable Conversation.
4. Each Conversation has at most one open Turn in V1.
5. One Turn has exactly one RuntimeExecution in V1.
6. A RuntimeExecution has one immutable WorkspaceBinding.
7. Turn admission publishes Conversation, InitialRequest, Turn, RuntimeExecution,
   TurnStartCodeCheckpoint, base InstructionManifest, WorkspaceBinding, and policy epoch as one
   logical durable boundary.
8. No code side effect precedes publication of the TurnStartCodeCheckpoint.
9. Read-only and Plan execution do not require writer authority merely to exist.
10. Every coordinated workspace write acquires writer authority and revalidates current
    preconditions before execution.
11. Writer authority and recovery barrier are separate capabilities with separate release rules.
12. Ordinary input semantics derive from durable Runtime state.
13. Slash commands, ControlRequests, PermissionDecisions, and reconciliation decisions are not
    interpreted by the model before taking application effect.
14. A committed ToolInvocation is immutable.
15. `/resume` restores a Conversation and never automatically causes a side effect.
16. `/undo` creates a CodeRewindOperation, not a Turn, RuntimeExecution, Task, or Git reset.
17. Host loss is not user cancellation.
18. Filesystem and Git remain current code and repository authority.
19. Optional Memory unavailability cannot block the core Coding Agent workflow.
20. The ConversationApplicationCoordinator has no independent durable authority.

## Consistency review with accepted ADRs

### Repository, ProjectScope, WorkspaceBinding, and writer coordination

The startup draft binds the current WorkspaceBinding without giving the draft durable Conversation
identity. Turn admission freezes the RuntimeExecution binding, while later Conversation rebind
remains a between-Turn audited operation. Missing workspaces still require explicit rebind and
never fall back silently to cwd.

This ADR **supersedes the earlier writer-authority simplification** that one write-capable
RuntimeExecution acquires authority for its whole execution until terminal or pause. The retained
invariants are one coordinated active writer per WorkspaceBinding, no repository-wide lock for
ordinary file changes, and mandatory external-drift and per-file preconditions. The refined rule
is capability-based acquisition before a write path, explicit safe release, and a separate durable
recovery barrier for unknown effects.

### Conversation, Turn, and RuntimeExecution lifecycle

The empty draft remains non-durable until the first accepted request. Logical Turn admission
preserves one open Turn and one RuntimeExecution without adding a Task or Turn FSM. Input routing
uses the previously accepted InitialRequest, SteeringInput, UserReply, PermissionDecision, and
ControlRequest taxonomy. `/new`, `/resume`, cancellation, interruption, and host loss retain their
distinct lifecycle meanings.

The safe-release rule refines the old first-version assumption that a pending write-capable
permission request always retains writer authority. A short foreground wait may retain authority,
but stable detach or long wait may release it; execution must reacquire and revalidate before
dispatch. WAITING_RECONCILIATION continues to block coordinated writers through the separate
recovery barrier.

### Tool capability, command execution, and permission

Permission UI still submits a typed PermissionDecision and cannot execute a tool. Permission grant,
mode, and policy epoch do not waive writer, revision, sandbox, containment, or ToolHarness checks.
Mode changes do not rewrite or revive a committed invocation. ToolHarness remains the only real
side-effect gateway.

### Diff, CodeCheckpoint, and undo

Turn admission publishes the TurnStartCodeCheckpoint before any code side effect. The layered
`/diff` view maps directly to the three accepted diff authorities. `/undo` is now explicitly
unavailable during an open Turn and otherwise remains a separate, deterministic,
precondition-guarded CodeRewindOperation. User dirty and staged state retain the prior guarantees.

### Project instructions

Admission includes the base InstructionManifest required at Turn start. RuntimeExecution resume
continues to use its immutable instruction epoch. Path-aware activation, exact approval for
instruction-source changes, trust, and capability separation are unchanged.

### Context composition and compaction

First ContextManifest and Frozen ModelRequest follow successful admission and are committed before
the provider call. A post-admission composition failure may fail the RuntimeExecution without
creating a half Turn. The coordinator supplies versioned product inputs but cannot bypass the
stateless ContextComposer or ContextManifest. Transcript and Summary authority remain unchanged.

### Memory product positioning

The searchable Conversation picker uses original Conversation and Turn evidence. It does not
create Memory records. Memory remains optional M2-lite, has no required-context reservation, and
is absent from every core startup, execution, resume, diff, undo, and recovery dependency.

No consistency issue requires changing Conversation, Turn, RuntimeExecution, ModelRequest,
RepositoryIdentity, ProjectScope, or WorkspaceBinding relationships.

## Half-Turn and God-object review

The admission publication boundary prevents the following invalid visible states:

- accepted InitialRequest without a Turn;
- open Turn without a RuntimeExecution;
- code-changing accepted Turn without a TurnStartCodeCheckpoint;
- RuntimeExecution without an immutable WorkspaceBinding; and
- accepted execution without a base InstructionManifest or permission-policy epoch.

Failure before publication leaves only an attachment-local draft and unreferenced staged evidence.
Failure after publication is a normal RuntimeExecution outcome and remains auditable.

The coordinator avoids becoming a new Session God Object because it owns no durable aggregate,
Runtime FSM, tool executor, writer lease, prompt state, policy authority, or filesystem state. The
coordinator's methods are application use cases over independently versioned authorities. A future
implementation that caches one mutable Conversation/Runtime/workspace graph inside the
coordinator would violate this ADR even if it retained the coordinator name.

## Remaining decisions and closure

No unresolved product or domain-architecture issue blocks ADR consolidation and implementation
roadmap design. The remaining choices are representation, operational policy, or UI detail:

- database tables, events, migrations, indexes, and transaction implementation;
- physical staging, content-addressed storage, orphan collection, and retention;
- exact CLI rendering toolkit, keybindings, literal slash escape syntax, and startup flag names;
- writer safe-release timeout and lease-renewal parameters;
- exact typed representation of temporary writer contention;
- platform-specific signal handling and best-effort shutdown timing;
- title truncation length and display formatting;
- full `/history` indexing, ranking, and query syntax;
- `/context`, instruction-inspection, rename, and advanced audit UX; and
- any future background supervisor or managed process host.

Each deferred decision must preserve the authority, publication, immutable binding, input routing,
writer/recovery, permission, ToolHarness, and host-loss invariants above. This closes the final
horizontal product-architecture cluster; it does not activate implementation.

## Consequences

### Positive

- The accepted ADRs form one end-to-end interactive product workflow without adding a Task or
  God-object Session.
- Startup, resume, cancellation, host loss, permission, diff, and undo have distinct user-visible
  and durable meanings.
- Read-only work does not acquire a writer lease unnecessarily, while every real write retains
  final writer, drift, permission, and ToolHarness validation.
- Recovery barriers survive process lease loss and prevent unknown side effects from being hidden
  by concurrency.
- CLI, desktop, headless, or future background hosts can share the same domain model.
- Core Coding Agent behavior remains independent of Memory availability.

### Negative and risks

- Logical Turn admission spans durable records and externally mutable filesystem observations, so
  it needs explicit staging, versioning, and failure handling.
- Safe writer release improves concurrency but can force expensive rereads and invalidate a pending
  plan after drift.
- Resolver-specific input routing and resume UI are more complex than a single prompt loop.
- Explicit recovery and missing-workspace confirmation add friction compared with permissive auto
  resume behavior.
- Layered diff and conditional undo require clear coverage and provenance presentation.
- A thin coordinator boundary requires ongoing review because application facades naturally
  accumulate unrelated workflow logic.

## Alternatives rejected

- **A durable SessionManager or ConversationManager aggregate:** it recreates mixed transcript,
  Runtime, workspace, context, and tool authority.
- **CLI-owned state:** it makes terminal loss equivalent to domain loss and prevents another host
  from recovering the execution.
- **Automatic resume on plain startup:** it can attach to the wrong work or expose an unresolved
  execution without an explicit user choice.
- **Create Conversation at CLI startup:** it persists empty history and conflicts with durable
  acceptance semantics.
- **Create Turn before checkpoint or execution publication:** it permits half-Turn states and code
  side effects without rewind evidence.
- **Require writer authority for every Turn:** it blocks read-only and Plan work unnecessarily.
- **Hold writer authority for every non-terminal execution:** it blocks long permission and
  clarification waits and confuses authority with execution identity.
- **Clear reconciliation hazard on lease expiry:** it permits another coordinated writer while an
  earlier side effect remains unknown.
- **Send slash commands or natural-language approval through the model:** it makes application
  control and authorization nondeterministic.
- **Automatically restore execution on `/resume`:** it can repeat unfinished or unknown-effect
  work when the user only wanted the transcript.
- **Implement `/undo` as Git reset or a new Agent Turn:** it can erase user state or turn a
  deterministic compensation into model behavior.
- **Treat host loss as cancellation:** it invents user intent and destroys recoverability.

## Reference product behavior

The interaction direction is informed by public Claude Code behavior:

- [Quickstart](https://code.claude.com/docs/en/quickstart) documents `cd project && claude`,
  interactive startup, explicit continue/resume entry points, and session commands;
- [Manage sessions](https://code.claude.com/docs/en/sessions) documents continuously saved
  conversations, a resume picker, and display naming;
- [Interactive mode](https://code.claude.com/docs/en/interactive-mode) documents interrupt keys,
  queued mid-run messages, local commands, and `/diff`;
- [Commands](https://code.claude.com/docs/en/commands) documents application-level command
  surfaces distinct from ordinary prompts;
- [Configure permissions](https://code.claude.com/docs/en/permissions) documents client-enforced
  permission modes and scoped decisions; and
- [Checkpointing](https://code.claude.com/docs/en/checkpointing) documents per-turn code
  checkpoints and the limits of command-generated change restoration.

These sources establish observable UX only. They do not establish Claude Code's internal
Conversation/Turn/Runtime identity, transaction model, durable input routing, writer coordination,
permission request binding, host-loss recovery, or checkpoint representation. Those boundaries are
defined by this project's accepted ADR set.
