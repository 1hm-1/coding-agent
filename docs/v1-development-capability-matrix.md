# V1 Development Capability Matrix

Date: 2026-09-22
Status: **Acceptance contract frozen; Product-Layer M0–M3 Accepted and complete; real user trees remain read-only; M4–M7 and future matrix capabilities remain inactive**

## Purpose and authority

This matrix freezes the development capability promised by Coding Agent V1. It is an acceptance
input to M4 and M7, not a claim about the current five-tool implementation. The eight accepted ADRs
remain the product and domain authority; this document narrows their permitted future capability
space to a testable V1 delivery slice.

The completed [`M1 execution contract`](./execution-contracts/m1-execution-contract.md) implemented
only additive identity/mapping persistence. The accepted
[`M2 execution contract`](./execution-contracts/m2-execution-contract.md) establishes direct
WorkspaceBinding/lifecycle/read-only Product paths but implements no M4 matrix capability. The
M3+M4 real-tree mutation gate remains closed, and the additive Schema v6 Product lifecycle preserves
the legacy Runtime compatibility boundary. The accepted
[`M3 execution contract`](./execution-contracts/m3-execution-contract.md) implements only the
instruction/context half of that gate; neither M2 nor M3 acceptance opens mutation.

V1 targets a local, single-user Linux host with POSIX process semantics and a preprovisioned
pure-Python/package development workflow, using sandbox capabilities that the implementation can
actually probe and enforce. macOS and Windows are outside V1. Bounded filesystem operations and
read-only Git inspection are language-agnostic. Python is the only certified command ecosystem;
native compiler/toolchain provisioning is not included.
This Python certification is a V1 delivery and acceptance scope, not a domain-architecture
restriction. The capability/permission architecture remains ecosystem-agnostic. A future language
or tooling profile can use the same domain model, but it still requires explicit activation,
enforceable configuration and trust, and its own acceptance evidence; its existence never grants
automatic approval or expands the V1 promise.

## Decision vocabulary

Every normalized invocation receives one of the accepted permission outcomes:

- `ALLOW`: authorized under the effective mode/rules and still subject to final ToolHarness,
  writer, revision, containment, and sandbox validation;
- `ASK`: exact durable invocation approval is required, followed by the same final validation;
- `DENY`: an available, recognizable operation is rejected by policy, including a hard boundary;
  and
- `UNAVAILABLE`: the product or host cannot provide the capability under the required enforcement
  envelope. Approval cannot change this outcome.

`DENY` and `UNAVAILABLE` are deliberately distinct. An operation outside the V1 product promise or
missing an executable, dependency, or enforceable sandbox is `UNAVAILABLE`. A recognized operation
that violates a hard policy boundary is `DENY`. If both descriptions apply, the product reports the
most actionable non-executable reason without offering approval; neither result may be converted to
execution by user consent.

## Required V1 matrix

| Capability | Required V1 coverage | Normal policy shape | Enforcement and recovery boundary |
|---|---|---|---|
| Filesystem read | Bounded workspace-relative text/metadata read with file, byte, output, timeout, containment, and link limits | normally `ALLOW`; `DENY` for hard-boundary paths; `UNAVAILABLE` for unsupported file types or enforcement | ToolHarness and WorkspaceBinding containment; no external-path or credential expansion |
| Filesystem search | Bounded workspace-relative literal or otherwise explicitly supported search with deterministic result limits | normally `ALLOW`; `DENY` outside the binding; `UNAVAILABLE` for unsupported search form | Read-only ToolHarness path; limits and truncation are explicit |
| File create | Exact workspace-relative controlled create with absent-path/revision precondition and before/after evidence | Manual `ASK`; Accept Edits may `ALLOW`; Plan `DENY`; Noninteractive converts `ASK` to `DENY` | writer authority, path/type precondition, ToolHarness mutation, checkpoint/change evidence, crash reconciliation |
| File edit | Exact workspace-relative controlled edit with revision precondition and before/after evidence | Manual `ASK`; Accept Edits may `ALLOW`; Plan `DENY`; Noninteractive converts `ASK` to `DENY` | same controlled-write boundary; instruction-source edits still require exact `ASK` |
| File delete | Exact, bounded file delete with explicit target/type disclosure and retained restoration evidence | exact-invocation `ASK` in write-capable modes; `DENY` in Plan; never covered by a generic edit grant | all-target preconditions, writer authority, ToolHarness, tombstone/before image, success/failure/crash recovery tests; recursive or unbounded delete is not promised |
| Git inspection | Read-only `status`, `diff`, `log`, and `show` over the bound repository, preserving dirty and staged state | eligible for `ALLOW` only inside the frozen inspection envelope; otherwise `ASK`, `DENY`, or `UNAVAILABLE` as applicable | no index/ref/history/worktree mutation; disable optional index refresh and locks; no hooks, pager, external diff, textconv, aliases, or repository configuration that can escape into command execution |
| Python test | Preinstalled `python -m unittest` and `pytest` through explicit structured profiles and bounded foreground execution | matching trusted envelope may `ALLOW`; otherwise exact `ASK`; missing runner/dependency/sandbox is `UNAVAILABLE` | project code is untrusted and may write indirectly; M4 sandbox, writer, command-window observation, cleanup, and recovery apply |
| Python lint | Preinstalled Ruff through an explicit structured profile; check/fix forms have separate claims | read-only check may `ALLOW`; mutation-capable form is `ASK` or `DENY` by mode; missing Ruff is `UNAVAILABLE` | structured argv, offline execution, bounded outputs/resources; any fix is a command-observed mutation, not controlled-edit authorship |
| Python typecheck | Preinstalled mypy through an explicit structured profile | matching read-only envelope may `ALLOW`; otherwise exact `ASK`; missing mypy/dependencies is `UNAVAILABLE` | offline, foreground, bounded resources; caches may only use declared writable locations and are still mutations |
| Python build | Preinstalled `python -m build` for preprovisioned pure-Python/package projects, offline and no-isolation | exact `ASK` by default unless an explicit trusted rule applies; missing build/dependencies/sandbox or a required native toolchain is `UNAVAILABLE` | declared output/cache scope, no dependency/bootstrap/toolchain provisioning or network, foreground supervision, observed change and recovery evidence |
| Additional structured command | Only an explicitly configured/trusted executable and argv/resource envelope that the active sandbox can enforce | exact `ASK` by default; a narrow displayed rule may later `ALLOW`; never implicit trust from a project script name | exact durable invocation identity, structured argv, ToolHarness, foreground descendant cleanup, command-window observation, reconciliation |
| Other language/package ecosystem | Filesystem read/search/edit remains available, but Node/npm, Rust/Cargo, Go, Java, and other built-in command workflows are not V1-certified | built-in command capability is `UNAVAILABLE` unless separately configured as the preceding row | no claim of install, test, lint, typecheck, build, package, or publish support |
| Shell grammar / arbitrary shell | The ADR-reserved explicit shell-program request form is not delivered in V1; unrestricted arbitrary shell is not a V1 capability | `UNAVAILABLE`; no approval prompt | remains a future capability decision and is not removed from the domain model |

