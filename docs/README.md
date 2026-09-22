# 开发文档索引

本文是项目文档入口。接手开发时不要只读根目录 README；必须先确认“当前已经实现什么”和“下一里程碑允许改什么”。

## 1. 当前状态

| 里程碑 | 状态 | 权威文档 |
|---|---|---|
| M0 设计基线 | 已完成 | [`architecture.md`](./architecture.md) |
| M1 确定性纵向切片 | 已完成 | [`m1-vertical-slice.md`](./m1-vertical-slice.md) |
| M1.5 Engineering Hardening | 已完成 | [`m1.5-engineering-hardening.md`](./m1.5-engineering-hardening.md) |
| M2.1 SQLite 持久化基础 | 已完成 | [`m2-implementation-plan.md`](./m2-implementation-plan.md) |
| M2.2 中断恢复 / M2.3 真实模型 | 已完成 | [`m2-implementation-plan.md`](./m2-implementation-plan.md) |
| M3 Context Engine 与 Eval Harness | 已完成 | [`m3-implementation-plan.md`](./m3-implementation-plan.md) |
| M4.1 OS 级隔离基础 | 已完成 | [`m4-implementation-plan.md`](./m4-implementation-plan.md) |
| M4.2 结构化执行扩展 | 已完成 | [`m4-implementation-plan.md`](./m4-implementation-plan.md) |
| Release/Evidence Hardening | 已完成 | [`roadmap.md`](./roadmap.md)、[`current-state.md`](./current-state.md) |
| v0.1.0 固定发布基线 | 已完成 | [`releases/v0.1.0.md`](./releases/v0.1.0.md) |
| M5 有证据后的能力扩展 | M5.1 已完成，其余条件阶段 | [`roadmap.md`](./roadmap.md) |
| Phase 2 产品化架构与 IPC 契约（P2-D0） | 设计已完成 | [`v2-product-architecture.md`](./v2-product-architecture.md)、[`protocol/runtime-ipc-v1.md`](./protocol/runtime-ipc-v1.md) |
| Phase 2 P2-M1 Headless Runtime IPC | 已完成 | [`p2-implementation-plan.md`](./p2-implementation-plan.md) |
| Phase 2 P2-M2 Layered Memory | 已完成 | [`p2-m2-implementation-plan.md`](./p2-m2-implementation-plan.md) |
| P2-M2 Retrieval/Token 优化 | 已完成 | [`p2-m2-retrieval-optimization.md`](./p2-m2-retrieval-optimization.md) |
| Coding Agent V1 横向产品架构 | 已冻结；Product-Layer M0–M2 Accepted/完成；M3–M7 inactive，M3 未发布 | [`m2-product-lifecycle-design.md`](./m2-product-lifecycle-design.md)、[`evidence/m2-implementation-verification-2026-09-23.md`](./evidence/m2-implementation-verification-2026-09-23.md)、[`target-architecture-snapshot.md`](./target-architecture-snapshot.md)、[`architecture-consistency-audit.md`](./architecture-consistency-audit.md)、[`coding-agent-v1-implementation-roadmap.md`](./coding-agent-v1-implementation-roadmap.md)、[`v1-development-capability-matrix.md`](./v1-development-capability-matrix.md)、[accepted M2 milestone execution contract](./execution-contracts/m2-execution-contract.md) |
| CR-R1 Conversation-Centered Runtime Realignment | **SUPERSEDED；从未激活** | [`conversation-runtime-refactor-plan.md`](./conversation-runtime-refactor-plan.md) |
| P2-R1 Governed Agent Memory Redesign | 原默认 serving 假设已被 M2-lite ADR 取代；实施未激活 | [`decisions/memory-product-positioning.md`](./decisions/memory-product-positioning.md)、[`p2-r1-governed-agent-memory-redesign.md`](./p2-r1-governed-agent-memory-redesign.md) |

当前代码事实以 [`current-state.md`](./current-state.md) 为准。Runtime Kernel 基线以
[`architecture.md`](./architecture.md) 为准；产品层目标以
[`target-architecture-snapshot.md`](./target-architecture-snapshot.md) 与 accepted ADR 为准。目标与
当前事实冲突时，不要假设目标已经实现。

### 1.1 已接受但尚未实施的产品架构 ADR

以下 ADR 已完成 architecture decision，均不表示当前代码、Schema、CLI 或 Runtime IPC 已经实现：

