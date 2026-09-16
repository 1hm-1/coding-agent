# Coding Agent Phase 2 Product Architecture

> 文档状态：已批准设计；P2-M1 producer 与 P2-M2 Layered Memory 已完成，P2-M3 尚未激活。
> 固定发布基线：`v0.1.0`；当前开发版本为 `0.2.0.dev0`，事实仍以 [`current-state.md`](./current-state.md) 为准。
> 实施约束：任何子阶段开始前仍需单独激活、补充验收测试并更新交接文档。

## 1. 目标与定位

Phase 2 将当前单 Agent Runtime Kernel 演进为本地优先的成熟终端 Coding Agent，重点增加：

- 分层 Memory；
- Agent Profile 与 Skill；
- 受控 MCP capability gateway；
- 可恢复的任务内多 Agent 编排；
- terminal steering、approval 和可解释的 Agent Tree；
- 对上述能力的安全、恢复、成本和效果评测。

Phase 2 不替换当前 Runtime。`AgentRuntime` 继续是执行一个 Agent Session 的确定性 worker
kernel，新的产品能力通过上层 coordinator 和窄接口组合。

```text
v0.1.0
  Single-Agent Runtime Kernel
        │
        ├── FSM / checkpoint / call journal
        ├── Tool Harness / workspace / sandbox
        ├── context / compression
        └── trajectory / replay / eval
        │
        ▼
Phase 2
  Local Coding Agent Product
        ├── profiles / skills
        ├── layered memory
        ├── MCP capability gateway
        ├── multi-agent coordinator
        └── terminal interaction
```

## 2. 与 Agent Platform 的永久边界

两个项目属于同一系统的不同工程层，不重复实现彼此的核心职责。

| 责任 | Coding Agent | Agent Platform |
|---|---|---|
| 一次任务内的模型/工具循环 | 权威实现 | 不实现 |
| 子 Agent 分解、委派和结果整合 | 权威实现 | 视为一个不透明 Root Run |
| Working/Episodic/Semantic/Procedural Memory | 定义语义、选择和写入策略 | 最多提供持久介质或配置引用 |
| Skill 发现与加载 | 权威实现 | 最多保存启用配置 |
| MCP 工具发现与调用 | 权威实现 | 最多提供 secret reference、网络和租户策略 |
| workspace 内编辑、测试和 patch 整合 | 权威实现 | 为 attempt 准备受控执行根目录 |
| 多用户、认证、ownership | 不实现 | 权威实现 |
| HTTP、PostgreSQL、Outbox、Redis、Worker、SSE | 不实现 | 权威实现 |
| Runtime 内部 checkpoint 恢复 | 权威实现 | 不把 queue redelivery 误称为 checkpoint resume |
| run/attempt 的分布式投递与重试 | 不实现 | 权威实现 |

依赖方向固定为：

```text
Agent Platform worker
  → versioned subprocess/JSONL Runtime IPC
  → Coding Agent headless runner

Coding Agent
  ↛ Agent Platform package/database/queue
```

公共边界由 [`protocol/runtime-ipc-v1.md`](./protocol/runtime-ipc-v1.md) 定义。Platform 不读取
Coding Agent SQLite，不解析人类日志，也不依赖内部 Python 类型。

## 3. 非目标

Phase 2 不在 Coding Agent 中加入：

- FastAPI、API key、多租户 ownership；
- PostgreSQL run/attempt authority；
- Redis Streams、Outbox 或分布式 Worker；
- 平台级 retry、rate limit、SSE 或横向扩容；
- 默认网络或无授权的远程工具；
- 多个写 Agent 直接共享同一可写目录；
- 因功能清单而引入 LangChain/LangGraph 等第二套 Runtime；
- 未经 Eval 证明的任意并行、长期记忆自动写入或全量 MCP 开放。

## 4. 总体架构

```text
Terminal / Headless IPC
          │
          ▼
LocalSessionController
          │
          ▼
AgentCoordinator FSM ────────────────┐
    │                                │
    ├── AgentProfileRegistry         ├── Trajectory / Agent Tree
    ├── SkillRegistry                ├── Hierarchical Budgets
    ├── MemoryService                └── Cancellation / Recovery
    ├── CapabilityGateway
    │      ├── BuiltinToolProvider
    │      └── MCPToolProvider
    └── WorkspaceCoordinator
             │
             ▼
       AgentRuntime Kernel
             │
       ToolHarness / ModelBackend
```

