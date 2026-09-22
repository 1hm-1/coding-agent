# ADR: Tool Capability, Command Execution, and Permission UX

Date: 2026-09-19
Status: **Accepted architecture decision; implementation inactive**

## Context

The product target is a local, interactive Coding Agent that can inspect and modify a user's
working tree, execute development commands, and ask for authorization when a concrete operation
exceeds the current policy. Tool access must remain useful for normal coding while preserving the
project's durable journal, deterministic Runtime state, side-effect recovery, sandbox, provenance,
and audit boundaries.

The current implementation exposes five fixed tools. Each `ToolDefinition` has one coarse
permission (`READ`, `WRITE`, `EXECUTE_TEST`, or `EXECUTE_COMMAND`) and one recovery mode. The
Runtime's static allowed-permission set both filters the tool schemas visible to the model and
authorizes execution. `restricted_test` and `run_command` execute only trusted profiles;
`run_command` accepts structured argv, rejects shell interpreters, and fails closed when a profile
requires approval or capabilities the current sandbox cannot provide. A denied invocation becomes
a ToolResult; the product does not yet have a durable interactive permission request.

Those constraints remain valid implementation facts and must not be weakened without an activated
milestone. They are not the target product model for a Claude Code-like Coding Agent: a tool name
alone cannot express the difference between a bounded read, a reconciliable file edit, a test that
executes arbitrary project code, a destructive Git operation, and a command with an external
side effect.

This ADR defines the product and Runtime boundary for tool capability, command execution, and
permission interaction. It builds on:

- [`project-repository-workspace-conversation.md`](./project-repository-workspace-conversation.md),
  which defines WorkspaceBinding, external drift, writer authority, and narrower coordination for
  shared Git state; and
- [`conversation-turn-runtime-execution-lifecycle.md`](./conversation-turn-runtime-execution-lifecycle.md),
  which defines durable ToolInvocation identity, `WAITING_PERMISSION`, PermissionDecision,
  cancellation, reconciliation, and one Turn to one RuntimeExecution.

This ADR does not activate implementation, authorize a general shell in the current Runtime,
change a database or public schema, or define an implementation plan.

## Decision

### 1. Tool capability has a static ceiling and concrete invocation claims

Every tool has two distinct capability descriptions:

```text
ToolDefinition capability ceiling
        +
normalized ToolInvocation-specific CapabilityClaims
```

The ToolDefinition declares the maximum capability range that any invocation of that tool may
request. It is an upper bound and cannot authorize a concrete call.

A concrete ToolInvocation is normalized before permission evaluation. Its CapabilityClaims
describe at least the relevant:

- resource and path scope;
- process execution;
- filesystem writes;
- network access;
- workspace-external access;
- Git shared-state modification;
- external-system side effects;
- recovery classification; and
- required sandbox and process-isolation envelope.

Claims are inputs to policy, permission UX, recovery, and audit. They improve classification but
are not a proof that arbitrary code has no undeclared behavior. A script, test runner, build tool,
or interpreter may do more than its name implies. The effective sandbox and final ToolHarness
checks remain the enforcement boundary.

Permission risk and recovery mode are orthogonal:

- **Permission risk** asks whether the user and effective policy authorize the operation to occur.
- **Recovery mode** asks whether an operation that may already have started can be observed,
  reconciled, or safely retried after loss of the execution host.

Approval of a risky operation does not make it replayable. Conversely, a technically repeatable
operation may still require authorization.

### 2. Capability visibility and invocation authorization are separate

Tool visibility is decided at the capability level; authorization is decided for a normalized
invocation.

- A capability that is unavailable in the current environment or hard-disabled by effective
  policy is not exposed to the model.
- An available tool remains visible when some concrete invocations require authorization. Those
  invocations produce `ASK` when requested.
- A scoped deny does not require hiding the whole tool. Matching invocations are denied while
  non-matching invocations remain possible.

The Runtime must not treat the absence of preapproval as the absence of the capability. Otherwise
the model cannot propose an operation for the user to authorize.

### 3. Command execution is structured first, with an explicit shell form

