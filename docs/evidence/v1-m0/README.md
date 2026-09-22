# V1 M0 evidence bundle

Run all commands from the repository root. No command uses a live provider.

## Artifacts

- `pre-change-provenance.json`: dirty-worktree boundary before M0 edits.
- `metric-dictionary-v1.json`: measurement definitions and limitations.
- `baseline-manifest-v1.json`: immutable inputs, environment, results, and raw hashes.
- `offline-baseline-v1.json`: sanitized run-level projection of both eval lanes.
- `raw/`: sanitized `report.json`, `runs.jsonl`, and `manifest.snapshot.json` for both lanes, plus original/stored hashes.
- `representative-tasks-v1.json`: six pinned future real-repository tasks.
- `m1-migration-inventory-v1.json`: introspected v4 schema and M1 preservation inventory.
- `uncertain-retry-v1.json`: normalized request payloads, digests, journal events, and positive control.
- `verification-v1.json`: final commands, counts, quality results, and protected/hash status.

## Test and quality reproduction

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -p 'test_v1_m0_*.py' -v
PYTHONPATH=src python3 -m unittest tests.test_hardening tests.test_m2_recovery tests.test_persistence tests.test_protocol -v
PYTHONPATH=src python3 -m unittest discover -v
.venv/bin/ruff check src tests examples/todo_cli examples/mini_repos examples/memory_cold_warm_benchmark.py examples/memory_retrieval_holdout.py examples/v1_m0_collect_evidence.py examples/v1_m0_verify_release_snapshot.py
.venv/bin/mypy
PYTHONPATH=src python3 -m compileall -q src tests examples/todo_cli examples/mini_repos examples/memory_cold_warm_benchmark.py examples/memory_retrieval_holdout.py examples/v1_m0_collect_evidence.py examples/v1_m0_verify_release_snapshot.py
```

## Coverage and sandbox reproduction

```bash
m0_run_root=$(mktemp -d /tmp/coding-agent-v1-m0.XXXXXX)
export COVERAGE_FILE="$m0_run_root/.coverage"
PYTHONPATH=src .venv/bin/coverage run -m unittest discover -v
.venv/bin/coverage combine
.venv/bin/coverage report
PYTHONPATH=src python3 -c 'from coding_agent.sandbox import LinuxNamespaceExecutor; import json; print(json.dumps(LinuxNamespaceExecutor().capabilities().to_dict(), sort_keys=True))'
```

## Offline baseline reproduction

First verify every eval case has a scripted backend. Then run:

```bash
m0_run_root=$(mktemp -d /tmp/coding-agent-v1-m0.XXXXXX)
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

PYTHONPATH=src python3 examples/v1_m0_collect_evidence.py \
  --full-report "$m0_run_root/full-suite/report/report.json" \
  --stability-report "$m0_run_root/stability/report/report.json" \
  --output "$m0_run_root/offline-baseline-v1.json"
```

Compare semantic results and source hashes with `baseline-manifest-v1.json`. Timing can vary. A
new run's projection contains its own Session IDs and timing and will therefore have a different
whole-file hash. The collector reads the sibling `runs.jsonl`, retains run identity, metrics,
failures, and oracles, and verifies independently recomputed distributions against `report.json`.

The legacy evaluator's `end_to_end_latency_ms` includes fixture resolution/fingerprinting,
application/workspace setup, Runtime work, source checking, and oracle execution. The evidence
projection calls it `legacy_eval_wall_time`; it is not the metric dictionary's durable-acceptance to
terminal `end_to_end_elapsed_ms`.

## Scripted smokes and Memory regression

```bash
calculator_home=$(mktemp -d /tmp/coding-agent-v1-m0-calculator.XXXXXX)
PYTHONPATH=src .venv/bin/python -m coding_agent.cli \
  --agent-home "$calculator_home" run-scripted \
  --source examples/fixture --task "Fix add and run tests" \
  --script examples/scripted_run.json

todo_home=$(mktemp -d /tmp/coding-agent-v1-m0-todo.XXXXXX)
PYTHONPATH=src .venv/bin/python -m coding_agent.cli \
  --agent-home "$todo_home" run-scripted \
  --source examples/todo_cli \
  --task "Fix the empty input crash and run tests." \
  --script examples/todo_cli_scripted_run.json

PYTHONPATH=src .venv/bin/python examples/memory_cold_warm_benchmark.py
git diff --exit-code -- examples/fixture examples/todo_cli
```

Replay each printed Session ID with its corresponding agent home. Expected calculator evidence is
48 events and read/edit/test. Expected todo evidence is 72 events and test outcomes false then
true. Both report `source_unchanged=true`.

## Representative snapshot reproduction

For each entry in `representative-tasks-v1.json`, execute its `setup_command` and then its
`oracle_command` sequentially in the same POSIX shell. Each setup creates a fresh snapshot with
`git archive` at the pinned reference commit and changes into that snapshot. The package task uses
`examples/v1_m0_verify_release_snapshot.py`; its hash is frozen in the task manifest. The recorded
acceptance rule rejects command failure, failed assertions, skips, missing artifacts, or missing
dependencies. None of the six offline oracles requires a Provider.

## Limitations

The eval data is deterministic scripted evidence. Nonzero usage is synthetic fixture data, legacy
zero usage is unknown, and no provider quality, remote exactly-once execution, billing, general
coding success, or interactive responsiveness claim follows from it.
