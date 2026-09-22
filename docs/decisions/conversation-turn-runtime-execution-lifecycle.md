# ADR: Conversation, Turn, and RuntimeExecution Lifecycle

Date: 2026-09-19
Status: **Accepted architecture decision; implementation inactive**

## Context

The product is a local, interactive Coding Agent. Its product lifecycle must distinguish the
long-lived conversation a user returns to, one user-facing unit of work inside that conversation,
and the recoverable Agent loop that performs that work:

```text
Conversation
└── Turn
    └── RuntimeExecution
```

The existing Phase 1 `Session` combines an initial task, transcript messages, Runtime FSM,
workspace, retry state, checkpoint, and result. It is interpreted as the current implementation
of `RuntimeExecution`; it is not the target long-lived Conversation.

The workspace and repository relationships are governed by
[`project-repository-workspace-conversation.md`](./project-repository-workspace-conversation.md).
In particular, a Conversation has a default WorkspaceBinding, a RuntimeExecution has one
immutable WorkspaceBinding, and the first implementation coordinates at most one writer per
WorkspaceBinding.

The final interactive workflow and writer safe-release refinement are governed by
[`interactive-cli-product-workflow.md`](./interactive-cli-product-workflow.md). That later ADR
supersedes the initial assumption that every pending write-capable permission wait must retain
writer authority until it resolves.

This ADR defines the product lifecycle above that Runtime kernel. It supersedes lifecycle
assumptions in [`conversation-runtime-refactor-plan.md`](../conversation-runtime-refactor-plan.md),
including mandatory Conversation archive state, one-user-message-per-Turn semantics, and treating
product `/resume` as direct Runtime recovery. It does not activate that refactor, authorize a
schema change, or change current Runtime behavior.

## Decision

### 1. Conversation lifecycle

A Conversation is a durable, long-lived transcript container. It owns product conversation
identity and ordered Turns. It does not own a Runtime FSM.

A Conversation is created when its first ordinary user request is durably accepted. Starting the
interactive CLI without submitting a request may present a transient empty draft; that draft does
not require a durable Conversation identity. Durable acceptance establishes the Conversation,
first Turn, and RuntimeExecution as one logical lifecycle boundary. A failure before that boundary
rejects the request rather than leaving an accepted Turn without an execution.

A terminal Turn never closes its Conversation. The Conversation remains available after a Turn
completes, fails, or is cancelled, and a later ordinary request creates the next Turn.

The first version has no core `closed` or `archived` Conversation state. A future history view may
add archive metadata without changing execution semantics.

`/new` starts a new empty-context Conversation. It:

- keeps the current RepositoryIdentity and ProjectScope;
- uses the current WorkspaceBinding as its default binding;
- does not clean or reset the working tree;
- does not delete, close, or archive the previous Conversation;
- does not copy the previous transcript into the new Conversation.

If the current Conversation has a non-terminal RuntimeExecution and background execution is not
available, `/new` must require the user to choose between requesting cancellation of the current
response and remaining in the current Conversation. Selecting cancellation does not switch
Conversations until cancellation reaches a terminal outcome. Reconciliation requirements cannot
be bypassed by `/new`.

Exiting a CLI attachment does not close a Conversation. The consequences for an active
RuntimeExecution depend on the execution host and control cause, as defined below.

### 2. Turn boundary

A Turn is one user-facing request from acceptance through the terminal outcome of its associated
RuntimeExecution:

```text
initial request durably accepted
  → zero or more ordered SteeringInput or UserReply events
  → RuntimeExecution terminal outcome
  → Turn finalized by projection
```

A Turn may contain more than one model-visible user input. The first version permits at most one
non-terminal Turn in a Conversation.

A Turn has identity and ordering but no independent Runtime FSM. Its product state is projected
from its one RuntimeExecution:

| RuntimeExecution status | Turn projection |
|---|---|
| non-terminal running or waiting status | `open` |
| `COMPLETED` | `completed` |
| `FAILED` | `failed` |
| `CANCELLED` | `cancelled` |

Permission waits, clarification waits, reconciliation waits, retry waits, interruption, process
recovery, and host attachment changes do not create a new Turn and do not finalize the current
Turn.

### 3. User input and control taxonomy

