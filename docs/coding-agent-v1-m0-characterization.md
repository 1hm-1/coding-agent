# Coding Agent V1 Product-Layer M0 Characterization

Status: **M0 Accepted and complete; M1–M7 remain inactive**

Date: 2026-09-21

Authority: [`execution-contracts/m0-execution-contract.md`](./execution-contracts/m0-execution-contract.md),
the Accepted product ADRs indexed by
[`architecture-consistency-audit.md`](./architecture-consistency-audit.md), and
[`target-architecture-snapshot.md`](./target-architecture-snapshot.md).

## Scope and conclusion

M0 characterized the existing one-shot Runtime Kernel without changing production code, schemas,
defaults, Accepted ADRs, or protected fixtures. It did not implement Product-Layer M1–M7 behavior.
The uncertain-provider-retry defect is reproduced and remains owned by M3.

The frozen interpretation is recorded unchanged: Python is the V1 certified capability scope, not
a domain architecture restriction; M5's P95 500 ms and every-observation 2 s thresholds apply to
local input, status, and stop acknowledgement only. Model, tool, and safe-cancellation completion
latencies are outside that threshold.

No contradiction was found among the Accepted product decisions. Differences between current
Session-based code and the target domain are recorded below as migration gaps rather than ADR
contradictions.

## Provenance and worktree boundary

The pre-change Git identity was `b5f83ed713a22aa4c44834334c2edde3f921566b` on `main`, with a
dirty worktree. [`pre-change-provenance.json`](./evidence/v1-m0/pre-change-provenance.json) records
every pre-existing modified or untracked path and its SHA-256. The pre-existing Memory experiment,
including `retrieval_backends.py`, `retrieval_spike.py`, its tests, and September 18 evidence, was
not modified by M0.

The contract interpreter was CPython 3.10.12. The project virtual environment used CPython
3.11.16, Ruff 0.16.6, mypy 1.20.2, and Coverage.py 7.16.0. The native sandbox reported
`linux_user_mount_pid_net`, image digest
`sha256:d7ebf3d20ece57bead318f6f57483c896787edeb127061a2254c6ff23b60c19c`, network disabled,
and all declared namespace, containment, resource-limit, output-limit, and descendant-cleanup
features available on the WSL2 6.18.33.2 kernel.

## Verification result

Before M0, the discovered suite ran 169 tests with no failures, skips, or expected failures. The
first M0 submission ran 181 total tests: 180 passed and one was the deliberate expected failure.
The evidence-closure update adds two focused tests; the final suite ran 183 total tests: 182 passed
and one was the deliberate expected failure, as recorded in `verification-v1.json`. The expected
failure executes the real uncertain retry path and asserts M3's future exact
frozen-request invariant; its paired current-behavior test passes and proves why the invariant does
not hold today.

The final focused M0 suite ran 14 tests with 13 passes and that one expected failure. The preserved
hardening, recovery, persistence, and Runtime IPC suite ran 42 tests with no failures or skips.
Ruff, mypy, compileall, and `git diff --check` passed. Coverage ran the full 183-test suite and
reported 7,898 statements, 1,624 missed, and 79.4% statement coverage. Coverage is descriptive;
M0 did not change its threshold or configuration.

The four semantic goldens and every checked-in Runtime IPC v1 schema, vector, and golden fixture
match the frozen hashes in [`protected_hashes.json`](../tests/fixtures/v1_m0/protected_hashes.json).
The CLI and IPC producer tests passed. Database schema remains v4. Fresh databases and test-owned
v1, v2, v3, and v4 starting points all migrate to v4 and remain idempotent; existing tests also
prove future-version rejection and transaction rollback.

## Runtime Kernel preservation coverage

