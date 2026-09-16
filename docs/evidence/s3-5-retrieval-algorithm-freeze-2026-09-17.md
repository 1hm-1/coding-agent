# S3.5 检索泛化修复与算法冻结

日期：2026-09-17  
基线：`6c47a7d`

## 决策

`retrieval_algorithm_changed: true`

唯一算法变化是对 lexical term 的首尾自然语言标点 `.:-` 做规范化，同时保留 identifier 内部标点，
例如 `v1.2` 与 `cache-key` 仍是完整 term。抽象失败类型是：scanner 为支持 identifier 而保留标点，
却把句末 `interval` 与 `interval:` 当成不同词，造成与内容无关的 false negative。

这不是 benchmark 特判：没有新增 alias、任务词、fixture literal、case ID、末尾词 boost 或阈值；规则
对所有 term 对称生效。公开 S3 hard-negative 分类需要区分“同主题但不含答案”的记录，纯 lexical
信号没有可靠通用修复，因此本轮不改变 score、权重、相对 floor、scope priority 或 top-k 策略。

## Development data

- L1/L2 已公开并明确作为 development set 的冻结 12-case benchmark；
- 自建通用 micro development case：短查询、带冒号/句点的相关 prose、topic-only negative，以及
  `v1.2`/`cache-key` identifier 保真；
- 当前单元测试与历史 benchmark。未使用任何受保护 Holdout 正文或结果；旧 L3 仅以既有冻结
  evidence 维持历史审计契约，不作为 candidate 指标。

## Before / after

| 指标 | `6c47a7d` before | candidate after |
| --- | ---: | ---: |
| L1/L2 relevant recall | 1.0 | 1.0 |
| L1/L2 irrelevant injection | 0.0 | 0.0 |
| L1/L2 retrieval Token | 81 | 81 |
| L1/L2 warm model Token | 2870 | 2870 |
| L1/L2 Memory Context Token | 130 | 130 |
| L1/L2 cold → warm task success | 8/12 → 12/12 | 8/12 → 12/12 |
| scope/revision/stale-deleted leakage | 0/0/0 | 0/0/0 |
| 原始三任务兼容 arm | 3/3 | 3/3 |
| manifest actual-token attribution | pass | pass |
| punctuation micro relevant recall | 0/1 | 1/1 |
| punctuation micro irrelevant injection | 0 | 0 |
| punctuation micro retrieval Token | 0 | 17 |

Micro case 不单独伪造模型 usage；模型 Token 的 before/after authority 是完整 L1/L2 Runtime benchmark，
其 warm total 保持 2870。Micro case 只证明 tokenization 的局部 false-negative 修复与 negative control。

## 冻结与边界

- 默认 `AgentApplication`、headless 与 Runtime IPC 的 Memory 路径保持关闭。
- 历史 L3 测试改为只校验已经冻结的 evidence/hash，不再用候选算法反复运行已知 Holdout；历史
  evidence、case manifest 与 hash 均未修改。
- 未读取、列举或访问 `/home/hmli/holdouts/`，未请求 Holdout 正文，未执行 Holdout v2。
- 本提交后 lexical normalization、alias、权重、threshold、relative floor、scope priority、top-k 与
  Token budget policy 视为冻结；后续改变需重新走 development A/B，并在查看结果前冻结新 Holdout。

## Known risks

- 边界标点规范化会略微扩大自然语言 exact-term match；当前 negative control 未发现误注入，但小型
  development set 不能证明所有领域的 precision。
- 纯 lexical 策略仍无法可靠区分共享主题词下的 answer-bearing fact 与文档性 hard negative。
- 现有少量 inflection alias 和末尾信息词权重属于此前冻结策略，本轮未扩大或重新验证其跨语言泛化。
- 没有真实 Provider 收益证据；Memory 继续显式 opt-in。

`holdout_accessed: false`
