# Resume benchmark and context-compression A/B

> Status: deterministic calibration and the live Provider run were completed on 2026-09-15.
> The planned `deepseek-v4-flash` identifier was no longer available, so the live run used the
> Provider-listed replacement `deepseek-flash`; exact provenance and limitations are recorded below.

## 1. Resume-facing stability benchmark

The benchmark uses five checked-in normal tasks and five fresh repetitions per task, for 25
independent runs. Every run gets a new SQLite authority and isolated workspace. Scripted negative
controls are excluded from both execution and the capability denominator.

| Case | Coverage |
|---|---|
| `calculator-success` | small edit and test |
| `todo-recovery` | parser edit and test |
| `pipeline-multi-file-recovery` | multi-file repair after a failed test observation |
| `settings-scoped-edit` | scoped edit and changed-path oracle |
| `search-lab-content-location` | content search under a tight tool budget |

Primary metric: `end_to_end_success_rate = oracle_success && runtime_completed`. Secondary metrics
come from `task_metrics`, which excludes negative controls: end-to-end latency, input/output/total
Token, Agent model calls, compression calls, and tool attempts. Distributions report count, total,
mean, nearest-rank P50, and nearest-rank P95. End-to-end latency includes workspace preparation,
the Agent run, and trusted oracles; `latency_ms` remains the event-timestamp Runtime interval.

Live command (after exporting the named key):

```bash
PYTHONPATH=src .venv/bin/python -m coding_agent.cli \
  --agent-home /tmp/coding-agent-resume-live \
  evaluate --suite examples/eval_suite.json --variant budgeted --repetitions 5 \
  --case-id calculator-success \
  --case-id todo-recovery \
  --case-id pipeline-multi-file-recovery \
  --case-id settings-scoped-edit \
  --case-id search-lab-content-location \
  --provider openai-compatible --model deepseek-flash \
  --base-url https://api.deepseek.com --api-key-env OPENAI_API_KEY \
  --thinking disabled --output /tmp/coding-agent-resume-live/report
```

Do not present the scripted calibration as a model capability score. A resume number becomes valid
only after the same command runs against a named Provider/model with 25 valid runs, zero evaluation
infrastructure failures, and a saved sanitized summary plus raw artifact hashes.

## 2. Context-compression A/B

The A/B isolates the compression engine while keeping the task, fixture, model, tool/context
budgets, oracle, and repetition key fixed:

- baseline `budgeted`: budgeted context builder, no summarizer;
- candidate `compressed`: the same builder plus the primary Provider as summarizer;
- one compression-sensitive task, ten repetitions per arm (20 runs total);
- arm order alternates within every pair to reduce time and Provider-load drift.

```bash
PYTHONPATH=src .venv/bin/python -m coding_agent.cli \
  --agent-home /tmp/coding-agent-context-live \
  evaluate --suite examples/eval_suite.json --repetitions 10 \
  --case-id long-history-compression --ab \
  --ab-variants budgeted compressed \
  --provider openai-compatible --model deepseek-flash \
  --base-url https://api.deepseek.com --api-key-env OPENAI_API_KEY \
  --thinking disabled --output /tmp/coding-agent-context-live/report
```

Read success deltas and paired Token/call/latency deltas from `paired_diff.json`; read each arm's
P50/P95 from `budgeted/report.json` and `compressed/report.json`. Count summarizer usage in
`compression_calls` and include its Token in `total_tokens`. A claim that compression saves Token
requires lower candidate `total_tokens` without a material end-to-end success regression. With ten
pairs, the result remains a small local benchmark rather than a production claim.

## 3. Deterministic calibration result

The offline calibration proved fresh-run selection, normal-task-only aggregation, percentile
reporting, compression accounting, and pairing. The 25-run stability calibration completed 25/25;
end-to-end latency was P50 548.9 ms / P95 743.4 ms, total Token was 24,815, Agent calls 145, and
tool attempts 120.

The scripted A/B completed 10/10 in both arms. Compression was triggered exactly once per candidate
run, but added 290 synthetic Token and one summarizer call per run while Agent calls and tool calls
were unchanged. Scripted usage is fixed fixture data, so this is a cost-accounting calibration, not
evidence that real compression helps or hurts. The sanitized machine-readable record is
[`evidence/resume-benchmark-calibration-2026-09-15.summary.json`](./evidence/resume-benchmark-calibration-2026-09-15.summary.json).

## 4. Live result

The adapter smoke passed 1/1 with `deepseek-flash`. The stability benchmark produced 25/25 valid
runs with zero infrastructure failures and a 24/25 (96%) end-to-end success rate. End-to-end latency
was P50 5.34 s / P95 9.04 s; total usage was 345,551 Token, 114 Agent model calls, and 156 tool
attempts. The only failed run was `search-lab-content-location` repetition 1, which exhausted the
fixed tool budget. Source invariants held in 25/25 runs, with no invalid tool calls, repeated failure
batches, or permission violations.

The paired A/B produced 10/10 valid runs in each arm with zero infrastructure failures. `budgeted`
completed end-to-end in 9/10; `compressed` completed in 10/10, while both arms passed the oracle in
10/10. The candidate added 339,864 total Token across ten pairs (mean +33,986.4), 37 total model
calls, 47 tool attempts, and a mean 6.49 s end-to-end latency per pair. It therefore does not support
a compression Token-savings claim. The compressed arm also recorded 18 compression-budget and 4
summary-schema rejection observations; their Provider costs remain included in the totals.

The planned `deepseek-v4-flash` model was absent from the authenticated Provider model list at run
time; only `deepseek-flash` and `deepseek-v4-pro` were listed, so both experiments used
`deepseek-flash`. The worktree was not clean, so Git HEAD alone is not sufficient provenance. The
sanitized record includes the Git HEAD, tracked content/diff hashes, manifests, raw artifact hashes,
and the model-selection limitation:
[`evidence/deepseek-resume-benchmark-live-2026-09-15.summary.json`](./evidence/deepseek-resume-benchmark-live-2026-09-15.summary.json).