| Asset to preserve | Current authority | Evidence |
|---|---|---|
| Explicit FSM and one Agent loop | `runtime.py`, `domain.py` | state-machine, vertical-slice, budget, classified-failure, recovery tests |
| SQLite authority and atomic mutations | `persistence.py`, `migrations.py` | persistence rollback/conflict tests and all-start migration characterization |
| Leases and stale-owner rejection | `SQLiteRunJournal` | `test_lease_takeover_rejects_old_owner_writes` |
| Model/tool journals and response reuse | `SQLiteRunJournal`, `AgentRuntime` | M2 recovery suite and M0 positive control |
| Bounded retry/fallback | Runtime and model adapters | model/recovery tests; uncertain identity gap remains explicit |
| ToolHarness side-effect gateway | `tools/harness.py` | tool, structured execution, sandbox, and protocol tests |
| Copied workspace/source protection | `WorkspaceManager`, `WorkspaceGuard` | workspace tests, two-task M0 test, eval source invariant, both scripted smokes |
| Sandbox fail closed and cleanup | `sandbox/` | native namespace, limits, containment, cleanup, and protocol cancellation tests |
| Provider formats in adapters | `models/openai_compatible.py`, `models/anthropic.py` | adapter contract and malformed-response tests |
| Context budgets and raw history | `context.py`, `compression.py`, summaries | context overflow/retention, lineage/staleness, compression round-trip tests |
| JSONL derived from SQLite | `export.py`, `trajectory.py` | delete/rebuild/export/replay persistence tests |
| CLI, IPC v1, eval/oracles | `cli.py`, `protocol/`, `evaluation.py` | CLI surface characterization, 42-test focused suite, offline baseline |
| Dormant governed Memory | `memory/` explicit composition | default-off test and unchanged 12-case cold/warm regression |

For injected crash and refusal cases, the referenced suites collectively assert durable journal
state, observable errors/results, filesystem or process effects, and recovery or explicit refusal.
They use fault hooks, fake executors/providers, clocks, and journals rather than arbitrary sleeps.

## Current code to target architecture inventory

| Current component and authority | Actual behavior and preservation test | Accepted target responsibility | Classification | Owner | Compatibility and open M1+ decision |
|---|---|---|---|---|---|
| `domain.Session`, `RuntimeSnapshot` | One one-shot task and its FSM counters/state; replay and persistence round trips pass | `Conversation → Turn → RuntimeExecution`; FSM remains execution-owned | Legacy compatibility plus retained Runtime asset | M1 | Preserve Session IDs/readers while deciding additive execution mapping; do not copy FSM state into Conversation/Turn |
| `AgentApplication.run_task()` | Creates a random Session and copied workspace; two calls produce isolated workspaces/transcripts and leave source unchanged | coordinator accepts product requests and publishes Conversation/Turn/Execution atomically | Legacy compatibility | M1–M2 | Keep `run_task()` meaning; decide product publication transaction and adapter boundary |
| `AgentApplication.resume_session()` | Resumes a legacy Runtime execution, validates source/workspace, and can rebuild model input | product Conversation resume is separate from RuntimeExecution recovery | Retained recovery asset plus target gap | M2, M5 | Keep legacy execution resume explicitly named; never reinterpret it as product `/resume` |
| `WorkspaceManager.create()`, `WorkspaceGuard` | Copies source to agent home and confines paths; no direct working-tree mutation | immutable `WorkspaceBinding`, repository/project identity, coordinated writer, drift handling | Retained containment asset plus deferred representation | M2 | Direct binding and its identity/rebind rules require M2; mutation gate remains closed through M3+M4 |
| `SQLiteRunJournal` leases | One execution lease, expiry/takeover, optimistic version checks | execution lease plus WorkspaceBinding writer authority and admission | Retained asset | M2 | Preserve stale-owner rejection; decide separate writer/admission records without weakening the execution lease |
| `messages`, `events`, checkpoints | Runtime protocol/audit stream and model transcript coexist under Session; sequence/version uniqueness | Conversation transcript and semantic events separate from Runtime audit events | Retained data plus target gap | M1, M3 | Preserve raw rows and order; decide synthetic historical transcript/semantic-event representation |
| `model_calls` journal | Durable request/response/error, ordinal, attempt, status; uncertain retry may overwrite request JSON | one exact frozen ModelRequest and ContextManifest before every provider call | Retained journal plus correctness gap | M3 | Add exact immutable request identity; preserve existing request IDs and historical ambiguity |
| `tool_calls` journal | Durable invocation arguments, revisions, result/error, attempt, and recovery mode; unknown effects reconcile via `WAITING_APPROVAL` | typed ToolInvocation, effect certainty, recovery barrier, permission lifecycle | Retained journal plus deferred representation | M2, M4 | Legacy `WAITING_APPROVAL` remains reconciliation; ordinary permission approval needs distinct records/state |
| `BudgetedContextBuilder`, summaries, compression | Deterministic bounded model-visible projection, required retention, derived summary lineage, raw events preserved | hybrid ContextComposer, transcript authority, ContextManifest, Frozen ModelRequest | Retained algorithms plus target gap | M3 | Adapt existing algorithms; decide manifest schema and exact composition transaction |
| `RunPolicy.allowed_permissions`, denial observations | Static enum gate; denial becomes tool observation; no request/decision tables | four-outcome PermissionPolicy with durable request and decision bound to invocation digest | Legacy compatibility plus target gap | M4 | Keep denial behavior for legacy runs; add typed lifecycle without turning reconciliation into approval |
| CLI one-shot commands | `run`, `run-scripted`, session admin, replay/export/evaluate, and headless IPC; no product `new/status/diff/undo/permissions` | ephemeral startup, interactive Conversation commands, typed input routing | Legacy compatibility plus target gap | M5 | Preserve current commands; product command naming/routing must be additive and explicit |
| Runtime IPC v1 producer | Strict schemas, vectors, redaction, cancellation/deadline behavior; Session identity remains public v1 field | Platform boundary remains Runtime execution protocol | Retained public contract | M1–M7 | Do not expose private product/SQLite schema or claim v0.1.0 implements future product protocol |
| Memory SQLite/governance and opt-in context seam | Governed records/audit exist; default application/headless/IPC wiring is off; current retrieval experiment is separate | optional low-authority UserPreference Memory with product provenance | Retained dormant asset plus deferred representation | M6 | Core Product cannot depend on Memory; M1 only preserves rows and later provenance mapping capability |

