# S3.8 检索后端独立复现与 Pareto 审计

日期：2026-09-18<br>
固定基线：`cdea7b9`<br>
suite SHA-256：`30a2589c6574a179f2fcf29cf18d2f5253ec8ca22c5c0b128bd1eb1ebf4dd488`<br>
`retrieval.py` SHA-256：`6e64026504311e3c467f6b34b747eea296bb68104576d8d01104d50dc073d0f8`

本轮只做独立验证，没有修改候选、`memory/retrieval.py`、Runtime FSM、Provider 或 Holdout
v3。候选矩阵从已提交 raw result 的参数重建；独立审计不调用原有 `evaluate_suite` 汇总器，逐案
调用 backend 并自行计算 digest、选择一致性、Token/latency 和指标。所有标签只在 backend 返回后
用于离线指标和 Pareto 分析。

## 复现结果

四个候选各执行 3 次。以下 digest 是排除 latency 的 deterministic result digest；同一候选的三次
digest 均相同，40/40 case 的 selected IDs 均与已提交 raw result 一致。

| candidate | availability | relevant recall | precision | irrelevant injection | retrieval Token total/mean | independent latency mean / P50 / P95 (ms) |
|---|---|---:|---:|---:|---:|---:|
| `lexical-control` | available | `0.375` | `1.000` | `0` | `196 / 4.9` | `0.05199954975978471 / 0.049874986871145666 / 0.07570900197606534` |
| `bm25-content` | available | `0.7083333333333334` | `0.3469387755102041` | `0.6530612244897959` | `1080 / 27.0` | `0.0685225004417589 / 0.05949199839960784 / 0.16527800471521914` |
| `structured-bm25` | available | `0.7083333333333334` | `0.3269230769230769` | `0.6730769230769231` | `1140 / 28.5` | `0.11054457572754472 / 0.10342900350224227 / 0.1516070042271167` |
| `embedding-hybrid` | unavailable | — | — | — | — | — |

Candidate deterministic digests：

- `lexical-control`: `54c2ea7bd130c9e748a79ed0430d20dd2925b86cd2eb9a18fab4a22764cef3cd`
- `bm25-content`: `83c7d73e8a9d3bb019b56639a63235d528312723d9a13d3940389f5c85614efd`
- `structured-bm25`: `7ae81384e495d26910a2eb53fe33cf892684fbe5caa233323c2f2bda6078141b`
- `embedding-hybrid`: `533b457f3aaf70845918cf80a8e2abddecd1337f7eef3c03c5a0a4402db764c0`

独立验证还确认：candidate parameters 与提交记录一致；逐案 selected-ID digest 与 source summary
一致；source manifest、raw、summary 的 suite/retrieval/file hash、candidate 集合、repeatability、
Token/latency 算术和 selection digest 一致；`retrieval.py` 与 `f1d03cf` 完全一致。延迟为本次独立
运行的单机观测，不能当作 SLA；Token 是现有 Memory retrieval estimator 成本，不是 Provider usage。

## 离线 Pareto

Pareto 只读取 committed raw result 的 `score_components[].score`。每个 available candidate 枚举全部
唯一 score threshold，并扫描 `top_k=1..5`；固定相对分数地板、case Token budget 和 tie-break。没有
重新调用 backend，也没有把 relevance label 传入 backend。

| candidate | unique thresholds | operating points | non-dominated points | `max_recall_when_irrelevant_injection_lte_0.15` |
|---|---:|---:|---:|---:|
| `lexical-control` | 61 | 305 | 18 | `0.375` |
| `bm25-content` | 55 | 275 | 18 | `0.041666666666666664` |
| `structured-bm25` | 65 | 325 | 19 | `0.125` |

embedding-hybrid 没有 raw score，故没有 operating point。全候选 non-dominated union 为 20 个点；
全局边界为：

```text
max_recall_when_irrelevant_injection_lte_0.15 = 0.375
min_irrelevant_injection_when_recall_gte_0.85 = null
```

因此在这份 development suite 的已产生 score 上，没有达到 recall `>= 0.85` 的 operating point；同时
injection `<= 0.15` 时最高 recall 只有 `0.375`。完整逐点结果、threshold、top-k、selection digest
与候选/全局 non-dominated 集合见 [`pareto.json`](./s3-8-memory-retrieval-backend-audit-2026-09-18/pareto.json)。

## 边界与证据

本审计不证明真实模型任务成功率、Provider Token 净收益或默认入口收益；embedding adapter 未配置，
也没有运行 Provider。默认 Application/headless/IPC Memory 仍关闭，未创建 Holdout v3。

- [raw-result.json](./s3-8-memory-retrieval-backend-audit-2026-09-18/raw-result.json)
- [redacted-summary.json](./s3-8-memory-retrieval-backend-audit-2026-09-18/redacted-summary.json)
- [pareto.json](./s3-8-memory-retrieval-backend-audit-2026-09-18/pareto.json)
- [manifest.json](./s3-8-memory-retrieval-backend-audit-2026-09-18/manifest.json)
