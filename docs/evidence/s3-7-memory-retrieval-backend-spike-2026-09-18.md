# S3.7 候选检索后端 Spike

日期：2026-09-18  
baseline：`e89a99f`  
commit：本报告所在 S3.7 delivery commit（最终交接给出解析后的 Git hash）  
production_retriever_changed：`false`

## candidates_available

| candidate | 状态 | 说明 |
|---|---|---|
| `lexical-control` | available | 直接执行冻结的 `LexicalMemoryRetriever`，trace selection 必须与 preview 一致 |
| `bm25-content` | available | 只索引 eligible record content |
| `structured-bm25` | available | content 加策略验证后的 fact metadata；metadata 无 authority |
| `embedding-hybrid` | unavailable | 已定义显式 adapter；没有真实 backend，不生成 hash/随机/ground-truth 假分数 |

`src/coding_agent/memory/retrieval.py` SHA-256 为
`6e64026504311e3c467f6b34b747eea296bb68104576d8d01104d50dc073d0f8`，与 `f1d03cf` 中该文件
完全一致。候选模块没有进入 `AgentApplication`、headless、Runtime IPC 或默认 Context 路径。

## label_projection_test

evaluator 在 backend 调用前构造类型受限的 `CandidateRequest`。backend-visible payload 只含 query
text、session/repository/user scope、repository revision、top-k/Token budget、由 query text 和
repository component 名称确定性生成的 query metadata，以及经过 SQLite authority 过滤的 eligible
record content/trusted metadata。

自动测试向 case 的 `expected_relevant_memory_ids`、`category`、`oracle`、`expected_selection`、
`ground_truth_relevance`、`metadata_ground_truth` 和 `verification` 注入唯一 sentinel，再通过捕获
backend 证明 request 中既无字段名也无 sentinel。ground truth 只在 backend 返回后读取并计算指标。

structured record metadata 固定为 `fact_type/entities/concept_keys/repository_component/validity/revision`
schema；值只能是有界 atom，control/permission vocabulary 和额外字段均拒绝。`validity` 与 metadata
revision 只作审计，不参与 admission；即使伪造 metadata revision，record authority revision 仍会把
不匹配记录排除。

## candidate_parameters

- lexical-control：`minimum_relevance=0.60`、`relative_score_floor=0.72`、`common_weight=0.10`、
  `final_informative_boost=1.50`，均来自冻结实现。
- bm25-content：`k1=1.2`、`b=0.75`、`minimum_score=0.10`、`relative_score_floor=0.35`，只索引 content。
- structured-bm25：沿用 BM25 参数，`metadata_weight=0.50`；索引 content、fact_type、entities、
  concept_keys、repository_component；authority fields 为空。
- embedding-hybrid：预设 `embedding_weight=0.65`，但 adapter 为 `null`、
  `fallback_simulation=false`，所以不产生质量指标。

四臂复用同一 SQLite scope/status authority、expiry/revision gate、40 条 query、8 个共享 pool、
`top_k=5`、`token_budget=512`、scope/token/id tie-break 和指标实现。每案完整 funnel 包含 eligible IDs、
score components、ranked IDs、absolute/relative-floor/Token/top-k rejection 和 selected IDs。

## development_suite_results

这是 development-only 结果，不用于选择生产后端：

| candidate | recall | precision | irrelevant injection |
|---|---:|---:|---:|
| lexical-control | `0.3750` | `1.0000` | `0.0000` |
| bm25-content | `0.7083` | `0.3469` | `0.6531` |
| structured-bm25 | `0.7083` | `0.3269` | `0.6731` |
| embedding-hybrid | unavailable | unavailable | unavailable |

BM25 的 recall 增益伴随明显误注入，structured metadata 在当前生产可生成 query facets 下没有带来
额外 recall，且 precision 更低。因此本 spike 不批准任何生产切换或阈值调参。

## zero_overlap_results

10 个 `zero_overlap_paraphrase` case：lexical-control recall `0.0`；bm25-content 和
structured-bm25 均为 `0.3`。后两者的三例命中来自当前 BM25 tokenizer 可见的稀疏 content term，
不代表纯 lexical 已解决真正零词面交集；逐 case score/funnel 保留在 raw evidence。

## hard_negative_results

8 个同主题 hard negative：lexical-control 选择 `0/8`；bm25-content 在 `7/8` case 产生 15 条选择；
structured-bm25 在 `8/8` case 产生 15 条选择。这是 recall/precision trade-off 的阻断性结果，不能
通过只报告 recall 隐去。

## token_latency_cost

| candidate | selected retrieval Token total/mean | latency mean / P50 / P95 (ms) |
|---|---:|---:|
| lexical-control | `196 / 4.9` | `0.05123 / 0.04990 / 0.06733` |
| bm25-content | `1080 / 27.0` | `0.05981 / 0.05947 / 0.08077` |
| structured-bm25 | `1140 / 28.5` | `0.10940 / 0.10170 / 0.13976` |
| embedding-hybrid | unavailable | unavailable |

延迟是单机离线观测，只用于本次成本比较；不是生产 SLA。Token 是 selected Memory content 的现有
retrieval estimator 成本，不是 Provider model usage。

## repeatability

每个 candidate 连续执行三次，排除 wall latency 后的逐案 funnel/selection digest 三次相同；另以两个
独立 Python 进程复核同一 digest。tie case 按 score、scope priority、Token cost、memory ID 排序，
BM25 两臂三次都选择相同 ID。raw/summary 使用统一 schema；manifest 保存 suite、production
retrieval、raw 和 summary hash，自动测试反算 selected-ID digest 与文件 hash。

Evidence：

- [raw-result.json](./s3-7-memory-retrieval-backends-2026-09-18/raw-result.json)
- [redacted-summary.json](./s3-7-memory-retrieval-backends-2026-09-18/redacted-summary.json)
- [manifest.json](./s3-7-memory-retrieval-backends-2026-09-18/manifest.json)

## known_risks

- BM25 参数只在 development data 上记录，尚未校准；当前误注入不可接受。
- structured query metadata 只使用生产可获得的文本和 component 名称，能力有限；不能用
  `metadata_ground_truth` 补齐语义，否则会标签泄漏。
- record fact metadata 的创建/维护质量与成本尚未做真实 Runtime event A/B。
- embedding adapter 只有接口，没有真实 backend、网络、模型成本、隐私或 latency 数据。
- 本次只测 retrieval，不证明端到端任务成功率或真实 Provider Token 净收益。
- 不创建 Holdout v3，也不根据本结果选择、修改或启用生产后端。