设计规则：

1. `AgentRuntime` 不知道 Terminal、MCP transport 或父子 Agent Tree；
2. `AgentCoordinator` 不直接执行文件、命令或远程工具；所有副作用仍经过 ToolHarness；
3. Memory/Skill 只通过 Context Builder 的版本化输入进入模型上下文；
4. MCP 工具必须适配为内部 `ToolDefinition`/`ToolResult`，不能建立旁路；
5. workspace 写权限由 `WorkspaceCoordinator` 发放，首版坚持 single-writer；
6. coordinator 和每个 worker Runtime 使用不同 FSM、checkpoint 和预算；
7. SQLite 仍是本地产品恢复 authority，外部 IPC 只是脱敏投影。

## 5. 模块边界与未来目录

目录只在对应里程碑激活后创建；本节是结构契约，不要求提前建立空模块。

```text
src/coding_agent/
├── runtime.py                       # 现有单 Agent kernel；不承载产品编排
├── application.py                   # 现有本地用例；后续由 controller 组合
├── product/
│   ├── controller.py                # terminal/headless use cases
│   └── profiles.py                  # AgentProfile 与版本化快照
├── orchestration/
│   ├── coordinator.py               # Root Run/Agent Tree FSM
│   ├── domain.py                    # AgentNode、Delegation、Dependency
│   ├── scheduler.py                 # 有界 ready-node 调度
│   ├── integration.py               # 子结果汇总和 single-writer handoff
│   └── recovery.py                  # coordinator checkpoint/cascade cancel
├── memory/
│   ├── base.py                      # MemoryStore/Selector/Writer protocols
│   ├── domain.py                    # MemoryRecord、scope、provenance、state
│   ├── sqlite.py                    # 本地 authority implementation
│   ├── retrieval.py                 # bounded deterministic retrieval
│   └── policy.py                    # proposal/validation/approval/stale
├── skills/
│   ├── domain.py                    # SkillManifest 与 version identity
│   ├── registry.py                  # discovery、conflict、scope
│   ├── loader.py                    # instructions/resources/scripts
│   └── selector.py                  # deterministic candidates + model choice seam
├── capabilities/
│   ├── base.py                      # ToolProvider/CapabilitySnapshot protocols
│   └── gateway.py                   # namespace、permission、registration lifecycle
├── mcp/
│   ├── client.py                    # MCP transport adapter
│   ├── provider.py                  # MCP tools → ToolProvider
│   ├── auth.py                      # secret reference only
│   └── policy.py                    # server trust/network/output policy
├── workspace_coordination/
│   ├── coordinator.py               # ownership、revision、single-writer lease
│   └── patch.py                     # future isolated branch/patch integration
└── protocol/
    ├── headless.py                  # Runtime IPC producer
    └── projection.py                # internal events → public wire events

protocol/v1/                         # implementation-independent JSON Schemas
tests/
├── protocol/                        # producer/golden/compatibility tests
├── memory/
├── skills/
├── mcp/
└── orchestration/
```

不得把这些模块折叠回 `runtime.py`。跨层数据使用显式 dataclass/enum/Protocol，并提供版本化
JSON serialization；不持久化 pickle Runtime 对象。

## 6. 核心领域模型

### 6.1 Agent Profile

```python
@dataclass(frozen=True)
class AgentProfile:
    profile_id: str
    version: int
    role: str
    system_instructions: str
    model_policy: ModelPolicy
    allowed_tool_names: tuple[str, ...]
    allowed_skill_names: tuple[str, ...]
    memory_scopes: tuple[str, ...]
    max_children: int
```

Profile 是不可变执行快照。运行中修改磁盘配置不能改变已开始 Agent 的权限、Prompt 或预算。

### 6.2 Product Run 与 Agent Tree

```python
@dataclass(frozen=True)
class ProductRunSnapshot:
    run_id: str
    state: CoordinatorState
    root_agent_id: str
    active_agent_ids: tuple[str, ...]
    ready_agent_ids: tuple[str, ...]
    total_budget: HierarchicalBudget
    workspace_revision: str

@dataclass(frozen=True)
class AgentNode:
    agent_id: str
    run_id: str
    parent_agent_id: str | None
    profile_snapshot: AgentProfile
    assigned_task: str
    state: AgentNodeState
    runtime_session_id: str | None
    workspace_access: str
```

Agent identity、Runtime session identity 和外部 Platform execution identity 必须分离，不能复用
同一个字符串表达三种生命周期。

