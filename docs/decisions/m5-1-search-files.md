# M5.1 Decision: minimal read-only `search_files`

Date: 2026-09-06

## Evidence and decision

The five-file pipeline case is not sufficient evidence for search: after clarifying the
workspace-relative path contract, its 10-run DeepSeek baseline completed 10/10 runtimes,
recorded zero invalid paths and zero repeated failure batches. One raw oracle failure was an
equivalent `lower().strip()` implementation and exposed an overly exact oracle.

A separate nested `search_lab` fixture uses 12 opaque rule filenames and does not disclose the
responsible implementation path. Its no-search DeepSeek baseline produced 0/10 end-to-end
success: every run exhausted the fixed eight-call tool budget while attempting content
location. This repeatable failure coverage approves a minimal `search_files` experiment.

M5.1 was accepted after the same-task candidate materially improved end-to-end success from
0/10 to 6/10 and reduced direct `read_file` calls from 75 to 49 without invalid calls or source
invariant violations. The candidate still exhausted its tool budget in 4/10 runs and increased
aggregate input tokens from 73,073 to 147,027, so the decision approves only this narrow tool and
does not claim the locating problem is solved.

## Permission and schema increment

- Reuse `Permission.READ`; add no new permission.
- Accept a non-empty literal `query`, an optional workspace-relative `path`, and bounded
  `max_results`.
- Search only regular UTF-8 files in the isolated workspace.
- Reject absolute paths and parent traversal through `WorkspaceGuard`.
- Never search `.git`, follow symlinks, interpret regular expressions, invoke a shell, access
  Git metadata, or use the network.
- Bound files scanned, bytes scanned, matches returned, output size, and wall time.

## Acceptance tests

- Success: a literal query locates a nested file and returns workspace-relative path, line, and
  bounded text.
- Failure: absolute/parent paths, unknown schema fields, and invalid result limits fail closed.
- Recovery: the definition is `READ_ONLY`; a committed result is reused after a crash without a
  second logical attempt, and searches do not change the workspace revision.
- Regression: all four semantic golden tests and the existing tool/sandbox boundaries pass.
- Eval: run the same `search_lab` task and policy with and without the tool, then compare
  end-to-end success, invalid calls, direct reads, total tool calls, and tokens.

## Explicit non-goals

No regex engine, filename glob, replacement, shell command, Git inspection, dependency
installation, default network, or multi-agent behavior is included.

## Outcome

The success, failure, recovery, bounded-discovery, regression, and 10+10 live A/B checks passed.
Sanitized aggregate evidence and raw artifact hashes are stored in
`docs/evidence/deepseek-m5-1-search-2026-09-06.summary.json`.

A follow-up expanded the benchmark to three repositories and ran a 15+15 live prompt A/B without
raising the eight-call budget. Compact search-first guidance improved end-to-end success from
12/15 to 13/15, moved the median first search from call four to call one, and reduced input tokens
by 11.5%. The guidance is retained; two remaining budget failures are explicitly not evidence for
a larger budget. The follow-up summary is
`docs/evidence/deepseek-m5-1-search-expanded-2026-09-06.summary.json`.

A second focused follow-up fixed `task_runtime.remaining_budgets`, which had incorrectly repeated
the initial maxima on every model request, and added compact guidance to stop unrelated reads and
reserve one call for restricted tests. On the same 15-run benchmark, end-to-end and Runtime
completion moved from 13/15 to 14/15 while oracle success stayed 14/15 and budget failures fell
from two to one. Input tokens rose by 9.0%, so this is retained as a truthful runtime contract and
behavioral guardrail, not claimed as a general efficiency win. The sanitized summary is
`docs/evidence/deepseek-m5-1-budget-aware-2026-09-06.summary.json`.
