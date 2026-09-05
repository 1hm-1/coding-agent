# Coding Agent Development Instructions

Scope: this file applies to `/home/hmli/code/coding-agent` only.

1. Before editing, read `docs/HANDOFF.md`, `docs/current-state.md`, and the active milestone document.
2. Current implementation baseline is M4.2 complete. Release/Evidence Hardening covers document
   drift, recovery metrics, Git/CI/coverage/type-check evidence, opt-in provider smoke, and
   multi-repository evaluation; M5 capability expansion remains conditional on failure coverage.
3. Preserve the M1/M1.5 vertical slice and all four semantic golden tests.
4. Do not add a general Shell tool. `restricted_test` accepts trusted profile names only, and
   `run_command` accepts only trusted profiles with structured argv.
5. Never let Runtime bypass ToolHarness for side effects, and never write to the source repository.
6. Keep provider-specific formats inside model adapters. Keep state transitions inside the FSM.
7. Do not add multi-agent, UI, RAG, Skill, or framework dependencies unless the roadmap is explicitly changed by the user.
8. Run `PYTHONPATH=src python3 -m unittest discover -v` after relevant changes.
9. Update `docs/current-state.md`, the active checklist, and roadmap status when behavior changes.
10. Do not modify the sibling `../hermes-agent` repository; it contains pre-existing user changes.
11. Implement only the active milestone acceptance criteria. Historical M2.1 must not be
   retroactively expanded with resume or real provider adapters; current capability changes need
   an eval-backed decision.
12. Treat SQLite as authority once M2.1 integrates it; JSONL remains a rebuildable export, never a second state authority.
13. Do not mark a checklist item complete until its success, failure, and rollback/recovery tests pass.