Trusted structured profiles are validated execution and possible auto-approval envelopes. They are
not the permanent domain command universe, do not prove that project code is pure, and do not make
the current `python_project` profile a complete V1 development capability.

Across the entire matrix, any recognized instruction/rule-source mutation requires exact review
and approval. This includes create, edit, delete, a command whose declared/known effects cover that
source, and CodeRewindOperation that restores it. Accept Edits never auto-allows such a mutation;
Plan/read-only ceilings, explicit deny, managed hard deny, unavailable enforcement, and other
restriction-first outcomes remain stronger than `ASK`.

## Unsupported versus prohibited operations

The following are not V1 product promises and normally report `UNAVAILABLE`: network installation,
dependency bootstrap, remote fetch, publish, push, arbitrary package-manager workflows, unmanaged
background processes, unsupported interpreters, and operations whose executable, dependency, or
sandbox is absent.

The following are V1 hard-policy `DENY` boundaries when recognized: arbitrary credential access;
workspace escape; containment bypass; recursive or unbounded delete; destructive Git reset/clean/
checkout, index writes, ref/history mutation, or hidden commits; an attempt to evade foreground
supervision; and execution that would require disabling mandatory ToolHarness, writer, revision,
mode, or sandbox checks. A hard boundary is never approvable.

Remote/publish/push and network installation can be both unsupported and dangerous. V1 must not
offer approval for them. Reporting `UNAVAILABLE` because the capability is absent does not weaken
the hard policy that would still apply if a future capability were introduced.

## Mutation rollout gate

M2 may establish a real WorkspaceBinding, multi-Turn continuity, and read-only interactive access.
No product path may mutate a real user working tree until both M3 instruction/context protection
and the complete M4 permission, capability, code-change, and recovery gate pass.

The gate covers explicit create/edit/delete, test/lint/typecheck/build and their caches, Git
inspection side effects such as optional index refresh, startup/admission/checkpoint or instruction
artifacts placed inside the checkout, hooks or helper processes, indirect command effects, and
CodeRewindOperation. Product ledger, manifests, and artifacts may persist in separate agent-owned
storage. An InstructionManifest placeholder or checkpoint identity/coverage shell does not
establish M3 instruction enforcement or M4 diff/rewind protection.

Isolated test fixtures and copied workspaces are exempt only from rollout into a real user tree.
They are never exempt from actual ToolHarness routing, containment, permission-mode semantics,
sandbox enforcement, journaling, failure handling, or recovery tests.

## M4 exit criterion

M4 is not complete until every required row above has executable success, expected-failure,
permission, hard-boundary, unavailable-environment, crash/restart, and recovery evidence appropriate
to its effects. The tests must prove:

1. exact durable invocation identity survives permission wait and recovery;
2. ALLOW/ASK/DENY/UNAVAILABLE and mode precedence match the accepted permission ADR;
3. no direct or indirect mutation reaches a real user tree before both M3 and M4 gates pass;
4. read-only Git inspection preserves the index, refs, history, staged content, dirty content, and
   process envelope;
5. controlled create/edit/delete retain sufficient before/after authority for their declared
   rewind guarantee, while command diffs are never called automatically reversible;
6. absent tools, dependencies, or sandbox features fail as `UNAVAILABLE`, never as an approval
   bypass; and
7. the published V1 support matrix matches this document without implying support for other
   ecosystems or arbitrary shell.

Only after this exit gate and M3 are both complete may a later milestone enable product mutation of
a real working tree. Completion of this document or M2 alone enables nothing.
