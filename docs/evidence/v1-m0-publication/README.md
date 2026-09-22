# M0 publication verification evidence

This directory is the Phase B publication-verification bundle for tested commit
`c08621d45d72ac8b470b5810b68060839eb433dd`. It was collected in a clean full-history
clone, not the implementation worktree. The clone's tree was
`cc7f6db852636e826c8787b8bac391763557630c` before these publication artifacts were added.

The original environment supplied `/home/hmli/code/coding-agent/.venv`; no dependency was
installed or changed. `PYTHONPATH=src` was used, and the recorded import check resolves
`coding_agent` to this clone's `src/coding_agent/__init__.py`.

`raw/logs/full-unittest.log` is the already-completed clean-clone full-suite record: 183 tests,
182 pass, one expected M3-owned failure, no skip, exit 0. Its recorded execution predates this
Phase B continuation and is not represented as a fresh rerun.

Fresh Phase B checks are recorded in `verification-v1.json` and `raw/logs/`. The fresh scripted
evaluation lanes preserve their own 39 unique Session IDs and complete raw report/runs/manifest
files. They do not reuse, overwrite, or replace the immutable M0 evidence under `../v1-m0/`.

Compare old and new evaluation rows by `(case_id, repetition)` semantics only. Session IDs and
timing necessarily differ. Scripted tokens are synthetic fixture values; zero/missing legacy usage
remains unknown. `end_to_end_latency_ms` is legacy evaluator wall time, not durable acceptance to
terminal end-to-end time.

No live provider, paid request, user database, or new retrieval/holdout research was used.

Collection date: 2026-09-22. The publication executor transitioned from Sol High to Terra High
after M0 acceptance; Terra High performed the recorded Phase B checks and evidence assembly.

The actual commands used the existing venv and clone import path:

```bash
PYTHONPATH=src /home/hmli/code/coding-agent/.venv/bin/python -m unittest discover -s tests -p 'test_v1_m0_*.py' -v
PYTHONPATH=src /home/hmli/code/coding-agent/.venv/bin/python -m unittest tests.test_hardening tests.test_m2_recovery tests.test_persistence tests.test_protocol -v
PYTHONPATH=src /home/hmli/code/coding-agent/.venv/bin/coverage run -m unittest discover -v
PYTHONPATH=src /home/hmli/code/coding-agent/.venv/bin/python -m coding_agent.cli --agent-home <fresh-root> evaluate --suite examples/eval_suite.json --variant budgeted --repetitions 1 --output <fresh-root>/full-suite/report
PYTHONPATH=src /home/hmli/code/coding-agent/.venv/bin/python -m coding_agent.cli --agent-home <fresh-root> evaluate --suite examples/eval_suite.json --variant budgeted --repetitions 5 --case-id calculator-success --case-id todo-recovery --case-id pipeline-multi-file-recovery --case-id settings-scoped-edit --case-id search-lab-content-location --output <fresh-root>/stability/report
```

Ruff, mypy, compileall, native capability, calculator/todo scripted smoke, and Memory cold/warm
commands are indexed with their exit results in `verification-v1.json`; exact output is under
`raw/logs/`. Reproduce the complete command sequence through `../v1-m0/README.md`, retaining a
fresh agent home and preserving the baseline/raw comparison procedure.

The inherited full-suite log was recorded at 14:34 before this Phase B continuation. Coverage is a
later fresh full-suite rerun. Logs retain temporary paths where the evaluator emitted them; they are
not represented as globally path-redacted artifacts.

The publication verification intentionally leaves all existing M0 evidence, eight Accepted ADRs,
four semantic goldens, and protected IPC schemas/vectors untouched.
