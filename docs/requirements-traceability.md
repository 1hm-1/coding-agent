# 需求与面试能力追踪

> 用途：确保原始工程目标没有在分阶段开发中丢失，并防止面试表达超过代码证据。  
> 状态词：已实现 / 已设计未实现 / 明确不采用 / 条件扩展。

## 1. 核心工程需求映射

| 原始需求 | 状态 | 模块/计划 | 当前或未来验收证据 |
|---|---|---|---|
| 显式 Agent execution loop | 已实现 | `runtime.py` | FSM 单测、vertical E2E、golden transition path |
| 状态机而非裸循环 | 已实现 | `RuntimeState`、`ALLOWED_TRANSITIONS`、handler map | 合法/非法迁移测试 |
| tool call、observation、retry、interrupt、resume | 已实现 | Runtime + M2 journal | M1 golden；M2 crash/retry matrix |
| 统一 Tool contract | 已实现 | `tools/base.py`、`tools/harness.py` | schema/permission/timeout/error/output tests |
| read/search/edit/shell/test/git | M1 收敛为 read/edit/test；M4.2 增加结构化 `run_command`；其余条件扩展 | M5 条件阶段：search/git/更广执行需由 eval 决定；不提供 shell 字符串 | `test_m4_execution.py`、固定 eval 的 structured-command case；其余能力由后续 eval 证明，不按数量验收 |
| 每任务隔离 workspace | M4.1 Linux OS 强隔离已实现；其他平台 fail closed | `workspace.py`、`sandbox/` | source fingerprint、path escape、namespace attack/resource/parallel tests |
| messages/tool/state/metadata 持久化 | M2 已完成，SQLite authority 包含调用 journal | SQLite journal | transaction、round-trip、rollback、export、recovery tests |
| 可中断恢复 session | 已实现 | M2.2 Application/Runtime + lease | state-boundary/crash-window/reconciliation/resolution tests |
| recent/task/repo/compressed context | 已实现：预算、硬保留、摘要 lineage 和 stale invalidation | `context.py`、`compression.py`、`workspace.py` | retention/token/compression tests；paired A/B seam |
| Eval Harness | 已实现；golden 仍是 Runtime 回归子集 | `evaluation.py`、`cli.py` | versioned suite、trusted oracle、batch report |
| success/tool/token/latency/failure/recovery 指标 | M3 已完成离线聚合 | `evaluation.py` + committed events | report schema、失败分母和 recovery tests |
| replayable structured trajectory | 已实现，M2.1 已将 SQLite 设为 authority | `trajectory.py` / SQLite export | sequence/replay/golden/export equivalence |
| Release/Evidence hardening | 已完成 | Git/CI、coverage、release-surface mypy、recovery metrics、multi-repository eval、opt-in provider smoke | `.github/workflows/`、`pyproject.toml`、`evaluation.py`、`examples/eval_suite.json`、默认测试与手动 smoke；不宣称 hosted CI history 或真实 Provider 已运行 |

## 2. 面试高频主题映射

本表来自项目外的 `docs/面试问题.md`。它是能力覆盖清单，不是要求为了“题目齐全”加入无用功能。