The domain distinguishes at least five input and control meanings:

| Kind | Meaning | Model-visible user content | Creates a Turn | Resolves a wait |
|---|---|---:|---:|---|
| `InitialRequest` | First ordinary request for new work | yes | yes | no |
| `SteeringInput` | Unsolicited correction or added direction while the execution is running | yes | no | no |
| `UserReply` | Answer to a specific Agent clarification or information request | yes | no | `WAITING_USER_INPUT` only |
| `PermissionDecision` | Structured allow/deny decision for a specific permission request | no ordinary user message | no | `WAITING_PERMISSION` only |
| `ControlRequest` | Structured cancel or other execution-control intent | no ordinary user message | no | according to control semantics |

`PermissionDecision` and `ControlRequest` must not be represented as ordinary UserInput. An Agent
question that asks for product or implementation information is not a permission prompt. A user
answer to such a question is a `UserReply`, belongs to the current Turn, and is visible to the
model.

The first version permits at most one outstanding ordinary user-input request per
RuntimeExecution. The question or request must be durably committed before the execution enters
`WAITING_USER_INPUT`. A `UserReply` must correlate with that outstanding request. Unsolicited
content received while waiting remains `SteeringInput` unless the UI or protocol explicitly binds
it as the reply; it cannot silently resolve the wait.

### 4. Steering

Steering uses immediate durable commit and safe-boundary consumption.

Every accepted input receives a durable sequence in the Conversation. That sequence is the
authority for ordering multiple inputs and for resolving a race between new input and Turn
finalization:

- an input committed before the Turn finalization event belongs to the current Turn;
- an ordinary request accepted after finalization creates the next Turn.

The RuntimeExecution consumes committed SteeringInput at a safe boundary and exposes it to the
next applicable model call in durable order. It must not acknowledge an input as accepted before
the durable commit succeeds.

A SteeringInput does not cancel, mutate, or replace an already committed ToolInvocation. Stopping
an action requires a separate ControlRequest. Tool invocation identity, arguments, attempt
history, and outcome remain auditable even when later user direction changes the execution's next
decision.

### 5. RuntimeExecution identity and terminal outcomes

A RuntimeExecution is one recoverable Agent loop for one Turn. The first-version invariant is:

```text
one Turn ↔ one RuntimeExecution
```

The following remain within the same RuntimeExecution identity when they continue the same user
request:

- model calls and provider attempts;
- retryable provider errors and provider fallback;
- tool calls and safe tool attempts;
- permission and user-input waits;
- side-effect reconciliation;
- interruption and crash recovery;
- foreground or background host attachment changes.

The terminal RuntimeExecution outcomes are:

- `COMPLETED`: the Runtime produced its normal final result;
- `FAILED`: the Runtime cannot complete the request under the current execution;
- `CANCELLED`: the user explicitly and permanently stopped this execution.

`CANCELLED` is not resumable. A later request to try again creates a new Turn and a new
RuntimeExecution. The new execution may carry `retry_of_execution_id` or
`follows_execution_id` association for audit and UI grouping. This association does not create a
persistent Task.

### 6. Cancellation and interruption

Esc, Ctrl+C while a response is active, and `/stop` express a cancel request. Cancellation is a
two-stage protocol:

1. durably commit a `ControlRequest` with cancel intent;
2. stop at a safe boundary, settle or reconcile in-flight effects, and only then commit the
   terminal `CANCELLED` outcome.

A cancel request is not itself a terminal transition. It does not prove that a running tool
stopped or that a side effect did not occur.

An unknown or non-idempotent side effect must be reconciled before `CANCELLED` can be committed.
The first version does not define an `ABANDONED` terminal outcome and does not permit cancellation
to bypass reconciliation. A later requirement to abandon an execution with unresolved effects
requires a separate architecture decision.

`INTERRUPTED` remains a non-terminal, recoverable Runtime state. It means the execution reached a
durable stable pause boundary with a known resume target. It may be used internally without the
first product version exposing a `/pause` command. An explicit `resume_execution` or cancel
request can resolve it.

A process crash, terminal loss, or machine reboot is not user cancellation. Recovery uses the
same RuntimeExecution identity and its committed checkpoint and call journal. A crash does not
need to have committed `INTERRUPTED`; recovery may instead discover an expired execution lease
and reconcile the last committed Runtime state.