The target command capability supports two explicit request forms:

```text
structured argv invocation
explicit shell program
```

Structured argv is the normal form. It preserves argument boundaries and avoids unnecessary shell
interpretation.

The shell form is used only when the requested operation requires shell grammar such as a
pipeline, redirection, conditional composition, or another shell construct. It is not an
unrestricted raw-shell-only design. A shell program must:

- be identified explicitly as shell form;
- be displayed in full to the user when permission is requested;
- have a durable normalized execution representation;
- receive more conservative permission classification than an equivalent simple argv request;
- expose parsed subcommands, redirections, substitutions, and asynchronous constructs to
  capability analysis where analysis is possible; and
- be denied or require authorization when its semantics cannot be classified with sufficient
  confidence.

Static command parsing is not the final security boundary. Shell quoting, expansion, scripts,
wrappers, interpreters, repository configuration, and executable content can make apparent
behavior incomplete or misleading. The OS sandbox and ToolHarness constraints must bound the
actual process and its descendants.

### 4. Trusted CommandProfile is an execution envelope and auto-approval candidate

A trusted CommandProfile remains useful, but it has narrower semantics than the current command
universe:

```text
known sandbox and resource envelope
        +
candidate for automatic permission
```

A profile may constrain executable resolution, argv shape, cwd, environment, network, filesystem
access, resource limits, and process isolation. Matching a profile may support `ALLOW` under the
current mode and rules.

A profile does not establish that a script, build, or test is free of side effects. It also does
not define every command the target product can ever run. A non-profile command may remain an
available capability and produce `ASK`, provided the required sandbox and hard boundaries can be
satisfied.

### 5. PermissionPolicy has four outcomes

PermissionPolicy evaluates the normalized ToolInvocation, CapabilityClaims, effective mode,
applicable rules, WorkspaceBinding, writer authority, and available sandbox. It returns exactly
one of:

- `UNAVAILABLE`: the system cannot execute the invocation under the necessary constraints;
- `ALLOW`: the invocation is authorized without an interactive decision;
- `ASK`: a structured user decision is required before execution; or
- `DENY`: policy rejects the invocation.

`UNAVAILABLE` is not an authorization prompt. A normal PermissionDecision cannot turn an
unsupported or unenforceable operation into an executable one.

The policy preserves restriction-first precedence. Managed hard deny and explicit deny override
mode auto-grants. An explicit ask rule overrides an automatic allow. Capability-specific allow
rules and mode defaults apply only after hard boundaries and stronger restrictions are satisfied.

### 6. Hard boundaries cannot be approved away

The following conditions cannot be overridden by a one-time user approval:

- the required sandbox is unsupported or unavailable;
- workspace containment validation fails;
- required workspace writer authority or operation-specific repository coordination is absent;
- the invocation or schema is invalid;
- an enforceable Plan or read-only ceiling forbids the effect;
- a managed hard-deny rule matches; or
- necessary process or network isolation cannot be enforced.

Changing one of these conditions requires an explicit configuration, mode, workspace, or product
operation outside the pending PermissionDecision. The UI must not present an approval choice that
the execution boundary cannot honor.

### 7. Permission scope is capability-specific

There is no universal `allow once` / `always allow this tool` hierarchy. Each capability exposes
only scopes appropriate to its risk and resources.

The initial product rules are:

- a controlled workspace file edit may receive exact-invocation approval or be automatically
  allowed by Conversation-scoped Accept Edits mode;
- an ordinary command defaults to exact-invocation approval;
- a user may explicitly create a repository/project-scoped command rule only when its matcher and
  effect envelope can be accurately displayed;
- destructive operations, shared Git-state modifications, credential access, network writes, and
  external-system side effects permit exact-invocation approval only; and
- the product does not offer a generic `always allow run_command` choice.

Repository-scoped command rules may apply across worktrees that share one RepositoryIdentity and
the applicable ProjectScope. They are policy rules, not reusable exact grants: every invocation in
a worktree still receives fresh claims, workspace checks, sandbox admission, and recovery
classification. A grant containing a concrete absolute path or workspace resource remains bound to
its WorkspaceBinding.