### 6.3 Delegation

```python
@dataclass(frozen=True)
class DelegationRequest:
    delegation_id: str
    parent_agent_id: str
    profile_name: str
    objective: str
    input_refs: tuple[str, ...]
    requested_budget: AgentBudget

@dataclass(frozen=True)
class DelegationResult:
    delegation_id: str
    child_agent_id: str
    status: str
    summary: str
    evidence_refs: tuple[str, ...]
    proposed_changes_ref: str | None
```

父 Agent 接收结构化结果和证据引用，不自动拼接子 Agent 全部历史，避免上下文无界增长。

## 7. 分层 Memory

### 7.1 四类记忆

| 层 | 生命周期 | 内容 | 当前基础 |
|---|---|---|---|
| Working | 当前模型调用/Session | recent messages、pending call、repo/test facts | v0.1 已实现基础 |
| Episodic | 跨 Session、同项目或用户 | 任务、尝试、失败、修改、最终验证 | Phase 2 新增 |
| Semantic | 跨 Session、有明确 scope | 架构事实、命令、规范、稳定约束 | Phase 2 新增 |
| Procedural | 可版本化能力包 | Skill instructions/scripts/resources | Phase 2 新增 |

Context compression 是 Working Memory 管理，不等于长期 Memory。SQLite messages/summaries 也不应
被直接宣传成跨会话用户记忆。

### 7.2 MemoryRecord

每条长期记忆至少包含：

```text
memory_id
schema_version
scope = session | repository | user
kind = episodic | semantic
content
source_run_id / source_agent_id / source_event_refs
repository_revision
confidence
created_at / expires_at
status = proposed | active | stale | rejected | deleted
supersedes
content_hash
```

### 7.3 写入规则

模型不能直接写入永久可信记忆：

```text
model proposes
  → schema validation
  → provenance validation
  → scope/permission policy
  → dedup/conflict check
  → optional user approval
  → commit active record
```

默认规则：

- Session 事实不能自动提升为 user scope；
- Repository 事实绑定 revision 或验证条件；
- Secret、完整 tool output、API key 和宿主绝对路径禁止写入；
- 删除是显式 tombstone/audit event，用户可以查看和清除；
- 旧事实冲突时不静默覆盖，通过 `supersedes` 或 conflict 状态表达；
- 检索结果必须进入 `context_built` manifest，记录 memory id、版本、分数和 Token 成本。

### 7.4 检索与评测

首版优先确定性 lexical/metadata retrieval；只有真实召回缺口证明需要时再增加 embedding/vector
backend。无论使用哪种算法，都必须有：

- top-k 和 Token 上限；
- scope containment；
- stale filtering；
- provenance 返回；
- cold/warm paired A/B；
- relevant recall、irrelevant injection、task success、Token 和 latency 指标；
- cross-user/cross-repository leakage 负例。

P2-M2 的当前交付是显式 Python composition：调用方将 Memory store、service、retriever 和 query
factory 组装到 `BudgetedContextBuilder`。默认 `AgentApplication` 与 `run-headless` 不自动创建、
查询或注入 Memory，也不把 Memory 声明为 Runtime IPC capability；默认启用需要后续独立的
产品入口、权限和评测决策。

## 8. Skill 系统

Skill 是 Procedural Memory，不是没有权限模型的 Prompt 文件拼接。

```python
@dataclass(frozen=True)
class SkillManifest:
    name: str
    version: str
    description: str
    instructions_resource: str
    allowed_tools: tuple[str, ...]
    required_permissions: tuple[Permission, ...]
    scripts: tuple[SkillResource, ...]
    compatibility: SkillCompatibility
```

流程：

```text
discover manifests
  → validate identity/schema/signature or local trust
  → filter by profile and permission
  → rank bounded candidates
  → select
  → load only referenced resources
  → record context and execution events
```

Skill 中的脚本不能绕过 ToolHarness。脚本只能通过可信 profile 或未来专用 Tool 执行，并继承当前
workspace、network、resource 和 output policy。

## 9. MCP Capability Gateway

MCP 是外部 capability provider，不是第二条工具执行路径。

```python
class ToolProvider(Protocol):
    def describe(self) -> CapabilitySnapshot: ...
    def discover(self) -> Sequence[ToolDefinition]: ...
    def invoke(self, call: ToolCall, context: ToolContext) -> ToolResult: ...
```

