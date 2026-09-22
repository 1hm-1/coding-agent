# Product-Layer M1 Implementation Verification — 2026-09-22

Status: verification complete; **M1 is ACCEPTED / COMPLETE**. This is implementation evidence,
not activation of M2–M7.

## Scope evidence

- V5 is additive: it adds generated RepositoryIdentity registry/descriptors, ProjectScope,
  WorkspaceBinding, Conversation, Turn, private RuntimeExecution mapping, ordered semantic events,
  and observable mapping-recovery records. Legacy Runtime `sessions` remains authoritative for its
  FSM and old readers remain in place.
- The focused M1 suite covers generated/persisted identity and descriptor round-trip across reopen,
  non-Git identity, linked-worktree common-dir discovery, explicit descriptor-conflict rejection,
  same remote/history independent clones, V1–V4 upgrade, terminal/failed/interrupted/retry/waiting
  legacy states, retained Memory rows, synthetic provenance, atomic/idempotent mapping retry,
  copied-workspace truthfulness, source read-only behavior, post-run mapping recovery without
  changing a RunResult, and fail-closed corrupted/mismatched Product aggregates.
- `tests.test_persistence` injects V5 failures before/after every DDL/index statement boundary and
  at both provenance-insert and pre-commit seams. It proves rollback retains the V1–V4 provenance
  and a real legacy Session/checkpoint that successfully loads after a later healthy upgrade.
  Unknown future schema rejection remains covered.

## Commands and observed results

| Gate | Command / result |
|---|---|
| focused M1 + persistence | `PYTHONPATH=src .venv/bin/python -m unittest tests.test_persistence tests.test_m1_product_layer -v` — 23 passed |
| full regression, goldens, IPC, replay, compatibility | `PYTHONPATH=src .venv/bin/coverage run -m unittest discover -v` — 197 run: 196 passed, 1 expected failure (`tests.test_v1_m0_uncertain_retry...future_m3_invariant...`), 0 unexpected failures |
| explicit goldens / IPC / replay compatibility | `PYTHONPATH=src .venv/bin/python -m unittest tests.test_hardening tests.test_protocol tests.test_v1_m0_contract -v` — 27 passed; four protected semantic goldens, IPC schemas/vectors and SQLite replay compatibility remain verified |
| Ruff | `.venv/bin/ruff check src tests examples/todo_cli examples/mini_repos examples/memory_cold_warm_benchmark.py examples/memory_retrieval_holdout.py` — passed |
| configured + targeted M1 typing | `.venv/bin/mypy` — no issues in 40 configured source files; `.venv/bin/mypy src/coding_agent/application.py src/coding_agent/migrations.py src/coding_agent/persistence.py src/coding_agent/product_domain.py src/coding_agent/product_persistence.py` — no issues in 5 files |
| bytecode / diff hygiene | `PYTHONPATH=src python3 -m compileall -q src tests` and `git diff --check` — passed |
| documentation links / fences | `python3 -c 'import pathlib,re,subprocess,sys; names=subprocess.check_output(["git","diff","--name-only","HEAD"],text=True).splitlines()+["docs/evidence/m1-implementation-verification-2026-09-22.md","docs/m1-product-persistence-design.md"]; names=sorted(set(n for n in names if n.endswith(".md"))); bad=[]; base=pathlib.Path(".").resolve(); ...; print("markdown links/fences passed" if not bad else "\\n".join(bad)); sys.exit(bool(bad))'` — changed-Markdown local links and fenced-code audit passed (the omitted loop body is reproduced verbatim below) |
| coverage | `find . -maxdepth 1 -type f -name '.coverage*' -delete && PYTHONPATH=src .venv/bin/coverage run -m unittest discover -v && .venv/bin/coverage combine && .venv/bin/coverage report \| tail -1` — 79.9%, above configured 70% threshold; 196 passed + 1 expected failure; combine consumed 23 parallel files (45 already absent) |
| package | `m1_dist_dir=$(mktemp -d /tmp/coding-agent-m1-dist.XXXXXX); m1_cache_dir=$(mktemp -d /tmp/coding-agent-m1-uv-cache.XXXXXX); UV_CACHE_DIR="$m1_cache_dir" /home/hmli/.local/bin/uv build --out-dir "$m1_dist_dir"; find "$m1_dist_dir" -maxdepth 1 -type f -printf '%f\\n' \| sort; m1_smoke_dir=$(mktemp -d /tmp/coding-agent-m1-smoke.XXXXXX); .venv/bin/python -m venv "$m1_smoke_dir/venv"; m1_wheel=$(find "$m1_dist_dir" -maxdepth 1 -name '*.whl' -print -quit); "$m1_smoke_dir/venv/bin/python" -m pip install --no-deps "$m1_wheel"; "$m1_smoke_dir/venv/bin/python" -c "import coding_agent, coding_agent.product_domain, coding_agent.product_persistence; print('isolated import smoke passed')"` — sdist/wheel and isolated import smoke passed |
| legacy scripted smokes | `m1_calculator_home=$(mktemp -d /tmp/coding-agent-m1-calculator.XXXXXX); PYTHONPATH=src .venv/bin/python -m coding_agent.cli --agent-home "$m1_calculator_home" run-scripted --source examples/fixture --task "Fix calculator behavior and run tests." --script examples/scripted_run.json`; `m1_todo_home=$(mktemp -d /tmp/coding-agent-m1-todo.XXXXXX); PYTHONPATH=src .venv/bin/python -m coding_agent.cli --agent-home "$m1_todo_home" run-scripted --source examples/todo_cli --task "Fix the empty input crash and run tests." --script examples/todo_cli_scripted_run.json` — calculator completed 4 model / 3 tool calls; todo completed 6 model / 5 tool calls; copied workspaces used |
| Memory regression | `PYTHONPATH=src .venv/bin/python examples/memory_cold_warm_benchmark.py` — completed offline; this did not enable default Memory or use a live Provider |

The first package-build attempt could not read the sandboxed uv cache; repeating with a temporary
cache required the declared build dependency download and completed successfully. No live Provider
was run. No M2 Turn Admission, direct-working-tree behavior, M3 request/context work, M4 capability
work, M5 CLI, M6 default Memory, M7 removal, IPC schema change, or uncertain-retry repair was made.

The documentation audit's complete inline command was:

```bash
python3 -c 'import pathlib,re,subprocess,sys; names=subprocess.check_output(["git","diff","--name-only","HEAD"],text=True).splitlines()+["docs/evidence/m1-implementation-verification-2026-09-22.md","docs/m1-product-persistence-design.md"]; names=sorted(set(n for n in names if n.endswith(".md"))); bad=[]; base=pathlib.Path(".").resolve();
for name in names:
 p=pathlib.Path(name); text=p.read_text(encoding="utf-8");
 if sum(1 for line in text.splitlines() if line.startswith("```"))%2: bad.append(f"unbalanced fence: {name}")
 for target in re.findall(r"(?<!!)(?:\[[^\]]*\])\(([^)#]+)(?:#[^)]*)?\)",text):
  if "://" in target or target.startswith("mailto:"): continue
  q=(p.parent/target).resolve() if not target.startswith("/") else (base/target.lstrip("/"));
  if not q.exists(): bad.append(f"missing link: {name} -> {target}")
print("markdown links/fences passed" if not bad else "\\n".join(bad)); sys.exit(bool(bad))'
```

## Remaining owner decision

The implementation met the M1 exit evidence above and was accepted on 2026-09-22. M2–M7 remain
inactive; this acceptance neither issues nor activates M2.
