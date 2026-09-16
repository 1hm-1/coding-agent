# Coding Agent Development Instructions

Scope: this file applies to `/home/hmli/code/coding-agent` only.

1. Before editing, read `docs/HANDOFF.md`, `docs/current-state.md`, and the active milestone document.
2. Current implementation baseline is Phase 2 P2-M1 Headless Runtime IPC complete. P2-M2 Memory is
   the next candidate milestone but remains inactive until the user explicitly activates it.
3. Preserve the M1/M1.5 vertical slice and all four semantic golden tests.
4. Do not add a general Shell tool. `restricted_test` accepts trusted profile names only, and
   `run_command` accepts only trusted profiles with structured argv.
5. Never let Runtime bypass ToolHarness for side effects, and never write to the source repository.
6. Keep provider-specific formats inside model adapters. Keep state transitions inside the FSM.
7. Memory, Skill, MCP, multi-agent, UI, RAG, and framework dependencies remain deferred until
   their named milestone is explicitly activated.
8. Run `PYTHONPATH=src python3 -m unittest discover -v` after relevant changes.
9. Update `docs/current-state.md`, the active checklist, and roadmap status when behavior changes.
10. Do not modify the sibling `../hermes-agent` repository; it contains pre-existing user changes.
11. Implement only the active milestone acceptance criteria. Historical M2.1 must not be
   retroactively expanded with resume or real provider adapters; current capability changes need
   an eval-backed decision.
12. Treat SQLite as authority once M2.1 integrates it; JSONL remains a rebuildable export, never a second state authority.
13. Do not mark a checklist item complete until its success, failure, and rollback/recovery tests pass.
14. `docs/protocol/runtime-ipc-v1.md` and `protocol/v1/*.schema.json` are the producer authority for
    public Runtime IPC. Do not expose private SQLite/trajectory schemas as Platform IPC, duplicate
    the schemas in a consumer repository, or claim that `v0.1.0` implements the protocol.
