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
| read/search/edit/shell/test/git | read/edit/test 已实现；M4.2 增加结构化 `run_command`；M5.1 增加只读字面量 `search_files`；其余条件扩展 | search 已由独立 live A/B 批准；后续 capability holdout 12/12 未形成 git/更广执行的失败覆盖；不提供 shell 字符串 | `test_tools.py`、`test_m2_recovery.py`、`test_m4_execution.py`、search decision record、固定 eval 和 capability holdout |
| Workspace ownership / isolation | 当前 one-shot/headless 使用 copied workspace；V1 interactive 目标为 direct working tree 默认、显式 managed worktree 隔离 | `workspace.py`、`sandbox/`；[`project-repository-workspace-conversation.md`](./decisions/project-repository-workspace-conversation.md) | 既有 source fingerprint/path escape/namespace tests 保留；新产品路径需 writer、drift、rebind、worktree 与 recovery-barrier 验收 |
| messages/tool/state/metadata 持久化 | M2 已完成，SQLite authority 包含调用 journal | SQLite journal | transaction、round-trip、rollback、export、recovery tests |
| 可中断恢复 session | 已实现 | M2.2 Application/Runtime + lease | state-boundary/crash-window/reconciliation/resolution tests |
| recent/task/repo/compressed context | 已实现：预算、硬保留、摘要 lineage 和 stale invalidation | `context.py`、`compression.py`、`workspace.py` | retention/token/compression tests；paired A/B seam |
| Eval Harness | 已实现；golden 仍是 Runtime 回归子集 | `evaluation.py`、`cli.py` | versioned suite、trusted oracle、batch report |
| success/tool/token/latency/failure/recovery 指标 | M3 已完成离线聚合 | `evaluation.py` + committed events | report schema、失败分母和 recovery tests |
| replayable structured trajectory | 已实现，M2.1 已将 SQLite 设为 authority | `trajectory.py` / SQLite export | sequence/replay/golden/export equivalence |
| Release/Evidence hardening | 已完成 | Git/CI、coverage、扩大的 mypy 门禁、recovery metrics、multi-repository eval、opt-in provider smoke | `.github/workflows/`、`pyproject.toml`、`evaluation.py`、`examples/eval_suite.json`、默认测试与手动 smoke；提交 `cf82f3c` 的 Python 3.10/3.11 hosted CI 成功；不宣称生产成功率 |
| 成熟终端 Agent 产品扩展 | 横向产品架构已冻结；M0 已 Accepted/完成；M1 ACTIVE、M2–M7 未激活；本发布轮未实现 | [`target-architecture-snapshot.md`](./target-architecture-snapshot.md)、[`coding-agent-v1-implementation-roadmap.md`](./coding-agent-v1-implementation-roadmap.md)、[`execution-contracts/m1-execution-contract.md`](./execution-contracts/m1-execution-contract.md) | Headless IPC 与 Memory governance 是可复用资产；Conversation/Turn/RuntimeExecution、direct workspace、Instructions、frozen Context、Permission、Diff/Undo 与 interactive CLI 仍需按 M0–M7 验收；Memory M2-lite 不阻塞 critical path |
| Runtime 与 Agent Platform 集成 | P2-M1 producer 已完成；Platform consumer 待外部验证 | [`protocol/runtime-ipc-v1.md`](./protocol/runtime-ipc-v1.md)、`protocol/v1/*.schema.json`、[`p2-implementation-plan.md`](./p2-implementation-plan.md)、`tests/test_protocol.py` | discovery/headless、golden、取消/退出码、v0.1/v0.2 vectors 已通过；consumer suite 尚未执行 |

## 2. 面试高频主题映射

本表来自项目外的 `docs/面试问题.md`。它是能力覆盖清单，不是要求为了“题目齐全”加入无用功能。

