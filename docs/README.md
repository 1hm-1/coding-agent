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

当前代码事实以 [`current-state.md`](./current-state.md) 为准。目标架构以 [`architecture.md`](./architecture.md) 为准。两者冲突时，不要假设目标已经实现。

## 2. 推荐阅读顺序

第一次接手：

1. [`HANDOFF.md`](./HANDOFF.md)：工作区状态、禁止事项和下一步；
2. [`current-state.md`](./current-state.md)：当前代码能力与已知限制；
3. [`development-guide.md`](./development-guide.md)：环境、命令和开发流程；
4. [`contracts.md`](./contracts.md)：当前接口、事件和失败语义；
5. [`testing-strategy.md`](./testing-strategy.md)：测试与 golden 更新规则；
6. [`m2-implementation-plan.md`](./m2-implementation-plan.md)：M2 子阶段的实施顺序与验收；
7. [`m3-implementation-plan.md`](./m3-implementation-plan.md)：Context、Compression 和 Eval 验收；
8. [`architecture.md`](./architecture.md) 与 [`module-design.md`](./module-design.md)：长期设计；
9. [`requirements-traceability.md`](./requirements-traceability.md)：原始目标与面试能力的证据边界。

做代码评审：先读 `contracts.md` 和 `testing-strategy.md`，再对照当前里程碑退出条件。

准备面试讲解：读 `architecture.md`、`m1.5-engineering-hardening.md` 和 `roadmap.md`，但只能把 `current-state.md` 标为“已实现”的能力当作项目事实。

## 3. 文档职责

| 文档 | 回答的问题 |
|---|---|
| `current-state.md` | 现在真实能运行什么？有哪些技术债？ |
| `architecture.md` | 最终系统为什么这样分层？关键不变量是什么？ |
| `module-design.md` | 每个模块负责什么、依赖谁、将如何演进？ |
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

不要通过删除失败用例或放宽 golden 来“修复”回归。若行为确实需要改变，先在设计文档解释原因，再版本化迁移测试证据。
