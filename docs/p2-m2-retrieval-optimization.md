# P2-M2 Retrieval Precision 与 Token 效率优化

> 状态：**已完成**
> 激活日期：2026-09-16
> 前置基线：提交 `80dba4c` 已冻结 12-case 扩展 Memory benchmark；134/134 默认测试通过。

## 1. 目标与边界

本工作只优化 P2-M2 已有的确定性 lexical/metadata retrieval 与 Memory Context 表达，不激活
P2-M3，不加入 embedding、vector backend、RAG framework、Skill、MCP、多 Agent、公共 IPC 字段
或第三方 Runtime 依赖。默认 `AgentApplication` 与 `run-headless` 继续保持 Memory-disabled。

## 2. 验收清单

- [x] 冻结 benchmark 同进程运行旧基线/候选 A/B，并归因误召回来源。
- [x] relevant recall >= 0.90，irrelevant injection <= 0.15。
- [x] scope/revision/stale/deleted leakage 均为 0。
- [x] warm task success 不低于 cold，原始 3-case warm 3/3。
- [x] 相对旧 warm 基线，Memory 额外模型 Token 至少下降 25%。
- [x] Context manifest 保留 retrieval id、memory id、schema/record version、score、provenance
  与实际 Context Token 成本；SQLite retrieval audit 继续完整。
- [x] 检索阈值、常见词降权、scope 优先级、动态截断和预算行为具备成功/失败测试。
- [x] 134 个既有测试与四份 semantic golden 不回退；优化后新增 2 个检索测试，当前为 136/136；
  Ruff、mypy、compileall、coverage、build、calculator/todo smoke 及 Memory benchmark 通过。
- [x] `current-state.md`、本清单、roadmap、testing/structure/HANDOFF 与 evidence 同步。

## 3. 决策原则

1. 先用冻结 benchmark 证明旧算法的误召回，再改变默认行为。
2. 评分只使用确定性 lexical/metadata 信号；相同输入与记录集合必须得到稳定排序。
3. 模型输入只携带完成任务所需的 Memory 内容与不可信边界；完整归因保留在审计 manifest，
   不把重复 provenance 当作模型推理材料。
4. Token 指标区分 retrieval 预算估算、Context counter 实测值和模型 usage，不混为一项。

## 4. 完成证据

- before→after：relevant recall 1.0→1.0，precision 2/3→1.0，irrelevant injection 1/3→0；
- scope/revision/stale/deleted leakage 全为 0，warm 12/12 不低于 cold 8/12，原始 3-case warm 3/3；
- retrieval Token 116→81，Memory Context/额外模型 Token 803→130，下降 83.8%；
- 同一轮实测 wall latency：before cold mean `45.834164333731074ms`、warm mean
  `47.033260334198225ms`（warm-cold `+1.1990960004671507ms`）；after cold mean
  `45.834164333731074ms`、warm mean `45.95990916732262ms`（warm-cold
  `+0.12574483359154698ms`）。检索 latency mean 为 `0.056614917411934584ms`→
  `0.06059541647118749ms`（`+0.003980499059252907ms`），该回退原样保留，且不作为优化
  验收阈值；完整 p50/p95 见脱敏 evidence；
- 本轮门禁：136/136 默认 unittest、四份 semantic golden、Ruff、33 文件 mypy、compileall、
  78.5% statement coverage、wheel/sdist、独立 wheel import、calculator/todo smoke 和冻结 A/B
  均通过；
- SQLite schema 保持 v4，无新增 Runtime 依赖，默认 Application/headless Memory 仍关闭。

## 5. Memory 入口决策

本轮只证明确定性 trusted oracle benchmark 中的检索精度与 Context Token 变化，没有运行真实
Provider，也没有证明真实模型或通用任务的净收益。因此 Memory 的当前交付形态仍明确为显式
Python composition：调用方自行组装 `SQLiteMemoryStore`、`MemoryService`、
`LexicalMemoryRetriever` 和 query factory 到 `BudgetedContextBuilder`。默认
`AgentApplication`、`run-headless` 和 Runtime IPC capability 不创建、查询或注入 Memory；
P2-M3 Profiles/Skill Runtime 仍未激活。后续只有扩大真实任务/Provider 的 A/B 并证明净收益后，
才重新评估默认入口或 IPC 集成。

## 6. S2 独立复核

S2 对 `80dba4c`（冻结 12-case benchmark）、`6e261f9`（检索/Context 优化）和 `053482c`
（证据收口）逐项复核。指标定义在 L2 未改动，before arm 复现 L1 的 803 Token/1/3 误注入；
默认 Application/headless/IPC 仍没有 Memory wiring，preview 不写 retrieval audit，scope/revision/
status 过滤测试通过，Token 降低没有伴随 scripted task success 下降。

复核发现一处明确缺陷：record 级 `actual_context_token_cost` 曾固定使用基类紧凑 renderer 计算，
导致 benchmark before arm 的旧 JSON renderer 下 record 归因与实际注入不一致；总 section Token
始终正确。现已改为使用实际 builder renderer，并加入自定义 renderer 回归测试。

仍存在证据限制：12-case 每例使用隔离 memory pool，检索别名、阈值和末尾信息词权重与冻结样本
同源，且没有真实 Provider/非同源 retrieval holdout。`original_three_case_warm_success` 当前统计冻结
扩展集合的前三个 case，并不重建最初共享 memory pool 的拓扑；S2 另用 `5298ba0` 原始脚本配合当前
实现复跑得到 warm 3/3，但 checked-in benchmark 应在后续证据工作中显式补上该兼容 arm。