An exact approval binds all execution-relevant semantics, including the normalized operation,
WorkspaceBinding, cwd, paths and resources, environment policy, network policy, resource limits,
sandbox requirements, and foreground/background form. A material change requires a new policy
decision.

### 8. Permission requests are durable Runtime lifecycle records

Interactive permission follows this lifecycle:

```text
Committed ToolInvocation
    → normalize and classify
    → PermissionPolicy = ASK
    → durable PermissionRequest
    → WAITING_PERMISSION
    → matching PermissionDecision
    → revalidate
    → execute the same ToolInvocation
```

PermissionRequest binds the exact normalized invocation digest and identifies the corresponding
ToolInvocation. It also records the requested capability and resource scope, reason for the
prompt, risk information, and the approval scopes that are valid for that capability.

No side effect begins before the PermissionRequest is durably committed. Approval never mutates
the invocation or asks the model to recreate it. Before execution, ToolHarness revalidates:

- the grant and invocation digest;
- current policy and mode;
- workspace and resource preconditions;
- sandbox capability;
- required workspace writer authority or operation-specific repository coordination; and
- any material executable, interpreter, or execution-environment facts covered by normalization.

A failure during revalidation does not execute the old intent. It produces the applicable deny,
unavailable, stale-request, or newly required permission outcome.

`WAITING_PERMISSION` remains a non-terminal state of the same RuntimeExecution. It does not create
a new Turn or RuntimeExecution. A matching PermissionDecision is its normal resolver; cancellation
remains a separate ControlRequest.

For a pending write-capable invocation, a short foreground permission wait may retain already
acquired WorkspaceBinding writer authority. A durable stable detach or long wait may release it
under [`interactive-cli-product-workflow.md`](./interactive-cli-product-workflow.md). Approval does
not execute until the Runtime reacquires any required authority and revalidates the workspace. A
permission wait for an invocation that requires no writer authority does not acquire one merely
because it is waiting.

### 9. Approval and denial continue the same execution

An approval executes the same committed ToolInvocation after revalidation. Its ToolResult returns
to the same RuntimeExecution and model loop.

An ordinary denial rejects only the pending ToolInvocation. It produces a structured
`permission_denied` observation so the Agent can adapt inside the same Turn and RuntimeExecution.
The PermissionDecision itself is structured control data, not SteeringInput or UserReply.

Stopping the response is distinct:

```text
PermissionDecision(deny)
        +
ControlRequest(cancel)
```

The UI may present this combination as “Deny and stop,” but it must preserve both durable domain
events and the cancellation protocol. A denial alone does not imply `CANCELLED`.

Within one Turn, a later invocation with the same normalized digest as an already denied
invocation is denied again without another prompt. This suppression is local to that exact
invocation and Turn. It does not create a permanent rule or broaden the denial to materially
different invocations.

### 10. Foreground process supervision is the first-version command boundary

The first-version command requirement is supervised foreground execution. The complete descendant
process tree must remain inside the approved sandbox and process supervisor. Timeout, cancellation,
or command completion must apply the defined cleanup policy to the descendants.

Commands may not escape supervision through shell `&`, `nohup`, `setsid`, double fork, or
equivalent daemonization. Lexical rejection alone is insufficient: if the process boundary cannot
prevent or reliably clean up daemon escape, the invocation is `UNAVAILABLE` or `DENY`.

A timeout is normally a ToolResult within the same RuntimeExecution. It enters
`WAITING_RECONCILIATION` instead when process cleanup or a possible side effect cannot be
established. Killing a process tree does not prove that an earlier filesystem, Git, network, or
external effect did not happen.

### 11. Future managed background execution is explicit

A future background command capability must use an explicit `ManagedProcess` resource. It must
not arise accidentally from shell syntax. The possible long-lived owner is:

```text
Conversation + WorkspaceBinding
```

The resource also retains originating RuntimeExecution and ToolInvocation provenance. This allows
a process to remain attributable if it outlives its originating Turn, without converting it into a
Task or another RuntimeExecution.