## M1 migration inventory

[`m1-migration-inventory-v1.json`](./evidence/v1-m0/m1-migration-inventory-v1.json) is the
machine-readable inventory. It records every table, column, primary key, foreign key, unique/index
definition, and current `CREATE TABLE` statement obtained from a disposable v4 database. It also
records JSON-serialized fields, legacy identifier surfaces, preservation readers, candidate
historical mappings, required migration fixtures, failure behavior, and decisions reserved for M1.

The inventory separately versions every serialized format. `RuntimeSnapshot` writes v2 and accepts
v1/v2. Runtime Event, SummaryRecord, and MemoryRecord each have an explicit v1 reader. Policy,
message metadata, model/tool journal payloads, Memory audit payloads, and retrieval selections have
no embedded format version; the inventory labels them `legacy-unversioned-v0`, names their current
readers, and requires raw-content compatibility. SQLite schema v4 is not treated as their payload
version.

The current versions are:

| Version | Additions |
|---|---|
| v1 | `sessions`, `messages`, `events`, `checkpoints` |
| v2 | Session lease/interrupt/resume/context columns, `model_calls`, `tool_calls`, call indexes |
| v3 | `summaries` and lineage/range indexes |
| v4 | `memory_records`, `memory_events`, `memory_retrievals`, governance/retrieval indexes |

Supported test-owned starts are blank/v0 and committed v1–v4. Each existing migration commits in
its own `BEGIN IMMEDIATE`; a failing version rolls itself back while earlier committed versions
remain. A recorded version above v4 raises `FutureSchemaVersion`.

The candidate compatibility mapping is one synthetic historical Conversation and one Turn per
legacy Session, with the Session represented by or linked to one RuntimeExecution. This is an M1
input, not final DDL. M1 must decide physical names/keys, durable linkage, provenance markers,
mixed-version sequencing, and backfill recovery. It must preserve messages, events, checkpoints,
call journals, summaries, leases, Memory records, v1 IPC, CLI/API Session IDs, traces, eval evidence,
and JSONL rebuildability.