### 7. Distinct non-terminal wait semantics

The domain distinguishes the following wait meanings even if a later Runtime state-machine
implementation decides to represent some of them with a shared enum plus a typed reason. Their
cause, resolver, resume rule, writer-authority treatment, and UI must remain distinguishable.

| Wait meaning | Entering cause | Primary allowed resolver | Resume semantics | Writer-authority semantics | UI presentation |
|---|---|---|---|---|---|
| `WAITING_PERMISSION` | A specific action requires a structured authorization decision before invocation | Matching `PermissionDecision`; cancel remains a separate ControlRequest | Allow or deny is recorded for that request; continuation remains in the same execution and revalidates action preconditions | A short foreground wait may retain already-acquired authority; a durable stable detach or long wait may release it; a later write must reacquire authority and revalidate drift | Show the requested action, risk/scope, and explicit allow/deny choices |
| `WAITING_USER_INPUT` | The Agent durably asked for missing information or a product/implementation choice | Matching `UserReply`; cancel remains a separate ControlRequest | The reply is appended to the current Turn as model-visible content; the same execution reacquires authority when needed, revalidates workspace facts, and continues with its next model decision | A short foreground wait may retain already-acquired authority; a durable stable detach or long wait may release it; the durable reply remains pending if required authority cannot yet be reacquired | Show the Agent question and a normal reply input, without permission language; after reply, show any wait to reacquire writer authority separately |
| `WAITING_RECONCILIATION` | A committed or in-flight action may have produced an unknown side effect | Explicit reconciliation evidence or decision for the identified ToolInvocation | Record effect-applied or effect-not-applied evidence, then continue, safely retry, fail, or complete the pending cancellation according to that resolution | Preserve a durable reconciliation hold that blocks another coordinated writer on the WorkspaceBinding; process lease expiry does not clear the hazard | Show the uncertain action, known evidence, and explicit reconciliation choices; do not present ordinary resume |
| `INTERRUPTED` | The Runtime reached a durable stable pause boundary with no unresolved in-flight effect | Explicit `resume_execution` or cancel ControlRequest | Reacquire writer authority, validate the immutable WorkspaceBinding and current workspace facts, then resume the saved Runtime target | Release active writer authority after the stable checkpoint; reacquisition and drift validation are mandatory before resuming | Show paused/interrupted status and explicit resume or cancel actions |

`WAITING_USER_INPUT` does not break the Conversation → Turn → RuntimeExecution model. It introduces
no new top-level lifecycle identity. It is a typed non-terminal condition of the same execution,
and its matching UserReply is another ordered input in the same Turn.

### 8. Retry

Automatic recovery that continues the same accepted request retains the RuntimeExecution
identity:

- provider or network transient retry;
- provider fallback;
- safe retry of the same logical ToolInvocation;
- reuse of a committed model or tool result;
- crash recovery and reconciliation.

Tool attempts remain subordinate to their logical ToolInvocation. A later model decision to call
the same tool again is a new ToolInvocation, not another attempt of the prior invocation.

Once a RuntimeExecution is `FAILED` or `CANCELLED`, a user-initiated “try again” creates a new
Turn and RuntimeExecution. Terminal execution history is immutable.

### 9. Resume

Product `/resume` means `resume_conversation`. It restores the selected Conversation for reading
and further interaction. It does not automatically resume a RuntimeExecution or execute an
unfinished ToolInvocation.

If the Conversation contains a recoverable RuntimeExecution, the product presents these distinct
choices:

- resume the same execution;
- abandon/cancel the execution, represented in the domain as a cancellation request;
- inspect the Conversation without executing.

Unknown side effects must be reconciled before execution resume or terminal cancellation. Inspect
only does not resolve, cancel, or move the execution.

`resume_execution` is a separate operation. It applies only to a recoverable non-terminal
RuntimeExecution, preserves the original Turn and immutable WorkspaceBinding, reacquires writer
authority where necessary, and performs the recovery validations required by the wait or
interruption reason.