- [`decisions/project-repository-workspace-conversation.md`](./decisions/project-repository-workspace-conversation.md)：RepositoryIdentity、ProjectScope、WorkspaceBinding 与 Conversation；
- [`decisions/conversation-turn-runtime-execution-lifecycle.md`](./decisions/conversation-turn-runtime-execution-lifecycle.md)：Conversation、Turn 与 RuntimeExecution lifecycle；
- [`decisions/tool-capability-command-permission.md`](./decisions/tool-capability-command-permission.md)：Tool capability、command execution 与 permission UX；
- [`decisions/diff-code-checkpoint-undo.md`](./decisions/diff-code-checkpoint-undo.md)：Diff authority、CodeCheckpoint 与 undo；
- [`decisions/project-instructions-repository-rules.md`](./decisions/project-instructions-repository-rules.md)：Project instructions 与 repository rules；
- [`decisions/context-composition-compaction.md`](./decisions/context-composition-compaction.md)：per-ModelRequest context composition、compaction、Summary lineage 与恢复边界。
- [`decisions/memory-product-positioning.md`](./decisions/memory-product-positioning.md)：M2-lite、显式 UserPreference Memory、authority 与默认关闭边界。
- [`decisions/interactive-cli-product-workflow.md`](./decisions/interactive-cli-product-workflow.md)：Interactive CLI、thin ConversationApplicationCoordinator、Turn admission、writer safe release 与端到端产品流程。

## 2. 推荐阅读顺序

第一次接手：

1. [`HANDOFF.md`](./HANDOFF.md)：工作区状态、禁止事项和下一步；
2. [`current-state.md`](./current-state.md)：当前代码能力与已知限制；
3. [`target-architecture-snapshot.md`](./target-architecture-snapshot.md)：冻结后的产品主干与 authority；
4. [`architecture-consistency-audit.md`](./architecture-consistency-audit.md)：旧假设与 accepted ADR 的冲突及 supersession；
5. [`coding-agent-v1-m0-characterization.md`](./coding-agent-v1-m0-characterization.md)：M0 的当前到目标、迁移、重试、指标、baseline 与退出证据；
6. [`coding-agent-v1-implementation-roadmap.md`](./coding-agent-v1-implementation-roadmap.md)：M0–M2 已 Accepted/完成、M3–M7 未激活的实施 DAG；
7. [已完成的 M0 milestone execution contract](./execution-contracts/m0-execution-contract.md)：M0 executor 的历史授权范围、禁区、证据与退出条件；
8. [已完成的 M1 milestone execution contract](./execution-contracts/m1-execution-contract.md)：M1 executor 的已完成领域主干/持久化迁移范围、禁区、验收与停机条件；
9. [accepted M2 milestone execution contract](./execution-contracts/m2-execution-contract.md)：M2 Workspace/lifecycle/atomic admission 封闭范围、只读 real-tree 门禁、验收与完成状态；
10. [`v1-development-capability-matrix.md`](./v1-development-capability-matrix.md)：V1 能力承诺、权限结果、M3+M4 real-tree mutation gate 与明确 non-goals；
11. 本页列出的全部 accepted ADR；
12. [`development-guide.md`](./development-guide.md)：环境、命令和开发流程；
13. [`contracts.md`](./contracts.md)：当前接口、事件和失败语义；
14. [`testing-strategy.md`](./testing-strategy.md)：测试与 golden 更新规则；
15. [`architecture.md`](./architecture.md) 与 [`module-design.md`](./module-design.md)：当前 Runtime Kernel 架构；
16. [`protocol/runtime-ipc-v1.md`](./protocol/runtime-ipc-v1.md)：当前 Runtime producer 与未来 Agent Platform consumer 的进程契约；
17. [`requirements-traceability.md`](./requirements-traceability.md)：原始目标与面试能力的证据边界；
18. 已完成的 P2-M1/P2-M2 实施文档与冻结评测证据。

做代码评审：先读 `contracts.md` 和 `testing-strategy.md`，再对照当前里程碑退出条件。

准备面试讲解：读 `architecture.md`、`m1.5-engineering-hardening.md` 和 `roadmap.md`，但只能把 `current-state.md` 标为“已实现”的能力当作项目事实。

## 3. 文档职责