Required fixtures cover terminal completed/failed, interrupted, retrying, uncertain,
`WAITING_APPROVAL` reconciliation, committed-response reuse, and mixed historical v1–v4 content.
No future DDL or production mapping was added in M0.

## Uncertain provider retry characterization

[`uncertain-retry-v1.json`](./evidence/v1-m0/uncertain-retry-v1.json) contains the normalized
requests, digests, stored journal content, attempt counters, selected events, and positive control.
The fake provider receives attempt 1 before simulated host loss. The persisted call is `running`.
Recovery emits `model_call_uncertain` and `retry_scheduled`; a test-owned admissible context marker
changes from A to B before resume.

The request ID remains `m0-uncertain-retry:model:1`, but the normalized digest changes from
`d4d04bdaafbe038962b2dfffe99c4b60c756e8928d662ae510b31eb951e49c83` to
`d31b3dffecc303831c98e6e4758e449dc2ec03bd6155cfe211b9540619ef2bc0`. Attempt becomes 2,
and stored `request_json` has the resumed digest. The protected source remains `stable\n`. This
proves reconstruction and overwrite, not reuse of the original exact payload. It does not prove
whether the remote provider executed or billed attempt 1.

The positive control crashes after durable response persistence. Resume performs zero provider
calls, reaches `completed`, and retains identical before/after request digest
`be2f49c7d7523e109b15b61342f2921fbdd32401b267af73b4653062e2de89a1`.

M3 owns the correction. M0 intentionally does not repair or bless this behavior as compatibility.

## Metrics and offline baseline

[`metric-dictionary-v1.json`](./evidence/v1-m0/metric-dictionary-v1.json) freezes units,
boundaries, formulas, populations, sources, aggregation, provenance, and unknown handling. It keeps
measured, estimated, unknown, and synthetic classifications separate. Usage-based prices are
estimates unless independently reconciled. Overlapping spans are not summed into elapsed time,
permission/user wait remains separate from machine work, and unrelated process-local clocks are
not subtracted across restart.

The read-only projection in
[`offline-baseline-v1.json`](./evidence/v1-m0/offline-baseline-v1.json) preserves each run and
separates normal, negative-control, and infrastructure-invalid populations. All 39 legacy Session
IDs, complete per-run metrics, tool/model counts, failure reasons, and oracle results are present.
Its independently recomputed count/total/mean/P50/P95 distributions match the evaluator report for
every checked metric. It identifies current
limitations: model latency is success-only; a legacy usage zero has no presence bit and is treated
as unknown; scripted nonzero usage is synthetic; legacy `permission_violations` counts denials and
does not establish an unauthorized effect; complete machine/user wait, recovery, and downtime
spans are unavailable. Production accounting was not changed.

| Lane | Population and outcome | Current timing observation |
|---|---|---|
| Complete scripted suite | 14/14 valid; 10/10 normal tasks succeeded and completed; 4/4 negative controls observed; 0 infrastructure invalid; source invariant 1.0 | legacy eval wall time mean 396.10 ms, P95 768.95 ms for normal runs |
| Five-case stability | 25/25 valid/successful/completed; five fresh repetitions per case; 0 infrastructure invalid; source invariant 1.0 | legacy eval wall time mean 579.15 ms, P95 765.58 ms |
| Calculator smoke | completed; 48 events; read/edit/test; final test passed; source unchanged | timing is not a release claim |
| Todo recovery smoke | completed; 72 events; test false then true; source unchanged | timing is not a release claim |
| Memory cold/warm regression | 12 pairs; cold 8/12, warm 12/12; recall/precision 1.0; injection 0; defaults off | deterministic trusted oracle and synthetic usage, not provider quality |