The exact historical model-input and future-request composition rules for this recovery path are
governed by [`context-composition-compaction.md`](./context-composition-compaction.md). Retrying one
committed provider request reuses its frozen input, while a new model decision receives a new
ModelRequest and ContextManifest.

Conversation resume never substitutes the current process directory for a missing workspace and
never turns Conversation rebind into execution resume. Those constraints remain governed by the
workspace ADR.

### 10. UI attachment and execution host

UI attachment and RuntimeExecution host are separate architectural concepts. Detaching a UI does
not inherently cancel an execution, and terminating an execution does not close its Conversation.

The first version may use the interactive CLI process as the foreground execution host. This ADR
does not require a daemon, supervisor, background execution service, or detach implementation.

While no background host exists:

- an idle CLI may exit without changing Conversation lifecycle;
- Esc, active-response Ctrl+C, and `/stop` request cancellation;
- loss of the foreground process is handled as interruption/crash recovery rather than a durable
  user cancellation unless an explicit cancel request was already committed;
- `/new` during an active execution offers cancellation or staying in the current Conversation.

A future background or detach facility can preserve the same RuntimeExecution identity and add a
new execution host or attachment without changing this domain model.

## Architectural invariants

1. A Conversation is a durable transcript container and has no Runtime FSM.
2. A Conversation is created on durable acceptance of its first InitialRequest.
3. A terminal Turn does not close its Conversation.
4. The first version has no Conversation close/archive state.
5. `/new` creates a fresh empty-context Conversation and does not reset the working tree.
6. A Conversation has at most one non-terminal Turn in the first version.
7. A Turn begins with InitialRequest acceptance and ends with its RuntimeExecution terminal
   outcome.
8. A Turn has no independent Runtime FSM; its status is projected from RuntimeExecution.
9. One Turn has exactly one RuntimeExecution in the first version.
10. InitialRequest, SteeringInput, UserReply, PermissionDecision, and ControlRequest have distinct
    domain meanings.
11. PermissionDecision and ControlRequest are not ordinary model-visible user messages.
12. Steering ordering and the steering/finalization race are governed by durable sequence.
13. Steering does not mutate or cancel a committed ToolInvocation.
14. `CANCELLED` is terminal and cannot be resumed.
15. A cancel request is durable intent and does not immediately imply `CANCELLED`.
16. Unknown side effects must be reconciled before cancellation becomes terminal.
17. `INTERRUPTED` is non-terminal and recoverable.
18. `WAITING_PERMISSION`, `WAITING_USER_INPUT`, `WAITING_RECONCILIATION`, and `INTERRUPTED` remain
    semantically distinguishable regardless of their eventual FSM representation.
19. Automatic retry and crash recovery preserve RuntimeExecution identity when they continue the
    same accepted request.
20. User retry after `FAILED` or `CANCELLED` creates a new Turn and RuntimeExecution.
21. Product `/resume` restores a Conversation and never automatically runs an unfinished tool.
22. UI attachment loss is not, by itself, durable user cancellation.
23. None of these lifecycle relationships requires a persistent Task.

## Consistency review

The accepted decisions and `WAITING_USER_INPUT` are internally consistent.

- **Clarification versus Turn identity:** a clarification question does not complete the request.
  The UserReply is ordered model-visible input in the existing Turn, so no second Turn or
  RuntimeExecution is needed.
- **Clarification versus steering:** both are user content, but only UserReply references and
  resolves a durable outstanding question. Steering changes future direction without satisfying
  that request implicitly.
- **Clarification versus permission:** permission is a structured policy decision for a specific
  action. A product or implementation answer is ordinary model-visible content. Keeping them
  distinct prevents prose from silently authorizing a side effect.
- **Cancellation versus reconciliation:** cancellation records the user's intent to stop;
  reconciliation establishes what already happened. Requiring reconciliation before terminal
  cancellation preserves both facts.
- **Conversation continuation versus execution recovery:** terminal failure or cancellation ends
  one Turn but leaves the Conversation open. Recoverable waits and interruption remain within the
  current Turn.
- **Stable pause versus writer authority:** only a durable, side-effect-safe boundary may release
  active writer authority. A reconciliation hazard continues to block coordinated writers even
  without a live host.
