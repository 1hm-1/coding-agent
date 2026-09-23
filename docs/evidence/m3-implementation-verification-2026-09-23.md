# Product-Layer M3 Implementation Verification — 2026-09-23

Status: **implementation verified; M3 ACCEPTED / COMPLETE** by owner on 2026-09-23. This record does
not activate M4–M7, open the M3+M4 real-tree mutation gate, or claim a live Provider run.

## Scope and immutable identities

- Base HEAD before M3 implementation: `4cb1b88bdcb308008c0e4d99fb0fd9d5e69d2069`.
- Schema: additive v7; 49 ordered DDL/index/trigger statements plus migration provenance/commit.
- Canonical contract SHA-256 after milestone-close status annotation: `82c3043243001b1c87d7b47a3d80c691fdcfd372f272ba640accf5c85d8a2905`.
- Design SHA-256 after milestone-close status annotation: `8740f2ec31fe6e62e7df317c42ccd324925455c823466ca12735337a6f16eba0`.
- No dependency, public Runtime IPC schema/vector, provider wire format, general Shell tool, or
  real-tree write path was added. Memory remains absent on the default Product composition path.

## Deterministic test results

- `PYTHONPATH=src python3 -m unittest discover -q`: **257 passed**, 0 failed, 0 errors, 0 skipped,
  0 expected failures. The former M3 uncertain-retry expected failure is a normal pass.
- `PYTHONPATH=src python3 -m unittest tests.test_m3_instructions tests.test_m3_product_context tests.test_m3_context -q`:
  **45 passed**. It covers exact-name/bounded/symlink discovery, trust and precedence/conflict,
  drift/refresh/exact resume, deterministic allocation/overflow/Memory-absent, artifact/file/
  Summary provenance, real Product-to-AgentRuntime freeze, dispatch/outcome, commit-before-call
  same-attempt recovery, atomic freeze fault rollback at every M3 component boundary, live
  post-Harness artifact projection, auxiliary Summary crash/reopen lineage, correlated conflict
  wait/resolution, separate-connection steering/refresh races, exact whole-request/token-schema
  accounting, bounded lossless file capture and immutable post-freeze retry, and identity-bound
  metric counts including fallback and committed-response reuse. Two adversarial multi-class
  exact-overflow cases verify every post-eviction reservation, lending, release, allocation,
  spent and unallocated ledger identity against the final included selections.
- Focused `unittest` execution of `tests.test_m3_context`, `tests.test_m3_product_context`, and
  `tests.test_persistence` covers legacy compatibility, Product integration and v7
  migration/reopen boundaries; the full run above is the authoritative count.
- `PYTHONPATH=src python3 -m unittest tests.test_persistence -v`: migration/fault/reopen and the
  pre-v7 in-flight exact-request compatibility import pass. The v7 fault loop injects every one of
  49 statement boundaries plus provenance/commit and confirms usable v6 rollback.
- Protected semantic goldens, Runtime IPC schemas/vectors/goldens, JSONL export/replay, provider
  adapters, legacy one-shot/resume, copied workspaces, M1/M2 lifecycle/recovery, and Memory cold/warm
  regressions are included in the 257-test full run and passed unchanged.

## Static, coverage, package, and smoke gates

- `.venv/bin/ruff check src tests examples/m2_direct_tree_manifest.py`: passed.
- `.venv/bin/mypy`: passed, **44 configured source files**. This includes both new M3 modules and
  all configured persistence/Product files. `runtime.py` remains outside the repository's known
  configured-mypy scope; it is covered by Ruff, compileall, focused recovery, and the full suite.
  An additional direct `mypy --follow-imports=silent src/coding_agent/runtime.py` audit reports
  33 typing errors in that unconfigured file; it is not represented as a passing gate.
- `python3 -m compileall -q src tests examples`: passed.
- `COVERAGE_FILE=/tmp/coding-agent-m3-sol-medium-ledger-final.coverage PYTHONPATH=src .venv/bin/coverage run -m unittest discover -q`
  followed by `COVERAGE_FILE=/tmp/coding-agent-m3-sol-medium-ledger-final.coverage .venv/bin/coverage report -m`:
  **81.4%**, above the configured 70% threshold. The new coverage-file path was confirmed absent
  before the run, so no earlier data was combined. M3-owned coverage: persistence 83.5%,
  Product application 92.9%, Product context 94.0%, Product domain 90.2%, Product instructions
  81.6%, Product repository port 100%, migrations 95.4%, Runtime 82.5%. The earlier 81.7%
  report was stale/combined and is superseded by this fresh run.