This is an architecture reservation only. This ADR does not require a ManagedProcess schema,
supervisor, CLI, or implementation.

### 12. Permission UX, policy, Runtime, and ToolHarness have separate authority

Responsibilities are divided as follows:

```text
Permission UI
    displays a request and submits PermissionDecision

PermissionPolicy
    computes UNAVAILABLE / ALLOW / ASK / DENY

Runtime/controller
    durably records invocation, request, and decision
    and drives WAITING_PERMISSION transitions

ToolHarness
    performs final validation, sandbox admission,
    precondition checks, execution, result capture, and audit
```

ToolHarness remains the only gateway for real side effects. The UI cannot execute a tool. A tool
handler cannot prompt the user, expand its own authorization, or bypass Runtime state. ToolHarness
does not render or own permission UI.

The split does not permit ToolHarness to trust a boolean such as `approved=true`. It must verify a
grant bound to the exact durable execution payload and current enforcement conditions.

### 13. The first permission modes are Manual, Accept Edits, Plan, and Noninteractive

The initial mode set is:

| Mode | Semantics |
|---|---|
| `Manual` | Default. Controlled workspace reads are normally allowed; edits and commands without applicable preauthorization are evaluated and may produce `ASK`. |
| `Accept Edits` | Controlled workspace file-edit tools with revision preconditions may run automatically. Command execution does not inherit this authorization. |
| `Plan` | Enforceable read-only ceiling. A write cannot be authorized by answering a one-time permission prompt; the user must leave or change the mode through a distinct product action. |
| `Noninteractive` | Every invocation that would produce `ASK` becomes `DENY`; no interactive permission request is created. Intended for headless, CI, and evaluation use. |

Explicit deny and managed hard deny always override a mode auto-grant. Modes set policy defaults
and ceilings; they do not add a Conversation FSM or replace invocation-specific evaluation.

The architecture does not currently include an opaque automatic risk-classifier mode or an
ordinary local-interactive bypass-permissions mode.

### 14. User shell escape is deferred

A future interactive user command such as `! command` is a separate CLI UX question. This ADR does
not decide its sandboxing, transcript, process ownership, or permission behavior and does not
represent it as a model ToolInvocation.

## Command normalization and TOCTOU invariants

Permission is valid only when policy, UX, and execution refer to the same immutable operation.
The following invariants prevent an architectural check/execute split:

1. Normalization produces one durable execution payload; capability analysis, permission display,
   grant digest, denial suppression, and ToolHarness execution all reference that payload.
2. Structured argv remains an ordered argument vector through execution. It is not joined into a
   shell string after approval.
3. Shell form preserves the exact program, interpreter identity and options, cwd, environment
   policy, sandbox envelope, and other semantics that affect execution. No post-approval template
   interpolation or command reconstruction is allowed outside the approved shell semantics.
4. A human-readable rendering is derived from the durable payload. It is never parsed back into
   the operation to execute.
5. The digest covers all execution-relevant fields and the normalization semantics or version.
   Recovery cannot silently reinterpret an old approved payload under materially different
   normalization rules.
6. Executable, interpreter, cwd, workspace, and relevant resource facts that may drift between
   request and execution are revalidated. Material drift invalidates the pending approval or
   requires a new decision.
7. Shell expansion and invoked script contents may remain dynamic. Their uncertainty must be
   reflected in conservative claims and bounded by sandbox enforcement; textual normalization
   must not claim to prove their eventual behavior.

These rules close the avoidable TOCTOU path in which one command is reviewed and a different
command is executed. They cannot make mutable scripts or shell expansion semantically immutable.
That remaining risk is controlled through revalidation, exact grants, sandboxing, writer
authority, and recovery policy.

## Architectural invariants

1. ToolDefinition declares a capability ceiling and never authorizes a call by itself.
2. Authorization is evaluated against a normalized, concrete ToolInvocation and its claims.
3. CapabilityClaims are policy evidence, not a security proof.
4. Tool visibility is distinct from invocation authorization.
5. Command execution is structured-argv first; shell form is explicit and conservative.
6. Trusted CommandProfile defines an execution envelope and possible auto-approval, not script
   purity or the entire command universe.