- **Foreground CLI versus attachment principle:** the first implementation may combine UI and host
  in one process while the domain keeps their identities separate. A future host can be added
  without changing Conversation, Turn, or RuntimeExecution identity.
- **Retry versus one-to-one identity:** provider/tool attempts and crash recovery are internal to
  one RuntimeExecution. A user retry after a terminal result is a new Turn, so one-to-one remains
  intact.

No accepted or added decision requires replacing the Conversation → Turn → RuntimeExecution
model. No persistent Task, multi-execution Turn, or Conversation Runtime FSM is needed.

## Consequences

### Positive

- Product continuation and exact execution recovery have separate names and safety rules.
- Running corrections and clarification replies remain inside the user request they refine.
- Permission, user input, cancellation, and reconciliation cannot silently impersonate one
  another.
- Terminal execution history remains immutable while a Conversation stays useful after failure
  or cancellation.
- Existing checkpoint, model/tool attempt, retry, lease, and reconciliation capabilities remain
  execution-scoped.
- Foreground-only delivery remains possible without embedding that limitation in the domain
  model.

### Negative and risks

- Immediate durable steering requires a well-defined ordering boundary before execution
  finalization.
- A long-running committed tool may delay consumption of SteeringInput.
- `CANCELLED` may take time to reach because in-flight effects must settle or be reconciled.
- `WAITING_USER_INPUT` can release writer authority, so continuation may fail validation after
  another writer or external process changes the workspace.
- Retaining authority during a short permission wait reduces churn, while safe release can require
  expensive drift validation before the invocation proceeds.
- A reconciliation hold can block later write-capable work until the uncertain effect is resolved.
- Users may perceive a retry as the same request even though a retry after terminal failure or
  cancellation creates a new Turn; UI grouping may be needed later.

## Alternatives rejected for the first-version model

- **One user message per Turn:** it makes steering and clarification split one execution across
  multiple Turns and weakens the one-to-one lifecycle boundary.
- **Multiple RuntimeExecution attempts inside one Turn:** it requires a second attempt-selection
  lifecycle above the existing Runtime recovery machinery without a demonstrated product need.
- **Treat every wait as `INTERRUPTED`:** it loses resolver type, permission safety, clarification
  content, and uncertain-side-effect handling.
- **Represent permission and control as user messages:** it lets unstructured prose become policy
  authority and makes audit ambiguous.
- **Make cancel an alias for recoverable interrupt:** it allows a permanently stopped execution to
  be resumed and conflicts with user intent.
- **Commit `CANCELLED` immediately on keypress:** it can report a terminal outcome while a tool is
  still running or its side effect remains unknown.
- **Automatically resume the last execution on `/resume`:** it can restart work when the user only
  wanted the transcript and is unsafe around unknown effects or workspace drift.
- **Require a background supervisor now:** it expands implementation scope without changing the
  accepted lifecycle model.
- **Add a persistent Task for retries or clarification:** Conversation, Turn, RuntimeExecution,
  typed input events, and execution associations already express the required behavior.

## Scope and deferred representation decisions

This ADR intentionally does not define:

- database tables, migrations, indexes, or serialized event schemas;
- whether every wait meaning becomes a separate Runtime FSM enum or uses a typed wait reason;
- CLI rendering, literal slash escaping, and command details beyond the accepted core surface;
- a user-facing `/pause` command;
- a daemon, supervisor, background execution service, or attachment protocol;
- precise OS signal mapping for every terminal and platform;
- automatic timeout policy or retry budgets;
- an `ABANDONED` terminal outcome;
- implementation sequencing or milestone planning.

These representation and delivery choices must preserve the invariants and resolver boundaries in
this ADR. None is currently known to require a change to the three-entity product model.

## Reference product behavior

The interaction model is informed by publicly documented Claude Code behavior:

- [How Claude Code works](https://code.claude.com/docs/en/how-claude-code-works)
- [Manage sessions](https://code.claude.com/docs/en/sessions)
- [Commands](https://code.claude.com/docs/en/commands)
- [Manage background sessions](https://code.claude.com/docs/en/agent-view)

These sources establish observable interaction behavior only. They do not establish Claude Code's
internal Turn identity, Runtime FSM, persistence ordering, cancellation protocol, or recovery
implementation.
