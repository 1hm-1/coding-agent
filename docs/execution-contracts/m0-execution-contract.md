# M0 Execution Contract — Architecture Freeze + Characterization

Issued status: **ACTIVE — M0 only** (historical at issuance)

Completion status (2026-09-21): **ACCEPTED / COMPLETE**

> Completion annotation: the milestone owner accepted the M0 deliverables on 2026-09-21. The
> issued contract body below is preserved as the historical authorization and execution record;
> its present-tense authorization clauses do not reactivate M0. M1–M7 remain inactive, and M1
> requires a separately issued and activated formal execution contract.

> Later activation annotation (2026-09-22): the formal
> [`M1 execution contract`](./m1-execution-contract.md) has been issued and M1 is now **ACTIVE**.
> The preceding text remains the historical M0-issued state; this publication turn starts no M1
> implementation, leaving current code and Schema v4 at the M0-characterized legacy baseline.

Executor: **Sol High Codex session**

Milestone owner: the coordinating/review session

Repository: `/home/hmli/code/coding-agent`

You are authorized to execute this contract. M1–M7 remain inactive. Completion of M0 does not authorize starting M1.

The prerequisite wording clarifications and activation records have been updated. No implementation code, schema, tests, or Accepted ADRs were changed during that preparation.

## 1. Objective and frozen interpretation

Establish an evidence-backed characterization of the current implementation before introducing the new product-layer domain spine.

M0 must establish:

- What works today, including failure and recovery behavior.
- Which Runtime Kernel assets must survive the refactor.
- Where current implementation differs from the Accepted target architecture.
- What M1 must account for when introducing additive persistence mappings.
- Reproducible baseline evidence and explicit measurement limitations.

Two interpretations are frozen:

1. M5’s **P95 ≤ 500 ms and every observation ≤ 2 s** thresholds apply only to local control-plane responsiveness: input acknowledgement, status response, and stop acknowledgement. They do not constrain model-provider latency, tool completion latency, or safe cancellation completion.
2. Python is the **V1 certified capability scope**, not an architectural restriction. The domain architecture remains ecosystem-agnostic; additional language/tooling profiles must not require domain-model redesign. Such profiles still require separately authorized implementation and acceptance evidence.

Architecture and V1 delivery scope are closed. M0 is characterization, not another architecture workshop.

## 2. Exact allowed scope

You may:

- Inspect repository code, tests, documentation, schemas, fixtures, and existing evidence.
- Add focused characterization tests and test-owned fixtures.
- Reuse existing scripted providers, fake transports, fault injectors, clocks, journals, and sandbox test helpers.
- Add small offline evidence collectors/postprocessors when existing commands cannot produce the required characterization artifacts.
- Execute existing migrations against disposable test databases.
- Create the current-to-target inventory, M1 migration inventory, metric dictionary, baseline bundle, representative-task manifest, and M0 checklist/report.
- Update current-state, handoff, roadmap status, and document navigation to reflect verified M0 results.

Preferred locations:

- `tests/test_v1_m0_*.py`
- `tests/fixtures/v1_m0/`
- Test-only helpers under `tests/`
- `examples/v1_m0_*.py`, only if an offline runner/postprocessor is necessary
- `docs/coding-agent-v1-m0-characterization.md`
- `docs/evidence/v1-m0/`

Prefer new focused tests over modifying existing regression assertions.

### Narrow testability exception

Production changes are permitted only when an essential characterization cannot reasonably use existing injection seams or test-side techniques.

For such a change:

- Keep it confined to an existing component.
- Preserve default behavior, public interfaces, FSM decisions, persistence semantics, event payloads, permissions, and side effects.
- Prefer an optional no-op hook or injectable dependency over structural refactoring.
- Explain why existing seams were insufficient.
- Provide before/after behavioral-equivalence evidence.
- Report every changed production symbol explicitly.

This is not permission to fix behavior, optimize performance, add production instrumentation, or prepare M1 scaffolding.

## 3. Forbidden changes

Do not:

