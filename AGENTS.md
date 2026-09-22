# Coding Agent Development Instructions

Scope: this file applies to `/home/hmli/code/coding-agent` only.

1. Before editing, read `docs/HANDOFF.md`, `docs/current-state.md`, the current roadmap status,
   and any active formal execution contract under `docs/execution-contracts/` when present.
2. Current stable implementation baseline is Phase 2 P2-M2 Layered Memory complete. Product-Layer
   M0 Architecture Freeze + Characterization is Accepted and complete; the formal M1 execution
   contract is Accepted and **M1 is complete**. M1 additive Schema v5 identity/mapping work is
   implemented while the legacy Runtime compatibility boundary remains intact. The formal M2
   execution contract is **Accepted** and **M2 is complete** as of 2026-09-23. M3–M7 remain
   inactive; M3 has not been issued or activated.
   P2-R1 is historical/superseded, not the next candidate.
3. Preserve the M1/M1.5 vertical slice and all four semantic golden tests.
4. Do not add a general Shell tool. In the current legacy implementation, `restricted_test` accepts
   trusted profile names only and `run_command` accepts only trusted profiles with structured argv.
   Future gated interactive commands follow the V1 capability matrix: enforceable trusted
   envelopes and exact `ASK`, not a permanently profile-closed domain command model.
5. Never let Runtime bypass ToolHarness for side effects. Current legacy one-shot/headless paths
   must never write to the source repository. Target interactive direct-working-tree mutation is
   allowed only after both V1 M3 and M4 gates pass; M2 alone is read-only on real user trees.
6. Keep provider-specific formats inside model adapters. Keep state transitions inside the FSM.
7. Skill, MCP, multi-agent, UI, RAG, vector stores, and framework dependencies remain deferred until
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
15. Interpret P2-M2.3 correctly: it rejected the evaluated lexical/BM25 Dynamic Recall candidates,
    not Memory governance or History Search. The accepted M2-lite ADR supersedes P2-R1 default Core
    Snapshot and automatic-serving assumptions; do not change default Memory wiring without a newly
    activated milestone.
16. The V1 capability promise is frozen in `docs/v1-development-capability-matrix.md`. Do not treat
    trusted profiles as a complete command universe, add unrestricted arbitrary shell, or bypass
    the M3+M4 rollout gate through commands, caches, startup artifacts, indirect effects, or undo.
17. Do not confuse the Accepted, completed Product-Layer M0 with the completed historical Runtime
    M0. Product-Layer M0 was characterization only and did not itself implement target product
    behavior. M1 work was authorized only by its issued contract and is now complete. M2 work is
    authorized only by the then-active M2 contract; M3–M7 require future activation. Contract-publication
    turns authorize no code, schema, semantic, or metrics implementation. The historical
    narrowly scoped M0 testability exception is closed with M0 completion.
18. Store published formal milestone contracts at
    `docs/execution-contracts/mN-execution-contract.md`; issuance includes the repository copy and
    navigation links, with scope and activation explicit. The completed M1 contract is
    `docs/execution-contracts/m1-execution-contract.md`; the accepted M2 contract is
    `docs/execution-contracts/m2-execution-contract.md`. Do not invent or autoactivate M3–M7.
    Record material amendments explicitly instead of silently rewriting issued scope.