| 主题 | 项目中的回答位置 | 证据成熟度 | 表达边界 |
|---|---|---|---|
| Claude Code/ReAct 完整链路 | [`architecture.md`](./architecture.md) §7、[`contracts.md`](./contracts.md) | 已实现 | 是显式 FSM 的 tool-observation loop，不宣称复刻 Claude Code 内部实现 |
| Agent Harness | [`architecture.md`](./architecture.md) §9、[`contracts.md`](./contracts.md) §4 | 已实现 | 讲 schema、权限、deadline、归一错误和 observation |
| 工具慢、阻塞与 timeout | [`current-state.md`](./current-state.md)、[`module-design.md`](./module-design.md) | 部分实现 | 当前同步调用且有 deadline；不要声称已有 callback/async cancellation |
| 任务中断恢复 | [`m2-implementation-plan.md`](./m2-implementation-plan.md) §4 | 已实现 | 只宣称状态边界中断和定义 crash window 的可解释恢复 |
| 两类模型后端统一 | [`m2-implementation-plan.md`](./m2-implementation-plan.md) §5 | 已实现 | 两个 adapter 通过离线 contract，另有一次 DeepSeek opt-in smoke；未宣称多次 live baseline 或生产成功率 |
| retry/fallback | [`m2-implementation-plan.md`](./m2-implementation-plan.md) §5 | 已实现 | 只对分类基础设施错误；质量差不自动 fallback |
| 重复工具调用/幂等 | [`contracts.md`](./contracts.md)、M2.2 recovery rules | 部分实现 | 已确认结果不重复；未知写操作需 resolution，不能宣称 exactly-once |
| 上下文压缩与信息丢失 | [`architecture.md`](./architecture.md) §13、[`roadmap.md`](./roadmap.md) §7 | M3 已实现 | 不编造 Token 降幅；用 lineage、required-fact retention 和 task success A/B |
| 短期/长期记忆 | Context M3；跨会话用户偏好不在当前范围 | 目标边界明确 | Coding task state 不等于用户长期偏好；不要混称 |
| Eval 体系和 Badcase 定位 | [`testing-strategy.md`](./testing-strategy.md)、[`roadmap.md`](./roadmap.md) §7 | M3 离线 eval 已实现 | 固定 suite 可定位 runtime/harness 轨迹；不外推生产成功率 |
| A/B 与上线迭代 | [`roadmap.md`](./roadmap.md) M3 | 离线 paired A/B 已实现 | 只做固定 suite 的描述性比较；真实流量实验不在当前项目证据内 |
| 安全、权限、Prompt Injection | [`architecture.md`](./architecture.md) §10、M4.1/M4.2 | 应用层 + Linux namespace 部分实现 | capability fail-closed、structured argv allowlist、secret/network/escape/resource/approval tests；不宣称抵御内核漏洞或跨平台等价 |
| 单 Agent vs 多 Agent | [`architecture.md`](./architecture.md) §3/§16 | 明确采用单 Agent | 原因是可归因、低成本；只有 eval 证明并行瓶颈才考虑多 Agent |
| Skill/RAG/Memory 关系 | 非当前实现 | 明确不采用/条件扩展 | 不为面试题强行加入 Skill、RAG 或长期记忆 |
| 指标是否只看成功率 | [`architecture.md`](./architecture.md) §14 | 已设计，部分指标可 replay | 区分 runtime completion 与 task success，保留失败 run |
| Demo 与 production 区别 | [`current-state.md`](./current-state.md)、[`roadmap.md`](./roadmap.md) | 持续演进 | 用恢复、安全、评测、观测门禁说明，不使用“生产可用”标签 |

## 3. 当前可展示证据

可以现场运行和解释：

- `ScriptedBackend` 驱动的确定性完整任务；
- 显式 FSM 与非法迁移保护；
- 四工具统一 Harness（包含结构化 `run_command`）；
- source unchanged / isolated workspace modified；
- permission denied、handler error、timeout 和 test recovery；
- JSONL replay 与四份 semantic golden；
- SQLite schema v3、session/message/checkpoint/call journal/summary round-trip、atomic mutation 和删除 JSONL 后重建 projection；
- 状态边界 resume、工具 crash/reconciliation、lease takeover、retry/fallback 和两个 adapter 的离线 contract；
- context section budget、token counter fallback、summary lineage/stale/rejection 和 eval oracle/report/A-B；
- M4.1 `restricted_test` 的 Linux namespace isolation、默认禁网、环境/资源限制、进程清理、
  capability fail-closed 和并行 session 边界；
- M4.2 `run_command` 的固定 profile、executable allowlist、argv/cwd schema、非零退出观测、
  approval fail-closed 和不确定非幂等调用恢复；
- todo fixture 的 test `false → true`。

只能作为设计讨论、不能说“项目已经支持”：

- 任意时刻抢占中的 SQLite checkpoint resume；
- 真实 Provider live API 的运行效果（adapter wire contract 已有离线证据）；
- 生产流量中的 retry/fallback 效果；
- 未在固定数据集、baseline、样本数和成功率 delta 之外外推压缩 Token 节省；
- 未把当前小型固定 suite 的 task success rate 当作生产成功率；
- 所有平台/内核配置下的完整容器级隔离、OCI image lifecycle、通用 Shell 或已批准网络。

## 4. 需求变更流程

新增需求时先写一行追踪项，并回答：

1. 属于哪个里程碑，是否改变现阶段范围？
2. 由哪个模块负责，是否破坏现有依赖方向？
3. 成功、失败和恢复分别如何验收？
4. 需要新增或版本化哪些 Event/Golden/DB schema？
5. 会扩大哪些权限、数据保留或安全边界？
6. 完成后哪些面试表述从“设计”升级为“已实现”？

没有验收证据的能力只能标“已设计未实现”。