| 文档 | 回答的问题 |
|---|---|
| `current-state.md` | 现在真实能运行什么？有哪些技术债？ |
| `architecture.md` | 当前 v0.1 Runtime Kernel 为什么这样分层？关键不变量是什么？ |
| `module-design.md` | 当前代码每个模块负责什么、依赖谁？ |
| `target-architecture-snapshot.md` | 冻结后的一级领域主干、authority 和 Runtime Kernel 复用边界是什么？ |
| `architecture-consistency-audit.md` | 哪些旧假设与 accepted ADR 冲突，哪个决定是 authority？ |
| `coding-agent-v1-implementation-roadmap.md` | 如何按依赖从 M0 增量迁移到 M7，且让 Memory 不阻塞 critical path？ |
| `coding-agent-v1-m0-characterization.md` | M0 验证了哪些现状、迁移输入、缺陷、指标与退出条件？ |
| `execution-contracts/m0-execution-contract.md` | 已完成的 M0 milestone execution contract 曾授权 executor 做什么、禁止什么、必须提交哪些证据？ |
| `execution-contracts/m1-execution-contract.md` | 已完成的 M1 milestone execution contract 曾授权什么、禁止什么、必须如何验证和停机？ |
| `execution-contracts/m2-execution-contract.md` | Accepted M2 如何限定 Workspace/lifecycle/atomic admission、只读 real-tree、兼容与验收？ |
| `v1-development-capability-matrix.md` | V1 具体承诺哪些 filesystem/Git/Python/command 能力，哪些为 DENY/UNAVAILABLE，何时允许 real-tree mutation？ |
| `v2-product-architecture.md` | Phase 2 成熟终端 Coding Agent 如何扩展，哪些仍未实现？ |
| `protocol/runtime-ipc-v1.md` | Runtime producer 对 Platform consumer 暴露什么稳定进程协议？ |
| `protocol/compatibility.md` | IPC v1 如何演进、兼容和发布？ |
| `../protocol/v1/README.md` | 四个 IPC Schema 分别负责什么，如何修改和计算 digest？ |
| `contracts.md` | 当前代码接口、状态、事件和错误的精确定义是什么？ |
| `repository-structure.md` | 当前有哪些文件，未来文件应该放在哪里？ |
| `development-guide.md` | 新开发者如何安装、运行、修改和排错？ |
| `testing-strategy.md` | 什么测试证明什么能力？golden 怎么维护？ |
| `requirements-traceability.md` | 原始需求和面试高频主题分别由什么阶段提供证据？ |
| `roadmap.md` | 里程碑顺序、状态、交付物与退出门禁是什么？ |
| `m1-vertical-slice.md` | M1 为什么这样收敛，验收证据是什么？ |
| `m1.5-engineering-hardening.md` | 故障语义和长期回归门禁是什么？ |
| `m2-implementation-plan.md` | M2 如何按子阶段实施与验收？ |
| `m3-implementation-plan.md` | 如何实现可评测的上下文预算、压缩和 Eval Harness？ |
| `m4-implementation-plan.md` | 如何用 OS 强边界隔离执行并验证 threat model？ |
| `m5-eval-expansion.md` | 新工具能力是否已经被评测证据证明需要？ |
| `p2-implementation-plan.md` | P2-M1 实现了什么、哪些门禁已经通过？ |
| `p2-m2-implementation-plan.md` | P2-M2 Memory 实现了什么、哪些信任与质量门禁已经通过？ |
| `conversation-runtime-refactor-plan.md` | 已 superseded 的 CR-R1 历史草案；只用于理解决策演进。 |
| `decisions/context-composition-compaction.md` | ModelRequest context 如何从权威状态投影、压缩、审计并精确恢复？ |
| `decisions/memory-product-positioning.md` | Memory 在 Coding Agent 中承担什么职责、何时保存或注入，以及哪些默认 serving 假设已延期？ |
| `decisions/interactive-cli-product-workflow.md` | CLI 如何把 Conversation、Runtime、permission、workspace、diff/undo 和恢复串成完整产品流程？ |
| `p2-r1-governed-agent-memory-redesign.md` | 哪些早期 Memory serving 假设已被 M2-lite 取代，哪些 governance 资产继续保留？ |
| `resume-benchmark.md` | 简历稳定性指标和上下文压缩开关 A/B 如何设计、运行与解读？ |
| `releases/v0.1.0.md` | 固定版本如何安装、验收、运行 Eval/Demo，支持边界是什么？ |
| `HANDOFF.md` | 当前工作区如何安全交接？ |

## 4. 文档更新规则

代码变更时至少检查下表：

| 变化 | 必须更新 |
|---|---|
| 新增/删除状态或迁移 | `contracts.md`、`architecture.md`、状态机测试、受影响 golden |
| 修改 Event 字段或语义 | `contracts.md`、schema version、Replay、golden migration |
| 新增工具 | `contracts.md`、`module-design.md`、权限/路径/超时测试 |
| 修改里程碑范围或顺序 | `roadmap.md`、对应实施文档、`current-state.md` |
| 完成里程碑 | `roadmap.md` 状态、`current-state.md`、README、项目备忘录 |
| 新增持久化表 | `m2-implementation-plan.md` 或后续迁移文档、恢复测试 |
| 修改 CLI | README、`development-guide.md`、CLI smoke test |
| 修改安全边界 | `architecture.md` threat model、负例测试、README 限制说明 |
| 修改 Runtime IPC | `protocol/runtime-ipc-v1.md`、`protocol/v1/*.schema.json`、兼容矩阵、producer/consumer contract tests |

不要通过删除失败用例或放宽 golden 来“修复”回归。若行为确实需要改变，先在设计文档解释原因，再版本化迁移测试证据。