- Implement Conversation, Turn, WorkspaceBinding, coordinator, admission, permission, context, undo, or interactive product features.
- Add production domain entities, placeholder product packages, repository interfaces, or future schema scaffolding.
- Introduce migrations, change schema versions, or migrate user-owned databases.
- Rename or remove legacy Session, APIs, CLI commands, events, or persistence fields.
- Fix the uncertain-provider-retry gap. Its implementation owner remains M3.
- Change production metric collection, missing-usage normalization, retry policy, or permission accounting.
- Enable real-working-tree mutation, arbitrary shell, new command capabilities, background processes, or new ecosystems.
- Change Memory defaults or restart retrieval research.
- Add dependencies/frameworks, alter lockfiles, or expand quality-tool configuration.
- Rewrite Accepted ADRs, historical evidence, or golden fixtures to obtain passing results.
- Run paid/live providers merely because credentials exist.
- Modify `../hermes-agent`, discard pre-existing changes, commit, or push without separate authorization.

Existing migrations may run in disposable fixtures; that is not authorization for a new migration.

## 4. Required repository inspection

Before editing, fully read:

- `AGENTS.md`
- `docs/HANDOFF.md`
- `docs/current-state.md`
- `docs/README.md`
- [V1 implementation roadmap](../coding-agent-v1-implementation-roadmap.md), especially M0 and the measurement contract
- [V1 capability matrix](../v1-development-capability-matrix.md)
- `docs/target-architecture-snapshot.md`
- `docs/architecture-consistency-audit.md`
- All eight Accepted product ADRs identified by the audit
- `docs/protocol/runtime-ipc-v1.md`
- Relevant completed P2-M1/P2-M2 records, testing guidance, and baseline evidence

Inspect at least these implementation surfaces and their tests:

- `domain.py`, `runtime.py`, `application.py`
- `persistence.py`, `migrations.py`
- `workspace.py`
- `tools/`, `sandbox/`, command/test profiles
- `models/`, retry and fallback adapters
- `context.py`, `compression.py`
- `trajectory.py`, `export.py`, `evaluation.py`
- `cli.py`, `protocol/`, `protocol/v1/`
- Default Memory composition and governance boundaries
- `.github/workflows/ci.yml`, `pyproject.toml`

Record Git HEAD, dirty status, relevant tracked/untracked content hashes, interpreter/tool versions, and sandbox capabilities **before modifications**. HEAD alone is insufficient provenance for this dirty worktree.

Historical test counts are reference information, not the current result. Discover and report actual counts.

## 5. Runtime Kernel assets to protect

Preserve:

- Explicit FSM transitions and the single Agent execution loop.
- SQLite authority; atomic state/checkpoint/event/message/call mutations.
- Optimistic version checks, execution leases, and stale-owner rejection.
- Model/tool journals and committed-response reuse.
- Bounded retry/fallback and classified failures.
- ToolHarness as the sole runtime side-effect gateway.
- Workspace containment, revision checks, copied-workspace source protection.
- Sandbox fail-closed behavior, resource limits, and descendant cleanup.
- Provider-specific formats confined to adapters.
- Context budgets, required-content protection, summary lineage, and raw-history preservation.
- JSONL as a rebuildable projection and semantic replay.
- Existing CLI, Runtime IPC v1, eval/oracle contracts.
- Dormant Memory governance and existing negative evidence.

Preserve the four original semantic goldens unchanged:

- `bugfix_success.json`
- `test_failure_recovery.json`
- `permission_denied.json`
- `runtime_failure.json`

Preserve existing IPC schemas, vectors, and IPC golden fixtures as well.

## 6. Characterization-test requirements

Reuse existing coverage when it proves the required behavior. Add tests for missing evidence rather than duplicating the suite.

