# Product-Layer M1 Persistence Design

Status: implemented; final owner acceptance remains gated on the verification report.

This record makes the physical choices delegated by the active
[M1 execution contract](./execution-contracts/m1-execution-contract.md). It does not amend an
Accepted ADR.

## Authority and ownership

SQLite is the only durable authority. Migration V5 adds product records while every legacy Runtime
record and reader remains intact. `sessions` is still the physical Runtime execution state record;
`runtime_executions` is a private compatibility mapping, not a new FSM owner. `Conversation` and
`Turn` contain no FSM state, transition, permission, workspace-writer, or tool-audit fields.
Runtime messages/events/tool calls remain Runtime audit; `conversation_semantic_events` is the
separate append-only product semantic transcript.

`ProductRepository` is the narrow typed private Product persistence port. `SQLiteRunJournal` is its
sole implementation and remains the single SQLite authority. Application code crosses it through
`LegacySessionProductCompatibilityAdapter`, the explicit legacy Session-to-Product seam; no Product
table or adapter is exposed through Runtime IPC.

## ADR mapping

| M1 decision | Accepted ADR authority | Implementation boundary |
|---|---|---|
| generated repository registry; descriptors are evidence, common-dir links but remote/history do not merge clones | [`project-repository-workspace-conversation.md`](./decisions/project-repository-workspace-conversation.md) | `RepositoryIdentity` / `RepositoryDescriptor` and `ProductRepository.register_repository_identity()` |
| ProjectScope, immutable execution binding, Conversation default binding, one synthetic Turn to one mapped execution, no duplicate FSM | [`conversation-turn-runtime-execution-lifecycle.md`](./decisions/conversation-turn-runtime-execution-lifecycle.md) | V5 relations plus `LegacySessionProductCompatibilityAdapter` |
| legacy source never becomes a product direct-working-tree execution path | [`project-repository-workspace-conversation.md`](./decisions/project-repository-workspace-conversation.md) and the M1 contract | copied-workspace or explicit unprepared compatibility binding only |

## Explicitly deferred physical representations

- M2 owns active discovery/marker activation and any Git probing policy, real WorkspaceBinding
  lifecycle, Conversation rebind, writer authority, and Turn Admission publication.
- M2 owns the non-synthetic Conversation transcript event taxonomy and interactive lifecycle; M1
  stores only labelled import events and does not reconstruct user interaction semantics.
- M3 owns instruction/context manifests and frozen-request semantics; M4 owns permissions,
  checkpoint/diff/undo and command behavior; M5 owns product CLI; M6 owns default Memory serving;
  M7 owns legacy removal and release closure.

## Durable representation

| Record | V5 storage | Key / invariant |
|---|---|---|
| RepositoryIdentity | `repository_identities` | generated persisted UUID is authority |
| RepositoryDescriptor | `repository_descriptors` | observed evidence, never an identity ID |
| ProjectScope | `project_scopes` | unique `(repository_id, relative_path)` |
| WorkspaceBinding | `workspace_bindings` | immutable compatibility locator per mapping |
| Conversation | `conversations` | has a default binding, separate from execution binding |
| Turn | `turns` | ordered per Conversation; no Runtime FSM |
| RuntimeExecution | `runtime_executions` | exactly one legacy Session and one Turn |
| ConversationSemanticEvent | `conversation_semantic_events` | ordered and provenance-labelled |

The registry first creates an internal UUID, then attaches descriptors. An explicitly supplied ID
may only attach a descriptor to an already registered identity; it cannot mint caller-chosen IDs.
Canonical path or Git
common-dir may rediscover an existing identity; their descriptors are globally unique and conflict
across identities is rejected. Remote/history are retained for inspection but cannot discover or
merge independent clones. A moved path may be explicitly attached to a registered UUID, so an
identity is never a path hash. Non-Git directories use the same generated-ID registry.

## Migration and compatibility mapping

`V5` is additive. On journal open, each existing `sessions` row receives an independent,
idempotent M1 mapping transaction. A failed transaction leaves no product rows for that Session,
writes an observable `product_mapping_failures` recovery record in a separate transaction, and
surfaces startup failure; a healthy later open retries it. V5 DDL rollback leaves the V1–V4 database
usable. There is no destructive downgrade.

The adapter creates one RuntimeExecution, synthetic Conversation, synthetic Turn, one immutable
WorkspaceBinding referenced as the imported Conversation default and execution binding, and
`legacy_session_imported`. Provenance explicitly says it is a
mapping only: it does not reconstruct an `InitialRequest` or historical interactive exchange.
When `sessions.workspace_path` exists, its immutable binding is `legacy_copied_workspace`; otherwise
the binding is `legacy_unprepared_compatibility` and explicitly does not claim source-tree execution.
Source path remains a Repository descriptor only.

New legacy `run_task()` sessions are mapped after Runtime execution, in the same SQLite authority
but in a distinct atomic/retryable M1 transaction. If that mapping fails, it cannot replace an
already determined legacy `RunResult`: `product_mapping_failures` records the error and journal-open
backfill retries it. This is an explicit eventual-recovery seam, not a hidden claim that mapping is
already complete. It deliberately neither implements M2 Turn Admission nor changes the legacy
lifecycle/admission atomic boundary.
If the failure record itself cannot be written, the legacy result is still preserved; the missing
mapping remains detectable and a healthy later journal open retries backfill.

## Deferred work

M1 does not invoke new Git/runtime side-effect paths, create direct-working-tree bindings, or add
writer authority, rebind, admission, product CLI, context manifests, permissions, diff/undo, Memory
serving, or IPC changes. Those remain M2–M7 work under separate activation.