[`baseline-manifest-v1.json`](./evidence/v1-m0/baseline-manifest-v1.json) records commands,
configuration, environment, fixture and raw-output hashes, counts, and smoke/Memory results. The
sanitized raw `report.json`, `runs.jsonl`, and `manifest.snapshot.json` for both lanes are durable
under [`evidence/v1-m0/raw`](./evidence/v1-m0/raw/manifest-v1.json). Sanitization changes only each
absolute temporary `trace_path`; its manifest records original and stored hashes.
[`README.md`](./evidence/v1-m0/README.md) gives exact reproduction commands. Raw temporary files are
no longer the sole copy of any required baseline result.

The evaluator field named `end_to_end_latency_ms` starts before fixture resolution/fingerprinting
and ends after source-invariant and oracle execution. M0 therefore exposes it only as
`legacy_eval_wall_time_ms`. The canonical product `end_to_end_elapsed_ms` remains the separate
durable-acceptance-to-terminal metric and is unknown in this baseline. Production measurement code
was not changed.

The future M5 responsiveness plan is frozen at 100 measured observations per operation and load
class after 10 warmups, using a local monotonic clock around input acknowledgement, status
response, and stop acknowledgement. It will report P50/P95/max, environment/load, and misses
against P95 500 ms and max 2 seconds. Actual measurements are unavailable because M5 does not
exist.

## Representative real-task lane

[`representative-tasks-v1.json`](./evidence/v1-m0/representative-tasks-v1.json) pins six tasks from
the coding-agent and hermes-agent repositories across cross-file repair, regression-test addition,
and package/configuration work. All twelve base/reference commit objects resolve locally, and each
reference diff contains the named implementation or oracle paths. Each reference commit was then
exported with `git archive` into an isolated `/tmp` snapshot and its exact offline oracle executed.
The results were 32/32 and 3/3 unittest checks for the two coding-agent repair/audit tasks; a valid
v0.1.0 sdist/wheel, METADATA, release document, and relative links for the package task; and 59/59,
3/3, and 6/6 pytest checks for the three Hermes tasks. The manifest records exact commands,
interpreter/test-runner versions, dependency-manifest Git blob IDs, acceptance rules, tree IDs, and
per-task results. All offline oracle prerequisites were available. The sibling repository remained
read-only and no provider run was performed.

## Expected target gaps

[`expected_gaps.json`](../tests/fixtures/v1_m0/expected_gaps.json) records the required gaps:

| Gap | Owner |
|---|---|
| Multi-Turn Conversation continuity | M2 |
| Direct WorkspaceBinding | M2 |
| Ephemeral interactive startup | M5 |
| Product Conversation resume distinct from legacy execution resume | M2/M5 |
| Typed permission lifecycle | M4 |
| Frozen-request retry | M3 |
| Controlled code undo | M4 |
| Interactive responsiveness | M5 |

They are evidence boundaries, not placeholder implementation or activated work.

## Exit checklist

- [x] Frozen architecture and Python certification interpretation recorded; Accepted ADRs unchanged.
- [x] Runtime Kernel reliability assets mapped to passing preservation coverage.
- [x] Characterization tests pass; the single expected failure exercises and explains the M3 gap.
- [x] Uncertain provider retry reproduced without repair, including provider receipt and committed-response control.
- [x] Current-to-target and complete M1 migration inventories produced.
- [x] Full scripted and five-by-five stability baselines reproduced with durable sanitized raw files, linked Session IDs, and independently recalculable aggregates.
- [x] Six real-repository tasks pinned across two repositories and passed exact oracles in isolated historical snapshots.
- [x] Metric definitions separate measured, estimated, unknown, and synthetic values.
- [x] CLI/IPC, source isolation, protected goldens, schema v4, Memory defaults, and Runtime semantics remain intact.
- [x] Production testability changes: none.
- [x] Required tests, quality gates, coverage, native sandbox probe, Markdown validation, hash checks, and diff checks passed.
- [x] Evidence and reproduction instructions are durable and traceable.

M0 was accepted by the owner on 2026-09-21 and is complete. This acceptance does not activate M1;
M1 requires a separately issued and activated formal execution contract.
