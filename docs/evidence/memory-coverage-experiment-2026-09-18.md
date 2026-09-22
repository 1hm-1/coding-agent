# Memory coverage/cascade 离线优化实验

用户在 S3.8 收口后明确要求尝试优化 Memory。本次只增加 opt-in development candidate，
不激活 P2-M3、embedding、Provider 或新 Holdout；`retrieval.py` 与历史 S3.8 artifact 不修改。

## 方法与结果

content-only coverage 使用既有 tokenizer，不添加 alias：至少两个信息词匹配，query 信息词
覆盖率至少 0.5；query 若同时匹配其他记录独有的信息词则拒绝该候选。分数为 query coverage ×
record precision × confidence，相对地板 0.72，复用既有排序、scope/lifecycle gate 和 Token 上限。
不读取 query ground truth，不使用结构化标签作为答案，也不引入跨请求缓存。

第一轮单臂 coverage recall=9/24、injection=0，未超过 lexical。观察到两者选择集合互补后，
追加 cascade：若 lexical 有任何通过 relevance gate 的排序记录，就完整保留其决定；只有没有
排序记录时才使用 coverage。预算不足不触发 fallback。该组合是事后开发实验，不是预注册盲测。

在既有 40-case、24 个 relevant target 的 development suite 上各重复三次，最终轮为：

| 候选 | Recall | 误注入 | 检索 Token | mean latency (ms) |
|---|---:|---:|---:|---:|
| frozen lexical | 9/24 (37.5%) | 0/9 | 196 | 0.05118 |
| content BM25 | 17/24 (70.83%) | 32/49 | 1080 | 0.05990 |
| structured BM25 | 17/24 (70.83%) | 35/52 | 1140 | 0.10701 |
| coverage | 9/24 (37.5%) | 0/9 | 198 | 0.04666 |
| lexical → coverage | 10/24 (41.67%) | 0/10 | 220 | 0.13699 |

三次 deterministic digest 一致；新增两臂在 8 个 hard-negative case 上均不选择任何记录。
cascade 比 lexical 多命中一个 target，成本增加 24 个 retrieval Token；latency 也增加。
这些是检索 Token 估算与本机测量，不是模型 usage 或 SLA。

## 证据边界

- 召回仍低于 0.85，zero-overlap 类别召回仍为 0；不能宣称解决语义检索或真实 coding task failure。
- 共享信息词并不代表逻辑蕴含；竞争词拒绝也可能压制正常的多事实问题。该机制不提供新的
  prompt-injection 安全保证，仍依赖现有 policy、SQLite eligibility 和不可信 Context 边界。
- cascade 保留 primary 决策，也会保留其潜在误召回；本开发集的 injection=0 不是通用保证。
- 没有新 Holdout、Provider、默认 Memory wiring、公共 IPC、schema migration 或依赖。
- production candidate 仍为 none；本次小幅 development 改善不覆盖 S3.8 历史拒绝结论。

[机器可读证据](./memory-coverage-experiment-2026-09-18.json) 保存两轮完整 summary、参数、重复
digest、source manifest，以及 lexical/coverage/cascade 的逐 case selected/expected IDs 和 Token。
JSON 是紧凑选择投影，不是完整 raw score 文件；source manifest 的 raw hash 对应本机生成的完整
bundle，位于 `/tmp/memory-contrastive-first` 和 `/tmp/memory-contrastive-cascade`，临时目录并非
长期归档。suite SHA-256 为 `30a2589c6574a179f2fcf29cf18d2f5253ec8ca22c5c0b128bd1eb1ebf4dd488`。
下列命令可以重新生成完整 raw/summary/manifest（输出目录须为空）：

```bash
PYTHONPATH=src python3 examples/memory_retrieval_backend_spike.py \
  --suite examples/memory_retrieval_backend_development.json \
  --output /tmp/memory-coverage-reproduction --include-contrastive
```

不传 `--include-contrastive` 仍运行原四臂，作为关闭实验的回退路径。测试验证原四臂 digest
不变、两臂确定性、成功/拒绝、预算、输入顺序、隔离以及移除/恢复记录后无缓存残留。
全量 169/169 unittest（含四份 semantic golden）、Ruff、36 文件 mypy、compileall 通过。
既有 12-case cold/warm benchmark 也完成回归：warm 12/12、recall=1、injection=0，
retrieval/Context/warm model Token 仍为 81/130/2870；这是冻结生产 lexical 的回归，
不是新增 cascade 的 Runtime 收益证据。
SQLite 保持 v4；本次没有重新运行 coverage、build 或托管 CI，不沿用旧数字声称新门禁通过。