- `UV_CACHE_DIR=/tmp/coding-agent-uv-cache /home/hmli/.local/bin/uv build --out-dir <fresh-/tmp-dir>`
  produced wheel and sdist. A fresh `/tmp` venv installed the wheel with `--no-deps` and imported
  `product_context`, `product_instructions`,
  `product_application`, and `persistence`: `m3-package-smoke-ok`.
- Calculator copied-workspace scripted smoke: COMPLETED, 4 model calls / 3 tool calls. Todo
  copied-workspace recovery smoke: COMPLETED, 6 model calls / 5 tool calls. Source fixtures stayed
  outside Runtime writes; full M2 manifest/read-only tests also passed. The P2-M2 cold/warm Memory
  benchmark completed with no unrelated behavior changes; its latency values remain environment-dependent.
- `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3 examples/m2_direct_tree_manifest.py protocol/v1`:
  unchanged (9 entries, 10 Git paths; manifest SHA-256
  `0d270bfb0f610b9d3eeb580f1435e1483a249c8269bb6a1c296782c80cd26d73`). M3 direct-binding
  overflow and two-generation native compaction tests also compare complete pre/post tree manifests.
  The first manifest attempts against the example source directories were not valid smokes because
  `compileall` had populated ignored binary `__pycache__` files and the helper's first-readable-file
  probe selected one; the clean protocol fixture passed. No user source was modified.

## Recovery, atomicity, and metric coverage

- The model-start journal transaction publishes FrozenModelRequest, ContextManifest, composition/
  selection evidence, Runtime event/checkpoint, and first attempt intent or none of them. The
  provider gate is a separate append-only dispatch fact. Crash after commit but before dispatch
  resumes the same attempt and exact request without ContextBuilder recomposition.
- A dispatched request with unknown outcome receives a censored outcome and a new attempt over the
  byte-identical immutable request. A post-dispatch live-file mutation does not cause Product
  context reread or block exact retry; the dispatched retry request is proven equal to frozen JSON.
  Known failure/fallback histories remain append-only. A committed response is reused with zero
  provider calls and a distinct idempotent reuse count. Startup imports exact pre-v7 request bytes before
  resume. Immutable request/context/attempt rows have no-update/no-delete enforcement.
- Freeze revalidates Conversation product version, semantic frontier, Turn/Execution and active
  manifest revision inside the publishing `BEGIN IMMEDIATE`. A second SQLite connection appending
  valid SteeringInput between composition and freeze forces stale-candidate rejection and bounded
  recomposition; the one dispatched request contains the new constraint. A concurrent refresh
  during `CALLING_MODEL` is rejected by its safe-boundary gate, leaving the current manifest intact.
- Summary compression prepares an exact auxiliary request without calling its backend, commits the
  auxiliary request/manifest/attempt with `COMPRESSION_STARTED`, appends dispatch immediately before
  invocation, and appends success/failure/unknown outcome metrics. Each native SummaryArtifact binds
  the exact auxiliary request and successful attempt plus the frozen origin-contiguous semantic-prefix
  digest/range; concurrent prefix drift rejects publication. Validated generated content and typed
  claims are published only on success. A crash before dispatch reopens
  and invokes the byte-identical request once without consuming an Agent request ordinal; failure
  leaves the transcript and last valid Summary unchanged.