| 主题 | 项目中的回答位置 | 证据成熟度 | 表达边界 |
|---|---|---|---|
| Claude Code/ReAct 完整链路 | [`architecture.md`](./architecture.md) §7、[`contracts.md`](./contracts.md) | 已实现 | 是显式 FSM 的 tool-observation loop，不宣称复刻 Claude Code 内部实现 |
| Agent Harness | [`architecture.md`](./architecture.md) §9、[`contracts.md`](./contracts.md) §4 | 已实现 | 讲 schema、权限、deadline、归一错误和 observation |
| 工具慢、阻塞与 timeout | [`current-state.md`](./current-state.md)、[`module-design.md`](./module-design.md) | 已实现可终止边界 | 普通 handler 使用可杀死 worker 进程组；sandbox 工具使用 executor wall timeout；仍不宣称任意 Runtime 时刻可抢占 |
| 任务中断恢复 | [`m2-implementation-plan.md`](./m2-implementation-plan.md) §4 | 已实现 | 只宣称状态边界中断和定义 crash window 的可解释恢复 |
| 两类模型后端统一 | [`m2-implementation-plan.md`](./m2-implementation-plan.md) §5 | 已实现 | 两个 adapter 通过离线 contract，另有一次 DeepSeek opt-in smoke；未宣称多次 live baseline 或生产成功率 |
| retry/fallback | [`m2-implementation-plan.md`](./m2-implementation-plan.md) §5 | 已实现 | 只对分类基础设施错误；质量差不自动 fallback |
| 重复工具调用/幂等 | [`contracts.md`](./contracts.md)、M2.2 recovery rules | 部分实现 | 已确认结果不重复；未知写操作需 resolution，不能宣称 exactly-once |
| 上下文压缩与信息丢失 | [`architecture.md`](./architecture.md) §13、[`roadmap.md`](./roadmap.md) §7 | M3 已实现 | 不编造 Token 降幅；用 lineage、required-fact retention 和 task success A/B |
| 短期/长期记忆 | Context M3 与 P2-M2 governance lifecycle 已实现；V1 接受 M2-lite | control plane 已实现；显式 UserPreference 产品接线、History Search 未实现；Core Snapshot 与 generic auto top-k 默认 OFF | 可讲受控 lifecycle、provenance、scope 和冻结证据；Memory 是 optional low-authority source，Memory-off 是受支持的核心产品状态，不得宣称尚未实现的 serving 能力 |
| Eval 体系和 Badcase 定位 | [`testing-strategy.md`](./testing-strategy.md)、[`roadmap.md`](./roadmap.md) §7 | M3 离线 eval + M5.1 live A/B + capability holdout 已实现 | 能区分 oracle/runtime/e2e 和无效调用；小样本不外推生产成功率 |
| A/B 与上线迭代 | [`roadmap.md`](./roadmap.md) M3 | 离线 paired A/B 已实现 | 只做固定 suite 的描述性比较；真实流量实验不在当前项目证据内 |
| 安全、权限、Prompt Injection | [`architecture.md`](./architecture.md) §10、M4.1/M4.2 | 应用层 + Linux namespace 部分实现 | capability fail-closed、structured argv allowlist、secret/network/escape/resource/approval tests；不宣称抵御内核漏洞或跨平台等价 |
| 单 Agent vs 多 Agent | [`v2-product-architecture.md`](./v2-product-architecture.md) | 当前单 Agent；协调层已设计 | `AgentRuntime` 保持单任务内核；P2-M5 才能宣称可恢复多 Agent，并需对比单 Agent eval |
| Skill/RAG/Memory 关系 | [`v2-product-architecture.md`](./v2-product-architecture.md) | Memory lifecycle 已实现；合格 retrieval、Skill/RAG 未实现 | Memory 是带来源的数据；当前无生产检索候选；Skill 是未来受版本治理的流程包，semantic/embedding retrieval 只能作为独立 P2-M2.4 提案 |
| MCP 能力接入 | [`v2-product-architecture.md`](./v2-product-architecture.md) | 已设计未实现 | MCP 必须经 CapabilityGateway、ToolRegistry 和 ToolHarness；P2-M4 前不宣称支持 |
| 指标是否只看成功率 | [`architecture.md`](./architecture.md) §14 | 已设计，部分指标可 replay | 区分 runtime completion 与 task success，保留失败 run |
| Demo 与 production 区别 | [`current-state.md`](./current-state.md)、[`roadmap.md`](./roadmap.md) | 持续演进 | 用恢复、安全、评测、观测门禁说明，不使用“生产可用”标签 |

## 3. 当前可展示证据

可以现场运行和解释：

- `ScriptedBackend` 驱动的确定性完整任务；
- 显式 FSM 与非法迁移保护；
- 五工具统一 Harness（包含只读 `search_files` 和结构化 `run_command`）；
- source unchanged / isolated workspace modified；
- permission denied、handler error、timeout 和 test recovery；
- JSONL replay 与四份 semantic golden；
- SQLite schema v4、session/message/checkpoint/call journal/summary 与 Memory record/audit round-trip、atomic mutation 和删除 JSONL 后重建 Runtime projection；
- episodic/semantic proposal→approval、scope/provenance/revision 隔离、delete tombstone、Context
  manifest 和 12-case 冻结 cold/warm calibration；lexical retrieval 仅供显式实验，P2-M2.3 已
  completed with no qualifying backend，默认 Application/headless/IPC 保持 Memory-disabled；
- 状态边界 resume、工具 crash/reconciliation、lease takeover、retry/fallback 和两个 adapter 的离线 contract；
- context section budget、token counter fallback、summary lineage/stale/rejection 和 eval oracle/report/A-B；
- M4.1 `restricted_test` 的 Linux namespace isolation、默认禁网、环境/资源限制、进程清理、
  capability fail-closed 和并行 session 边界；
- M4.2 `run_command` 的固定 profile、executable allowlist、argv/cwd schema、非零退出观测、
  approval fail-closed 和不确定非幂等调用恢复；
- todo fixture 的 test `false → true`。

只能作为设计讨论、不能说“项目已经支持”：

- 任意时刻抢占中的 SQLite checkpoint resume；
- 真实 Provider 的通用运行效果（已有脱敏的小样本 DeepSeek 定向证据，不外推生产成功率）；
- 生产流量中的 retry/fallback 效果；
- 未在固定数据集、baseline、样本数和成功率 delta 之外外推压缩 Token 节省；
- 未把当前小型固定 suite 的 task success rate 当作生产成功率；
- 显式 UserPreference Memory 的 V1 产品入口与按需 Conversation History Search；
- generic automatic Memory extraction、默认 Core Snapshot/top-k、ProjectExperience auto recall、
  semantic/embedding/vector index；
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