| Area | Required characterization |
|---|---|
| One-shot execution | Session creation, isolated workspace, read/edit/test/final flow, source unchanged |
| FSM and budgets | Legal/illegal transitions, budget exhaustion, classified failures, Runtime completion versus task success |
| SQLite | Existing migration starting points, idempotence, future-version rejection, atomic rollback, sequence/version conflicts |
| Recovery | Interrupt/resume, lease takeover, committed-response reuse, model/tool crash boundaries |
| Tools and safety | Validation, permission denial, structured profiles, containment, timeout/cleanup, uncertain effects and reconciliation |
| Context | Budget overflow, required retention, summary lineage/staleness, raw-history preservation |
| Public compatibility | CLI behavior, IPC schemas/vectors/results/redaction, export/replay and JSONL rebuilding |
| Defaults | Memory-off application/headless behavior; no accidental feature activation |

For each relevant fault case, assert:

1. Durable state and journal evidence.
2. Observable result/error behavior.
3. Filesystem/process effects or their absence.
4. Recovery or explicit refusal behavior.

Use deterministic injection at meaningful boundaries. Do not use arbitrary sleeps to manufacture races.

Create a separate expected-gap catalog covering at least:

- Multi-Turn Conversation continuity.
- Direct WorkspaceBinding.
- Ephemeral interactive startup.
- Product Conversation resume versus legacy execution resume.
- Typed permission lifecycle.
- Frozen-request retry.
- Controlled code undo.
- Interactive responsiveness.

These are future gaps, not features M0 must implement. Avoid placeholder tests that merely assert constants or import nonexistent future modules. Any expected failure must exercise a real relevant path and identify its owning milestone.

## 7. Uncertain-provider-retry gap

Investigate and characterize the observed path:

- `AgentRuntime.resume()` can rebuild `_model_input`.
- `_on_calling_model()` can reconstruct a request for retry.
- `SQLiteRunJournal._upsert_model_call()` can overwrite `request_json` under the existing request ID.
- Existing uncertain-retry coverage verifies uncertainty/retry, but not original-input identity.

Required evidence:

1. Capture the original committed normalized request and digest.
2. Inject a crash after request persistence but before durable response persistence.
3. Include a fake-provider case where the provider receives the request before simulated host loss.
4. Change an admissible test-controlled context input before resume, without changing protected source data.
5. Capture the resumed request, request ID, attempt counters, journal events, and stored request contents.
6. Demonstrate whether the original payload is reconstructed or overwritten.
7. Retain a positive control proving that an already committed response is reused without another provider call.

Use existing seams first; no real provider is necessary.

Produce a passing current-behavior reproducer and an explicit **M3-owned correctness gap**. A narrowly scoped expected-failure assertion for the future invariant is acceptable if the failure cause is verified.

Do not make the defect a permanent compatibility requirement. Do not claim remote exactly-once execution or known billing for an uncertain attempt.

If evidence differs from the anticipated gap, report the actual behavior rather than forcing the expected conclusion.

## 8. Current-code → target-architecture inventory

Produce a source-referenced table with:

- Current component/symbol and authority.
- Actual behavior and preservation tests.
- Corresponding Accepted target responsibility.
- Classification: retained asset, legacy compatibility, target gap, or deferred representation.
- Owning future milestone.
- Compatibility implications and unresolved implementation decisions.

Cover Session/RuntimeSnapshot, application entry points, workspace management, execution leases, messages/events, model/tool journals, context/summaries, permissions, CLI/IPC, and Memory.

Current-versus-target differences are not automatically ADR contradictions.

## 9. M1 migration inventory

Document, without implementing:

- Actual schema version and all existing migration starting points.
- Tables, columns, keys, constraints, indexes, serialized records, and relevant versions.
- Which legacy identifiers appear in databases, APIs, CLI, IPC, traces, and evidence.
- Candidate mappings from legacy Session to RuntimeExecution and synthetic historical Conversation/Turn representations.
- Preservation requirements for messages, events, checkpoints, model/tool calls, summaries, leases, and Memory records.
- Fixtures needed for terminal, interrupted, retrying, uncertain, reconciliation, and mixed historical data.
- Atomic failure/rollback expectations and unsupported-version behavior.
- Payloads/readers that must remain available during migration.
- Representation decisions that belong to M1.

