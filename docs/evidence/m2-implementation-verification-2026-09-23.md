# M2 implementation verification — 2026-09-23

Status: **M2 ACCEPTED / COMPLETE** on 2026-09-23. M1 remains Accepted/complete; M3–M7 remain
inactive, and M3 is not issued or activated by this acceptance. The implementation verification
was produced before milestone-close publication; no live Provider was run because the M2 contract
supplied no new Provider hypothesis.

## Deterministic results

- `PYTHONPATH=src python3 -m unittest -v tests.test_m2_product_lifecycle tests.test_m2_recovery tests.test_persistence`:
  48/48 passed. This covers the v6 statement/provenance/commit fault matrix; before/after seams for
  every outer admission component on both new and existing idle Conversations; every internal
  Session component; concurrent typed Runtime input; terminal/input races; bounded direct
  observation; directory-aware manifests; writer/recovery paths; and restart repair.
- `PYTHONPATH=src python3 -m unittest discover -v`: 223 run, 222 passed, exactly one preserved
  expected failure, `test_future_m3_invariant_reuses_exact_frozen_request`. There were no skips or
  unexpected failures.
- The explicit 23-test compatibility command covering four semantic goldens, Runtime IPC v1
  producer/schema/vectors, JSONL export/replay, and the M3 uncertain-retry invariant passed 22 with
  that same one expected failure.
- `.venv/bin/ruff check src tests examples/m2_direct_tree_manifest.py`: passed. Targeted typing for
  the eight M2-owned domain/migration/persistence/Product/trajectory modules passed with
  `.venv/bin/mypy src/coding_agent/domain.py src/coding_agent/migrations.py src/coding_agent/persistence.py src/coding_agent/product_application.py src/coding_agent/product_domain.py src/coding_agent/product_persistence.py src/coding_agent/product_workspace.py src/coding_agent/trajectory.py`.
  `compileall` over source, tests, demos, benchmarks, and the M2 manifest helper passed.
- `COVERAGE_FILE=/tmp/coding-agent-m2-final.coverage PYTHONPATH=src .venv/bin/coverage run -m unittest discover`
  followed by `coverage report`: 80.0% statement coverage, above the configured 70% threshold.
  M2-owned module coverage was persistence 81.2%, Product application 89.9%, Product domain 92.0%,
  Product persistence 100%, Product workspace 79.5%, and Runtime 80.4%.

## Package, smoke, and gate closure

- `uv build` initially could not fetch `setuptools>=77` under restricted network. The approved
  network retry succeeded. Wheel SHA-256 is
  `664ef58b89cfa2821f16357b0b92025c3ef299f1c828736705b06f44797875f5`; sdist SHA-256 is
  `973ff9dca97042028424dc545a5bb47bc751486f51116288aaa516125fc8074d`.
- A fresh `/tmp/coding-agent-m2-solmedium-venv` installed the wheel with `--no-deps`; imports of
  `product_application`, `product_domain`, `product_persistence`, and `product_workspace` passed.
- Calculator copied-workspace smoke completed in 16 steps/4 model calls/3 tool calls. Todo
  copied-workspace smoke completed in 24 steps/6 model calls/5 tool calls. Both ended `completed`.
- `PYTHONPATH=src .venv/bin/python examples/memory_cold_warm_benchmark.py` passed. Its 12-case warm
  arm retained recall/precision 1.0, irrelevant injection 0.0, and cold/warm task success
  0.667/1.0. A separately captured output hash was
  `f5afc87c5ad3f7e025d22aa4eb3c498b21413de324501c0c8abf791af0dbdc85`; latency is machine-local
  and is not used as an M2 benefit claim.
- `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src .venv/bin/python examples/m2_direct_tree_manifest.py /home/hmli/code/coding-agent`
  is the checked-in direct-tree gate. It performs a Harness/Guard read, rejects every M2-prohibited
  side-effect class, and compares files, empty directories, symlink targets, Git refs/index, and
  lock-path presence before/after. It passed on the final document content; its emitted hash is an
  ephemeral digest of the entire dirty tree and is intentionally not copied back into that tree.

## Acceptance mapping and limits

- A mapped terminal Runtime journal mutation and Turn finalization now share one transaction.
  Internal safe-boundary steering selects and consumes the current ordered batch in one transaction,
  so concurrent valid input cannot fail Runtime through a stale Product version. Two concurrently
  accepted inputs reach the real `AgentRuntime`/`ContextBuilder` exactly once; finalization closes
  any accepted undelivered control, and the concurrent input/finalization test proves a single
  durable order.
- Linked-worktree refs resolve through the common directory. Incomplete filesystem observations
  retain explicit frontiers/exclusions; the 10,000-entry frontier counts directories, files, and
  directory symlinks. The standalone manifest includes directory lstat mode/type evidence and
  detects chmod drift. Admission persists the actual fresh observation and its checkpoint frontier;
  rebind/resume/writer operations revalidate fresh facts.
- Execution-owned writers release in live terminal, stable user/permission wait, and interrupt
  transactions. Restart repair is fallback only, and release never clears a recovery barrier.
- Legacy `WAITING_APPROVAL` supports both the current and exact earlier v6 resolver tuples. Missing
  or contradictory evidence creates one idempotent fail-closed binding sentinel; both resume and
  writer acquisition remain blocked across reopen.
- The direct Product surface remains read-only. M2 adds no general Shell, permission policy,
  instruction discovery, Frozen ModelRequest, diff/undo, interactive CLI, default Memory, public
  IPC field, or real-tree mutation. Ordinary content/index drift is observed and invalidates old
  facts; identity/topology or branch/HEAD drift rejects silent execution resume.

## Final integrity

- Changed-document relative-link and fence audit: passed.
- `git diff --check`: passed.
- Final checked-in direct-tree manifest: passed with no changed entry.
- Repository-root coverage/build artifact scan: no `.coverage*`, `build`, or newly created
  `production_coding_agent.egg-info` remained. The pre-existing ignored `UNKNOWN.egg-info` was not
  altered or represented as M2 output.
