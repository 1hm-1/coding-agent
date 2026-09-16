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
- [x] 134 个既有测试与四份 semantic golden 不回退；Ruff、mypy、compileall、coverage、build、
  calculator/todo smoke 及 Memory benchmark 通过。
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
- 136/136 默认 unittest、四份 semantic golden、Ruff、33 文件 mypy、compileall、78.5%
  statement coverage、wheel/sdist、独立 wheel import、calculator/todo smoke 和冻结 A/B 通过；
- SQLite schema 保持 v4，无新增 Runtime 依赖，默认 Application/headless Memory 仍关闭。