Do not write future DDL, migration code, production mappings, or finalized physical storage designs.

## 10. Compatibility obligations

Throughout M0:

- Legacy one-shot/headless execution retains copied workspaces and read-only source semantics.
- `run_task()`, `resume_session()`, existing CLI commands, and IPC v1 retain their current meaning.
- Legacy `WAITING_APPROVAL` remains reconciliation behavior; do not turn it into ordinary permission approval.
- JSONL remains derived from SQLite.
- Runtime completion is not task success.
- Historical data and evidence are not rewritten into future product terminology.
- Python certification does not introduce ecosystem-specific restrictions into the domain model.

The future M3+M4 direct-working-tree gate remains closed.

## 11. Metric and baseline work

Create a versioned metric dictionary specifying units, boundaries, formulas, populations, sources, provenance, aggregation, and unknown handling.

Cover:

- Task/oracle success and Runtime completion.
- Model/tool attempts, failures, retries, timeouts, cancellation, and uncertainty.
- Model/tool/context/compaction latency.
- End-to-end elapsed, machine execution, permission/user waiting, backoff, recovery, and downtime.
- Input/output/cache/compaction tokens and available attribution.
- Derived cost and rate assumptions.
- Permission outcomes versus actual unauthorized effects.
- Future local control-plane acknowledgement measurements.

Requirements:

- Distinguish `measured`, `estimated`, and `unknown`.
- Label scripted usage as synthetic fixture data, not real-provider usage.
- Never convert missing usage into measured zero.
- Never present incomplete totals as complete.
- Separate user/permission waiting from machine execution.
- Avoid double-counting overlapping spans.
- Do not subtract unrelated process-local clocks across restart.
- Treat usage-based price calculations as estimated cost unless independently reconciled.
- Preserve all failures and report negative-control/infra-invalid populations separately.

Characterize the current success-only model-latency collection, missing-usage zero encoding, and denial-based legacy `permission_violations` field. Use a separate read-only evidence projection; do not fix production accounting in M0.

### Required baseline lanes

**Offline baseline**

- Run the existing complete scripted eval suite.
- Reproduce the five normal resume-benchmark tasks with five fresh repetitions each.
- Preserve configuration, fixture hashes, outputs, failures, and environment provenance.
- Demonstrate repeatability of deterministic semantics; measured timing need not be identical.

**Representative real-task manifest**

Freeze at least six tasks from at least two real Python repositories, covering at least three task types such as cross-file repair, regression-test addition, and package/configuration work.

For each task, record immutable revision/content identity, provenance, exact request, oracle, dependency/environment requirements, budgets, and intended evaluation lane. Validate the oracle/setup against available snapshots; explicitly report unavailable prerequisites.

These are not six newly implemented agent runs. M0 does not require paid provider execution or future interactive capabilities.

Public reference retrieval is allowed under normal access/approval rules. Do not substitute generated mini-fixtures and call them real-repository evidence.

Freeze the future M5 control-plane measurement method and sample plan, but mark actual interactive measurements unavailable until that implementation exists.

## 12. Required commands and checks

Run from the repository root. Capture commands, exit codes, actual counts, skips, expected failures, and artifact locations.

Before changes and again after changes:

```bash
git status --short
git rev-parse HEAD
git diff --check
python3 --version
PYTHONPATH=src python3 -m unittest discover -v
```

Run focused characterization and preserved recovery/golden/IPC suites:

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -p 'test_v1_m0_*.py' -v
PYTHONPATH=src python3 -m unittest tests.test_hardening tests.test_m2_recovery tests.test_persistence tests.test_protocol -v
```

Run existing quality gates:

```bash
.venv/bin/ruff check src tests examples/todo_cli examples/mini_repos examples/memory_cold_warm_benchmark.py examples/memory_retrieval_holdout.py
.venv/bin/mypy
PYTHONPATH=src python3 -m compileall -q src tests
```

Include any added evidence-runner files in lint/compile checks. Use the existing configuration; do not relax it.

Use a fresh temporary root for execution artifacts:

```bash
m0_run_root=$(mktemp -d /tmp/coding-agent-v1-m0.XXXXXX)
export COVERAGE_FILE="$m0_run_root/.coverage"