```text
MCP Server
  → MCP client/transport
  → MCPToolProvider
  → CapabilityGateway
  → namespaced ToolDefinition
  → ToolRegistry
  → ToolHarness
```

必须解决：

- Server identity、配置来源和版本快照；
- 工具命名空间，例如 `mcp.github.create_issue`；
- 动态 schema 大小和支持子集；
- 本地权限与远程工具权限映射；
- network/secret 的显式 approval；
- deadline、断线、取消和有界重连；
- 返回内容视为不可信 observation；
- 工具集合在一次模型调用期间不可悄然变化；
- discovery/invocation 的事件、Token 和延迟；
- 恶意 server、prompt injection、超大输出和名称冲突测试。

## 10. 多 Agent 编排

### 10.1 首个拓扑

首版固定为有界角色集合：

```text
Manager
├── Explorer       READ only，可并行
├── Implementer    唯一 WRITE owner
└── Reviewer       READ/TEST only
```

不允许模型递归创建任意角色。`AgentProfileRegistry` 决定可创建角色、最大数量、工具和预算。

### 10.2 Single-writer workspace

首版不让多个 Agent 并发写同一 workspace：

- Explorer/Reviewer 使用只读 snapshot；
- Implementer 持有唯一 writer lease；
- Manager 只整合结果，不直接写文件；
- 测试期间 workspace revision 被记录；
- writer crash 继续沿用现有 tool journal/reconciliation。

只有 Eval 证明多个独立写分支有收益后，才考虑：

```text
child isolated worktree
  → bounded patch artifact
  → deterministic validation
  → Integrator apply
  → conflict → WAITING_APPROVAL
```

### 10.3 Coordinator FSM

```text
CREATED
  → PLANNING
  → SCHEDULING
  → RUNNING_CHILDREN
  → INTEGRATING
  → VERIFYING
  → COMPLETED | FAILED

任何非终态
  → INTERRUPTED

不确定整合/外部副作用
  → WAITING_APPROVAL
```

Agent worker 的现有 FSM 不扩展为 coordinator FSM。Coordinator checkpoint 保存 Agent Tree、
dependency、ready/active 集合、预算和 workspace revision；每个 Agent Session 继续保存自己的模型、
工具和 observation 状态。

### 10.4 层级预算

Root Run 持有总预算，父 Agent 委派时预留而不是复制预算：

```text
root token/tool/time budget
  ├── manager reserve
  ├── child allocation
  └── final verification reserve
```

子 Agent 未使用预算可以归还；已消耗预算不能因 retry 或 resume 重置。必须记录：

- total/allocated/used/remaining；
- 每个 Agent 的 model/tool/token/time；
- delegation count/depth；
- cancellation 后未使用预算释放；
- 最终验证保留量。

### 10.5 中断、取消和恢复

- 父 Run interrupt 先停止创建新 Agent，再向活动子 Session 请求安全中断；
- 子 Agent 已持久化结果直接复用；
- read-only 子任务可以按策略重启；
- writer 不确定状态仍由现有 recovery mode 决定，不由 coordinator 猜测；
- cancellation 级联必须有 timeout 和终态聚合；
- 恢复时先恢复 coordinator lease，再恢复/重挂每个 child session；
- 不宣称跨主机 workspace portability，直到独立实现并验证。

## 11. Terminal 与 Headless 两个入口

```text
Terminal/TUI
  → 本地用户交互、steering、diff、approval、memory/skill/MCP 管理

Headless IPC
  → Agent Platform worker 使用的稳定机器协议
```

二者调用相同 application use cases，但不共享输出格式。Terminal 可以改变展示；Headless IPC 必须遵守
版本化 Schema。平台永远不解析 ANSI、进度条或自然语言日志。

## 12. 持久化与事件

未来 SQLite migration 应新增独立表，而不是把所有对象塞入 Session JSON：

```text
product_runs
agent_nodes
delegations
agent_dependencies
agent_profile_snapshots
memory_records
memory_retrievals
skill_uses
capability_snapshots
mcp_calls
workspace_leases
```

现有 `sessions/messages/events/checkpoints/model_calls/tool_calls/summaries` 保持单 Agent Runtime
authority。跨表 mutation 继续使用短事务、expected version 和 lease。

新增内部事件至少包括：

