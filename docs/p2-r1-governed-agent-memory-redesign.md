# P2-R1 Governed Agent Memory Redesign

> 状态：**历史候选设计；部分假设已被 M2-lite ADR 取代；实施未激活**
> 决策日期：2026-09-18
> 取代说明：2026-09-20 接受的
> [`decisions/memory-product-positioning.md`](./decisions/memory-product-positioning.md) 保留本文件的
> SQLite/governance/provenance/scope/audit 资产，但正式取代 `core_snapshot = ON`、默认 safe automatic
> reads、自动提炼以及 Core Snapshot 作为核心 Coding Agent 下一必需纵向切片的假设。以下相关段落仅作
> 历史设计记录；冲突处以该 ADR 为准。
> 前置事实：P2-M2 Memory 治理生命周期已实现；P2-M2.3 Dynamic Recall 候选评测已完成，
> 当前没有合格的 lexical/BM25 production backend。
> 激活规则：不得直接激活本文的默认 serving 路线。未来 Memory 实施必须从 accepted M2-lite ADR
> 与 [`coding-agent-v1-implementation-roadmap.md`](./coding-agent-v1-implementation-roadmap.md) M6 重新定义；
> 当前不修改 Memory 行为。

## 1. 为什么需要重设产品定义

P2-M2 建立了可靠的 Memory control plane：provenance、scope、revision、proposal/approval、
supersede、tombstone、audit 和 SQLite authority 均有实现与测试。这些能力解决“记忆为什么可信、
如何撤销、如何隔离”，但没有解决用户最直接的问题：新的 Session 中，Agent 是否会自然地使用
过去有价值的信息。

此前把治理属性当成了 Memory 的第一目标，并把写入风险传导成默认不读。结果是 Memory 虽然存在，
正常产品入口却不会使用它。P2-M2.3 又把所有长期记忆过度集中到单一 top-k retrieval 问题；其失败
证明当前 Dynamic Recall 路线存在 lexical ceiling，并不证明长期 Memory 产品本身无价值。

本阶段将设计哲学改为：

> **Useful-by-default, governed-by-design。**
>
> Memory 的首要产品目标是在正确时机提供过去有用的信息，提升跨会话连续性、任务成功率和用户体验；
> 可验证、可隔离、可撤销、可审计是不可突破的硬约束，而不是 Memory 存在的目的。

## 2. 保留什么，改变什么

### 保留：Memory Governance / Control Plane

```text
Runtime committed event
        ↓
Memory proposal
        ↓
provenance / scope / secret / conflict validation
        ↓
SQLite authoritative record
        ↓
version / supersede / tombstone / retrieval audit
```

SQLite 继续是唯一 authority；既有 schema、历史 evidence 和失败结果不得重写。Runtime FSM、
ToolHarness、workspace、Provider adapter 和 IPC 边界不因 Memory 绕路。

### 历史候选：Memory Serving Plane（默认策略已被 M2-lite supersede）

```text
                         Agent Context
                              ▲
             ┌────────────────┼────────────────┐
             │                │                │
       Core Snapshot     Dynamic Recall    History Search
             │                │                │
             └────────────────┼────────────────┘
                              │
                       SQLite Authority
                              │
                provenance / scope / version
                revision / tombstone / audit
```

三条 serving 路径不能再混为一个 top-k retriever：

| 路径 | 回答的问题 | 首版行为 |
|---|---|---|
| Core Snapshot | Agent 应该一直知道什么？ | Session 开始生成约 1000–1500 Token 的冻结快照 |
| Dynamic Recall | 当前任务还可能需要什么？ | 有界自动召回；没有合格后端前不得把现有 lexical/BM25 设为生产默认 |
| History Search | 过去具体发生过什么？ | 模型按需调用的只读、有界搜索，必须经过 ToolHarness |

Core Snapshot 是从 active records 编译出的 materialized view，不是新的 authority。它必须记录
`snapshot_id`、源 record/version、scope、生成时间、repository identity/revision policy、Token 成本，
并可在记录 stale/delete/supersede 后重建。首版只纳入稳定且高价值的信息，例如用户交互偏好、
持久环境、Repository 架构决策、确认过的约束与 conventions；不注入完整事件历史或工具输出。

## 3. 历史 safe automatic mode（已被 M2-lite supersede）

以下是原目标默认值，仅保留为历史设计记录：

```text
MemoryServingPolicy
  core_snapshot  = ON
  dynamic_recall = AUTO（仅使用已通过门禁的 backend）
  history_search = ON_DEMAND
  writes         = GOVERNED
```

不同层的默认语义必须分开：

- `AgentRuntime` kernel 保持 Memory-agnostic 和确定性，不直接读取数据库；
- 本地交互产品/controller 在 scope、identity 和 policy 明确时默认提供 Core Snapshot；
- headless IPC 不能隐式读取宿主机器上的 user/repository Memory，必须通过版本化 capability/profile
  显式协商 authority、scope 和数据来源；
- 无身份、scope 冲突、快照不可验证或组件失败时 fail closed，并继续运行为 memory-off，而不是注入
  来源不明的 Context；
- “读默认可用”不等于“写无限制自动发生”。