PYTHONPATH=src .venv/bin/coverage run -m unittest discover -v
.venv/bin/coverage combine
.venv/bin/coverage report

PYTHONPATH=src python3 -c 'from coding_agent.sandbox import LinuxNamespaceExecutor; import json; print(json.dumps(LinuxNamespaceExecutor().capabilities().to_dict(), sort_keys=True))'

PYTHONPATH=src python3 -m coding_agent.cli \
  --agent-home "$m0_run_root/full-suite" \
  evaluate --suite examples/eval_suite.json \
  --variant budgeted --repetitions 1 \
  --output "$m0_run_root/full-suite/report"

PYTHONPATH=src python3 -m coding_agent.cli \
  --agent-home "$m0_run_root/stability" \
  evaluate --suite examples/eval_suite.json \
  --variant budgeted --repetitions 5 \
  --case-id calculator-success \
  --case-id todo-recovery \
  --case-id pipeline-multi-file-recovery \
  --case-id settings-scoped-edit \
  --case-id search-lab-content-location \
  --output "$m0_run_root/stability/report"
```

Verify the selected manifests use scripted backends before execution. Do not add provider overrides.

Also run the existing calculator/todo scripted smoke and Memory cold/warm regression benchmark documented in the repository. Use fresh agent homes.

If tooling or native sandbox capabilities are unavailable, report the exact limitation and seek the required environment/approval. Do not use a host-execution fallback or count skipped security checks as passed.

Finally validate Markdown links/fences, `git diff --check`, and protected-file hashes.

## 13. Required deliverables and final report

Deliver:

- M0 characterization report/checklist.
- Current-to-target inventory.
- M1 migration inventory.
- Versioned metric dictionary and baseline manifest.
- Representative real-task manifest.
- Uncertain-retry reproducer and evidence.
- Durable baseline results with raw-artifact hashes and reproduction instructions.

Temporary execution directories are acceptable; **final evidence must not exist only under `/tmp`**. Preserve sanitized evidence in the repository or a declared durable location.

Your final report must include:

1. Exact changed files, separated from pre-existing changes.
2. Every production testability change, its necessity, and equivalence evidence—or explicitly “none.”
3. Before/after test counts, failures, skips, expected failures, quality results, and sandbox capability.
4. Four semantic-golden and IPC compatibility results.
5. Schema/version and protected-file hash checks.
6. Retry-gap observations with request digests, attempts, and journal evidence.
7. Baseline configuration, populations, measured values, unknown coverage, and limitations.
8. Inventory/artifact paths and reproduction commands.
9. Remaining gaps with milestone ownership.
10. Any unmet M0 exit criterion.

Do not claim future functionality, general coding success, or performance improvements from scripted characterization.

## 14. M0 exit criteria and stop condition

M0 is ready for owner acceptance only when:

- Frozen architecture and certification scope are recorded without ADR changes.
- Existing reliability assets have explicit preservation coverage.
- Characterization tests pass; known expected failures are individually explained.
- The uncertain-provider-retry gap is reproducibly characterized, not repaired.
- Current-to-target and M1 migration inventories are complete.
- Offline baselines are reproducible and representative real tasks are pinned with usable oracle/setup evidence.
- Metric definitions distinguish observed, estimated, missing, and synthetic evidence.
- Legacy CLI/IPC, source isolation, goldens, schema, Memory defaults, and Runtime semantics remain intact.
- Any testability changes satisfy the narrow exception and are fully reported.
- Required checks have verified evidence; missing checks are not silently waived.
- Final evidence is durable and traceable.

If repository evidence reveals a genuine contradiction between Accepted domain decisions, stop the affected work and present the conflicting statements and reproducer to the milestone owner. Do not edit ADRs yourself.

**End by reporting “M0 ready for owner acceptance” or the specific unmet criteria. Stop there. Do not begin M1.**
