# S3.8 Retrieval Route Closure

Date: 2026-09-18  
Reviewed baseline: `8b8915f`  
Milestone status: **P2-M2.3 completed with no qualifying backend**

## Review result

The committed S3.8 raw result, summary, Pareto result, and manifest are mutually hash-consistent.
Independent numerator/denominator reconstruction produced the following fixed-parameter results:

| Candidate | Relevant hits / labels | Selected relevant / all selected | Recall | Precision | Irrelevant injection | Retrieval Token |
|---|---:|---:|---:|---:|---:|---:|
| lexical-control | 9 / 24 | 9 / 9 | 0.375 | 1.0 | 0.0 | 196 |
| bm25-content | 17 / 24 | 17 / 49 | 0.7083333333333334 | 0.3469387755102041 | 0.6530612244897959 | 1080 |
| structured-bm25 | 17 / 24 | 17 / 52 | 0.7083333333333334 | 0.3269230769230769 | 0.6730769230769231 | 1140 |

The committed Pareto artifact is arithmetically consistent with its declared sweep. Its decisive bounds are
`max_recall_when_irrelevant_injection_lte_0.15=0.375` and
`min_irrelevant_injection_when_recall_gte_0.85=null`.

One non-decision-affecting limitation is retained explicitly: the offline threshold sweep treats every raw scored
item as threshold-addressable. For lexical-control this includes zero-score items that the production retriever's
hard `no_lexical_overlap` guard cannot select, and the sweep does not add an above-maximum threshold representing
an empty selection. Therefore the reported count of 20 global non-dominated points describes the artifact's
exploratory score sweep, not an exact set of production-reachable configurations. A conservative recomputation
that excludes lexical hard-guard rejections and includes an empty-selection point leaves the acceptance result
unchanged: the maximum recall at injection `<=0.15` remains `0.375`, and no candidate reaches recall `>=0.85`.
The frozen raw, summary, Pareto, and manifest files are not modified by this review.

## Formal decision

```text
lexical-control: rejected
bm25-content: rejected
structured-bm25: rejected
embedding-hybrid: not evaluated/unavailable
production candidate: none
Memory default enablement: rejected
```

The current lexical retriever remains available only for explicit experimental Python composition. Default
`AgentApplication`, headless, and Runtime IPC paths remain Memory-disabled. No Holdout v3 is created, L4 is not
executed, and deterministic benchmark success is not extrapolated to real-Provider task success or net benefit.

## Design-only follow-up routes

| Route | Scope | Evidence needed before implementation or promotion | Current decision |
|---|---|---|---|
| A — freeze Memory and proceed to P2-M3 Profiles/Skills | Keep the governed Memory lifecycle and experimental lexical path frozen; activate the already planned profile and Skill contracts | Explicit P2-M3 activation and its permission, provenance, budget, replay, failure, and recovery gates | Candidate next milestone; not activated by this closure |
| B — propose P2-M2.4 Semantic/Embedding Retrieval | Create a separate retrieval milestone; do not retrofit it into P2-M2.3 or the default Runtime path | New design, implementation authorization, development data, and a brand-new blind holdout | Proposal only; not implemented |

P2-M2.4 would have to evaluate all of the following before an implementation decision:

- embedding provider, model, and version identity;
- network access, Secret handling, privacy, retention, and report redaction;
- cache identity and full rebuild behavior on model/version changes;
- propagation of delete, stale, repository revision, and session/repository/user scope changes;
- vector-index authority, rebuildability, consistency with SQLite authority, and audit attribution;
- timeout, offline behavior, backend unavailability, and fail-closed semantics;
- per-retrieval provider/index cost and Token per successful task;
- a new development set and a new blind Holdout created and frozen before results are inspected.

This closure adds no embedding dependency, changes no retrieval algorithm or default wiring, enables no network,
runs no Provider, and creates no Holdout v3.