因此，当前默认 Application/headless/IPC 未接入 Memory 是**已知产品缺口和现状事实**，不再是最终
设计目标。何时改变具体入口，必须由 P2-R1 的契约、迁移和端到端评测决定。

## 4. 写入策略：按风险治理

不再把 explicit approval 作为所有写入的统一 UX。proposal 和 provenance validation 仍然必经，
之后按风险分层：

| 风险 | 例子 | 目标处置 |
|---|---|---|
| 低风险、可验证 | Python 3.12、测试命令、代码风格、用户希望回答简洁 | 可信 committed event + policy 校验后可自动 activate |
| 中风险或冲突 | 可能变化的架构事实、低置信推断、与 active record 冲突 | 保留 proposal，要求确认或更多证据 |
| 高风险/敏感 | 身份、凭据、安全策略、破坏性偏好、跨仓库事实 | 必须显式 approval；Secret 继续直接拒绝 |

所有自动 activate 都必须可见、可撤销、可审计，并带有来源、置信度、policy decision 和版本。模型
不能直接把任意文本写成 active Memory。

## 5. Revision 不再只有 exact binding

Repository Memory 需要显式的 revision binding mode：

| 模式 | 适用内容 |
|---|---|
| `exact` | 某次 commit 的失败、瞬时构建结果 |
| `compatible` | 在验证条件持续成立时跨 revision 有效的项目事实 |
| `path_bound` | 只与指定路径/组件变化相关的事实 |
| `content_hash_bound` | 与特定配置、接口或文件内容绑定的事实 |
| `revision_independent` | 稳定约定、用户偏好等与 commit 无关的信息 |

任何宽于 `exact` 的模式都需要可重验证条件；repository identity 仍不可跨越，不能用宽松 revision
语义放宽 user/repository scope containment。

## 6. 评测和可观测性

P2-R1 不以“记录写入成功”或单独 retrieval recall 作为产品验收。至少同时观测：

- 跨会话 cold/warm task success 与成功任务 Token；
- 用户已明确告知的稳定事实，在下一 Session 的可用率；
- Core Snapshot 的 relevant coverage、irrelevant occupancy、Token、编译/注入延迟；
- Dynamic Recall 的 recall、precision、irrelevant injection 和 first relevant action；
- History Search 的调用率、命中率、工具调用成本和是否减少盲目探索；
- 错误或过时 Memory 导致的行为变化、纠正时间与回滚成功率；
- cross-user/cross-repository/revision/stale/deleted leakage；
- 自动写入的 proposal→activate/reject/rollback 分布和敏感信息拦截率；
- Provider paired A/B，并明确区分 Runtime completion、oracle task success 和用户体验代理指标。

每次 Context build 必须能归因到 snapshot/retrieval/history source，记录实际 Context Token，而不保存
Provider reasoning、Secret 或宿主绝对路径。

## 7. 分阶段实施建议

P2-R1 激活后按以下顺序进行；每一步都要先有失败/恢复测试，再改变默认行为：

| 子阶段 | 主责模型 | 交付物 | 退出门禁 |
|---|---|---|---|
| R1-D1 产品契约与 ADR | Sol medium | serving policy、entrypoint/identity/scope、snapshot/revision/write-risk contract | 决策无歧义；不改运行行为 |
| R1-E1 产品行为 fixture | Luna max | 跨 Session continuity、stale/delete/conflict/scope 负例与指标 schema | baseline 真实暴露“记了但不会用”的缺口 |
| R1-M1 Core Snapshot vertical slice | Sol medium | compiler、materialized identity、Context manifest、local controller integration | rebuild/revoke/fail-closed、预算与端到端 A/B 通过 |
| R1-M2 History Search 与写入 UX | Sol medium | 只读工具、risk-tier activation、approval/rollback | 不绕过 Harness，敏感/冲突负例通过 |
| R1-E2 独立评测与文档收口 | Luna max | paired Provider/离线报告、Token/latency/quality dashboard、操作文档 | 证明净收益后才决定具体默认入口 |

Dynamic Recall 可并行保留为研究 seam，但不是 R1-M1 的前置条件。若后续评估 embedding/hybrid，必须
单独冻结 development/holdout、provider/model/version、隐私与成本边界；不能用它阻塞 Core Snapshot。

## 8. 明确非目标

P2-R1 本身不授权：

- 将 SQLite 替换为 `MEMORY.md` 或 vector store；
- 默认启用当前已拒绝的 lexical/BM25 backend；
- 引入 embedding provider、RAG framework、Skill、MCP 或多 Agent；
- 让 Memory 绕过 ToolHarness、Runtime Context manifest、workspace 或 IPC capability negotiation；
- 删除、改写 P2-M2.3/Holdout 的失败证据；
- 在没有真实 A/B 的情况下宣称 Memory 已提升通用 Agent 能力。

## 9. 主线程接续入口

本文只保留历史设计与 governance 背景，没有活跃实现范围。未来工作先读取
[`decisions/memory-product-positioning.md`](./decisions/memory-product-positioning.md)、
[`target-architecture-snapshot.md`](./target-architecture-snapshot.md) 和新 roadmap。只有用户明确激活
M6 的具体 acceptance criteria 后，才允许修改 Memory 产品 wiring；不得激活本文的 R1-M1 Core
Snapshot 路线，也不得把 Memory 设为核心 workflow 依赖。