- Every terminal/unknown model attempt records `model_attempt_latency_ms` and `token_usage` with
  measured, synthetic, unknown, censored, complete/incomplete coverage as applicable. Auxiliary
  success/failure have separate populations and available Conversation/Turn/Execution/request/attempt
  identities. Every frozen composition records the M0 `context_compaction_latency_ms` identity with
  explicit unknown timing where no trustworthy monotonic boundary exists; overflow and stale rebuild
  are separate failure operations. Per-section estimates are retained. Known failure to fallback
  to success retains two attempt outcomes over one request plus one `model_fallback_count` sample
  bound to the failed attempt and backend pair; that explanatory count does not duplicate latency
  or usage. Auxiliary context operations start pending and transition to the actual successful or
  failed outcome; an unconfigured compaction rejection without auxiliary request gets its own
  Product-linked operation/sample. Compaction duration remains null/incomplete when whole-operation
  timing was not measured. Idempotent `count` samples cover frozen requests, manifests, context
  operation outcomes, omitted/stale sources with reasons, retries, attempt outcomes, auxiliary
  compaction outcomes, fallback, committed-response reuse, and safe-boundary cancellation, with all
  available Conversation/Turn/Execution/request/attempt/context identities. Retry-recovery overhead
  is explicitly unknown where a cross-process monotonic span is unavailable; it is not imputed as
  zero. Missing provider usage is never zero.

## Instruction, context, and read-only evidence

- Discovery accepts only exact `AGENTS.md`, explicit repository/project/binding roots and contained
  paths. Bounds are 256 KiB/source, 64 sources and 1 MiB total. Lstat/read/decode/type/count/byte and
  escaping/broken/cyclic symlink failures are durable unavailable evidence.
- Trust is repository/scope and optional binding scoped and changes only soft context eligibility.
  Immutable snapshots, precedence/override edges, conflicts, status history, path activation,
  staleness and canonical idempotent refresh are persisted. Exact resume verifies snapshot digest,
  byte length and UTF-8 without substituting live content. Trust is exact provider/source-kind
  matched. Bounded explicit-modal prose contradictions as well as structured `rule.*` conflicts
  produce stable evidence; unrelated prose remains additive. Conflicts use the existing correlated
  `WAITING_USER_INPUT` state and invoke no provider until a correlated UserReply durably resolves
  the conflict in a successor manifest. Staleness remains sticky until explicit refresh even if
  bytes revert.
- ContextComposer is stateless and deterministic. Required system/runtime/current-intent/
  instruction/protocol/resolver/constraint content is non-evictable. Tool schemas, output reserve
  and framing margin are charged first. The complete normalized message sequence is counted in one
  provider request-counter call where supported; otherwise message counting and a named conservative
  tool-schema charge are recorded. Provider/model/capability source/version/counter identity and tool
  accounting accompany the immutable manifest. A complete-request overflow is explicit and invokes
  no provider.
  Optional selection is explained by stable class/source records; default Memory is absent.
- ToolResultArtifact and NormalizedObservation are projected atomically from the post-Harness
  Runtime result. Bounded/truncated capture is `incomplete`; a pre-v7 result whose original capture
  boundary is unknowable reopens as `unknown`. Native Conversation compaction freezes the auxiliary
  request before summarizer dispatch and publishes only validated success over an exact
  origin-contiguous semantic prefix. Its typed claims carry exact event ranges and artifact/revision
  provenance; a second end-to-end Runtime compaction over a longer prefix supersedes the first,
  preserves parent lineage and transcript bytes, and only the current valid summary is selected
  after reopen. Affected-only code staleness is tested. Failed generation keeps the
  last valid SummaryArtifact; ID/content collisions fail closed. Legacy SummaryRecord remains
  execution-scoped compatibility evidence. FileContextItem and the other records keep capture
  completeness, prompt truncation, range/revision and source provenance separate. File capture uses
  bounded reads and persists selected bytes, encoding and range losslessly in the frozen model-visible
  source. All Product
  real-tree operations are read-only; durable artifacts remain in agent-owned SQLite.

## Final repository hygiene

- `git diff --check`: passed.
- Changed-Markdown local-link/fence audit: passed.
- Tracked source/test/example files contain no coverage, build, egg-info, bytecode, cache or
  temporary artifacts. M3 build/install products and coverage data are under `/tmp`; `uv build`
  refreshed ignored `src/production_coding_agent.egg-info/` metadata. The pre-existing ignored
  `.venv/` and `dist/` directories were not claimed as output.
- Verification itself did not commit or push. The owner subsequently accepted M3 and authorized one
  milestone-close commit/push; M4–M7 remain inactive.