7. PermissionPolicy returns `UNAVAILABLE`, `ALLOW`, `ASK`, or `DENY`.
8. A normal user approval cannot override a hard boundary.
9. Permission scopes are capability-specific; generic permanent command-tool approval is absent.
10. Repository command rules may cross worktrees of one RepositoryIdentity, while concrete
    workspace-resource grants remain bound to a WorkspaceBinding.
11. PermissionRequest and PermissionDecision are durable records bound to an immutable invocation.
12. Permission approval never mutates or replaces its ToolInvocation.
13. Permission denial is not cancellation.
14. Exact denied invocations do not prompt repeatedly inside one Turn.
15. Permission risk and recovery mode remain orthogonal.
16. First-version command execution is supervised and foreground-only.
17. ToolHarness is the sole real-side-effect gateway and never owns the permission UI.
18. Manual is the default mode; Plan is an enforceable ceiling; Noninteractive converts ASK to
    DENY; Accept Edits never grants command execution.
19. No accepted decision introduces a persistent Task or multiple RuntimeExecutions per Turn.

## Consistency review with the prior ADRs

The accepted decisions are consistent with both prior architecture clusters.

- **Immutable WorkspaceBinding:** every ToolInvocation and exact grant is evaluated within the
  RuntimeExecution's immutable WorkspaceBinding. A Conversation rebind cannot move or validate a
  pending permission request for an older execution.
- **Direct user working tree:** controlled edits may target the user's bound working tree, while
  revision preconditions and external-drift validation remain mandatory. Permission approval does
  not waive those checks.
- **Single coordinated writer:** an invocation requiring workspace writes must have the execution's
  writer authority before it can execute. Repository-level coordination remains limited to
  operations whose claims identify shared Git state.
- **WAITING_PERMISSION:** the state begins before invocation, resolves through a matching
  PermissionDecision, and resumes the same RuntimeExecution. It remains distinct from
  `WAITING_USER_INPUT`, `WAITING_RECONCILIATION`, and `INTERRUPTED`.
- **Writer authority while waiting:** a short write-capable wait may retain already-acquired
  authority, while a durable stable detach or long wait may release it. A later dispatch must
  reacquire and revalidate. A read-only or otherwise non-writing permission request neither needs
  nor acquires authority solely because it is waiting.
- **Steering:** SteeringInput cannot mutate a pending ToolInvocation. The user denies the old
  invocation and the model may later commit a different one.
- **Cancellation:** “Deny and stop” records both denial and a separate cancel ControlRequest.
  Terminal `CANCELLED` still waits for in-flight effects to settle or reconcile.
- **Recovery:** approval is not replay authority. If a permitted non-idempotent action becomes
  uncertain after it starts, recovery enters `WAITING_RECONCILIATION`, not
  `WAITING_PERMISSION`.
- **One Turn to one RuntimeExecution:** permission prompts, decisions, safe attempts, denial
  adaptation, timeout handling, and recovery all stay within the same execution.

There is no conflict requiring a change to the Conversation, Turn, RuntimeExecution,
RepositoryIdentity, ProjectScope, or WorkspaceBinding relationships.

## Effect on the product domain model

The main model remains:

```text
Conversation
└── Turn
    └── RuntimeExecution
        ├── ToolInvocation
        │   ├── CapabilityClaims
        │   ├── PermissionRequest / PermissionDecision when required
        │   ├── ToolAttempt
        │   └── ToolResult
        └── Runtime wait/control events
```

CapabilityClaims, PermissionRequest, and PermissionDecision are subordinate execution records,
not top-level product entities. They do not create an independent Task identity or a second
RuntimeExecution attempt.

A future ManagedProcess would be an attributable resource associated with Conversation and
WorkspaceBinding, with origin provenance back to a ToolInvocation. Before that capability is
enabled, its own lifecycle, cleanup, writer coordination, and recovery require a separate
decision. Reserving the resource does not alter the current three-entity lifecycle.