```text
product_run_created
agent_spawned / agent_started / agent_completed / agent_failed / agent_cancelled
delegation_requested / delegation_resolved
memory_proposed / memory_committed / memory_retrieved / memory_invalidated
skill_candidates_built / skill_selected / skill_loaded / skill_failed
capabilities_discovered / capability_rejected
mcp_call_started / mcp_call_finished
workspace_writer_acquired / workspace_revision_changed
product_run_finished
```

每个事件包含 `run_id`、`agent_id`、可选 `parent_agent_id` 和 `runtime_session_id`。公共 IPC 只投影安全、
稳定的子集。

## 13. 安全不变量

Phase 2 必须继承并加强以下不变量：

1. 所有本地和 MCP 工具副作用都经过 ToolHarness；
2. 原始 repository 不是写入目标；
3. 子 Agent 权限不能超过父授权和 AgentProfile 的交集；
4. Skill 不能扩大工具、network 或 filesystem 权限；
5. Memory 检索必须按 user/repository/session scope containment；
6. MCP server 输出和 Skill 内容都是潜在 prompt-injection 来源；
7. Secret 只在最窄 adapter/auth boundary 存在；
8. 多 Agent 不共享模型 API key 明文或宿主路径；
9. 未知远程副作用不自动重放；
10. capability probe/negotiation 失败时 fail closed。

## 14. 分阶段路线与门禁

### P2-D0：Architecture and Protocol Baseline（本轮文档）

交付：

- 本文；
- Runtime IPC v1 权威规范和 JSON Schemas；
- 兼容与版本策略；
- Agent Platform 消费侧文档同步。

退出条件：文档明确“已设计未实现”，两仓库责任不重合，协议只有一个权威来源。

### P2-M1：Runtime IPC v1 Bridge

- `protocol-info` 和 `run-headless`；
- request/event/result Schema validation；
- internal event public projection；
- cooperative interrupt、exit mapping、redaction；
- producer golden 和 Platform adapter contract tests。

本阶段先覆盖当前单 Agent Runtime，再开始 Memory/Skill/MCP。这样未来内部架构变化可由同一协议回归保护。

### P2-M2：Layered Memory

- episodic/semantic schema 和 SQLite store；
- proposal/approval/stale/delete；
- bounded retrieval/context manifest；
- leakage、cold/warm A/B 和 memory quality metrics。

### P2-M3：Agent Profiles and Skills

- immutable profile snapshot；
- Skill manifest/registry/loader/selector；
- permission intersection；
- deterministic selection baseline；
- malicious/broken Skill tests。

### P2-M4：MCP Capability Gateway

- MCP client provider；
- namespace/schema/permission/network/secret policy；
- discovery snapshot、timeout/cancel；
- malicious server and prompt-injection tests。

### P2-M5：Recoverable Multi-Agent Vertical Slice

- Manager/Explorer/Implementer/Reviewer；
- single-writer workspace；
- parent-child persistence and hierarchical budgets；
- cascade interrupt/resume；
- single-agent vs multi-agent paired Eval。

### P2-M6：Terminal Product and Release Evidence

- interactive steering/approval/diff；
- Memory/Skill/MCP management commands；
- Agent Tree/trajectory display；
- install/release/CI/golden/demo；
- external repository benchmark and limitation report。

## 15. 总体验收标准

Phase 2 只有在以下事实全部存在时，才能宣传为成熟终端 Coding Agent：

- v0.1 的 93 个默认测试、四份 semantic golden 和安全边界持续通过；
- Headless IPC 与 Terminal 使用相同 application semantics；
- v1 producer/consumer contract suite 通过；
- Memory 能证明 warm-run 收益且不存在 scope 泄漏；
- Skill 选择、权限和脚本执行可审计；
- MCP 异常、恶意输出、断线和 Secret 边界有负例；
- 多 Agent 在选定任务上相对单 Agent 有可重复收益，并报告额外 Token/延迟成本；
- writer crash、child crash、parent interrupt 和 cascade cancel 可恢复或进入明确等待/失败状态；
- 原仓库不变、workspace 隔离和 source invariant 继续成立；
- 不把本地 checkpoint 宣称为跨 Worker/跨节点恢复；
- README/current-state 只把真正实现且验证的能力标记为已完成。

## 16. 当前实施决策

P2-M1 与 P2-M2 已通过各自退出门禁。P2-M2 只实现 episodic/semantic Memory，验收见
[`p2-m2-implementation-plan.md`](./p2-m2-implementation-plan.md)。P2-M3 尚未激活；不得提前
开始 Skill、MCP、多 Agent、TUI、RAG 或向量检索。