因此结论为：**有条件通过**。Memory 必须保持显式 opt-in；L3 非同源 holdout 已补齐但未达到
recall/injection 门槛，仍需真实 Provider A/B，在这些证据完成前不得据此默认启用 Memory。
P2-M3 仍需用户另行明确激活。

## 7. L3 非同源冻结 Holdout

L3 在不修改 `memory/retrieval.py`、Memory scoring/alias/threshold、Context renderer 或 Token
归因逻辑的前提下，固定了 18 个新领域 case。case manifest SHA-256 为
`3d4bffb06a19ee219534d4649d93e983e7ecd14cebfd90b84ae64feb1f7e2ed1`，使用三个共享 pool（6/7/5），
并单独加入 `5298ba0` 三任务共享池兼容 arm。首轮结果必须按原样解释：warm task success `13/18`、
relevant recall `0.6666666666666666`、precision `0.8333333333333334`、irrelevant injection
`0.16666666666666666`；scope/revision/stale-deleted leakage `0`，无相关 Memory 行为变化 `0`，
manifest Token 与 renderer 一致，兼容 arm warm `3/3`。因此 L3 recall 与 injection 门槛均未通过，
不能用后续实现或挑题改写该首轮结论。

首轮后仅为补齐成本观测而重复运行一次：cold/warm wall latency mean 为
`41.255672772725426ms`/`42.08423031700982ms`，retrieval mean 为 `0.18361411154425392ms`；
这些延迟是独立重复观测，不冒充首轮结果。完整脱敏摘要见
[`memory-retrieval-holdout-2026-09-16.summary.json`](./evidence/memory-retrieval-holdout-2026-09-16.summary.json)。

该结果继续支持 Memory 仅作显式 Python composition；默认 Application/headless/IPC 不接入，
真实 Provider cold/warm A/B 与净收益证明仍未完成，P2-M3 不因本 holdout 自动激活。

## 8. S3 Holdout 审查与真实 Provider paired harness

S3 复核确认 L3 使用三个共享 pool、不是一例一池，并通过完整 Runtime final answer 统计 task
success；但多数 task/memory 是近同构关键词改写，且 deterministic backend 直接持有 expected
fact/answer。因此 L3 不是强非同源 Provider 证据。五个 recall miss 与两个 hard-negative injection
只做失败分类；未改检索算法。后续 development set 明确使用另一组 L1/L2 冻结 12-case 数据，
不使用 L3。未来若基于该 development set 调整算法，必须先冻结新的 Holdout v2，不能重看或改写
当前 L3 来调参。

新增 `evaluate-memory-live` paired harness：off/on 共用 Provider、model、task、RunPolicy、源码指纹
和 trusted oracle，pair 内交替执行顺序；Memory 必须由先前完成的 Runtime final result 产生，并以
committed `run_finished` event 经 journal provenance 校验。报告覆盖 end-to-end/oracle/Runtime、
Token、retrieval、latency、工具/失败、first relevant action 和安全不变量；写盘前阻止 Secret 与绝对
路径，且不保存 Provider reasoning。默认 Application/headless/IPC 未改变。

当前只有离线 harness contract test，没有消耗 Provider 凭据或生成 live 结果，因此 S3 结论为
**有条件通过**：Memory 保持显式 opt-in，继续补冻结真实任务集的 Provider A/B；P2-M3 仍未激活。
S3 门禁为 139/139 unittest、四份 semantic golden、Ruff、34 文件 mypy、compileall 与
`git diff --check`；未重跑完整 coverage，因此保留最近一次 78.5% 数字，不制造新覆盖率结论。
完整审查见 [`s3-holdout-and-live-ab-review-2026-09-17.md`](./evidence/s3-holdout-and-live-ab-review-2026-09-17.md)。

## 9. L3.5 盲测 Holdout v2（只冻结，不执行）

为避免 development set 污染，已先建立 L3.5 Holdout v2 的正文、共享 Memory pool 和 metadata
manifest，但本轮不调用检索器、Runtime、oracle 或 benchmark，也不生成结果。正文已转移到用户保管的
仓库外目录；共享仓库只保留对外可审计的 case ID、预期 oracle 类型和冻结状态，位于
[memory-retrieval-holdout-v2.manifest.json](./evidence/memory-retrieval-holdout-v2.manifest.json)。

manifest 固定 suite SHA-256 为
d1d9c45d9d06aea211780fa1d6b3d9baf4154891f1a3344ce1ae1cb658907d6f，主 Holdout 为 20 个 case、
4 个新任务领域/fixture repository 和 4 个共享 pool（每池 5 个 case）。覆盖 paraphrase、hard
negative、冲突记忆、无记忆、user scope、repository revision、stale/deleted 与 prompt-injection
负例；oracle 只检查测试、文件或工具行为，并为无记忆 case 保留 memory-on/off 行为等价比较。
原始 5298ba0 三任务共享池兼容 arm 单独保留，不计入 20 个主 case。

当前 manifest 标记 executed: false、results_generated: false。只有 S3.5 算法冻结后才允许
首次执行；在此之前不得根据正文或任何未生成的结果调整 case、检索算法或宣称模型收益。

本轮收口已通过静态 manifest 契约 3/3、Ruff、全量 unittest 142/142 和 git diff --check。上述检查
只验证仓库内的 manifest、冻结说明与现有实现，没有导入或执行用户保管的 Holdout 正文；manifest 仍
保持 executed: false、results_generated: false。