## Consequences

### Positive

- The model can request useful coding operations even when they are not preapproved.
- Permission prompts describe the concrete command, paths, resources, and effect envelope rather
  than only a coarse tool name.
- The existing sandbox, ToolHarness, call journal, reconciliation, and audit assets remain the
  execution kernel.
- Exact invocation binding preserves auditability across permission waits and crash recovery.
- Modes provide recognizable Coding Agent UX without introducing an opaque classifier or ordinary
  bypass mode.
- Restricted profiles remain valuable for predictable automatic execution without constraining
  the final product to a closed command catalog.

### Negative and risks

- Command and shell classification remains conservative and cannot infer all script behavior.
- Durable permission waits add more Runtime records and recovery cases.
- Retaining writer authority during a short write-capable permission wait may reduce concurrency;
  releasing it may cause later reacquisition or revision validation to fail.
- Repository-scoped command rules can remain syntactically stable while repository scripts change
  behavior.
- Exact prompt scopes reduce risk but can create permission fatigue.
- Plan-mode read-only classification is difficult for commands that execute project code; some
  apparently observational commands must be asked, denied, or unavailable.
- Cross-worktree repository rules require careful UX so users do not confuse policy reuse with a
  reusable grant for a specific workspace.

## Alternatives rejected for the target model

- **One coarse permission per tool:** it cannot express invocation-specific resources, command
  effects, or permission scope.
- **Use the preapproved permission set as tool visibility:** it prevents the model from proposing
  an operation that could validly be authorized.
- **Profile-only command universe:** it is appropriate for controlled evaluation but too closed for
  an out-of-the-box Coding Agent.
- **Unrestricted raw-shell-only execution:** it discards structured argument boundaries and makes
  permission analysis unnecessarily ambiguous.
- **Treat a trusted executable or test profile as harmless:** interpreters, tests, builds, and
  repository scripts can perform arbitrary actions inside their effective environment.
- **Let approval override sandbox or containment failure:** consent cannot create a missing
  enforcement capability.
- **Generic permanent approval for run_command:** the scope is too broad to communicate or enforce
  safely.
- **Represent denial as cancellation:** it prevents the Agent from selecting an authorized
  alternative and conflicts with the accepted control taxonomy.
- **Use permission risk as recovery classification:** authorization and safe replay answer
  different questions.
- **Allow accidental background escape:** an unmanaged process has no reliable ownership, output,
  stop, cleanup, or recovery semantics.
- **Put permission UI inside ToolHarness:** it couples the execution kernel to an attachment and
  prevents durable host-independent waiting.
- **Make the UI the execution authority:** another host or a UI defect could bypass the sole
  side-effect gateway.
- **Copy every Claude Code permission mode immediately:** automatic classification and bypass
  modes add policy ambiguity without being required for the initial Coding Agent loop.

## Deferred decisions

This ADR intentionally does not define:

- database tables, migrations, serialized events, or public schemas;
- the concrete CapabilityClaims type or command-normalization representation;
- the command grammar, parser library, interpreter set, matcher syntax, or rule storage format;
- the physical storage and administration UI for permission rules;
- a complete operation taxonomy for Git, network, credential, package-management, or external
  systems;
- platform-specific sandbox and descendant-process implementation;
- a ManagedProcess lifecycle, background supervisor, or process-control UI;
- user shell escape behavior; or
- implementation sequencing and milestones.

These representation decisions must preserve the durable identity, exact-grant, hard-boundary,
normalization, and ToolHarness invariants above.

## Reference product behavior

The interaction model is informed by publicly documented Claude Code behavior:

- [Permissions](https://code.claude.com/docs/en/permissions)
- [Sandboxing](https://code.claude.com/docs/en/sandboxing)
- [Interactive mode and background commands](https://code.claude.com/docs/en/interactive-mode)
- [CLI reference](https://code.claude.com/docs/en/cli-reference)

These references establish observable product behavior only. They do not establish Claude Code's
internal capability representation, policy engine, permission-request persistence, command
normalization, process supervisor, or recovery implementation.
