# Conversation-Centered Runtime Refactor Plan

> 计划代号：**CR-R1 Conversation-Centered Runtime Realignment**  
> 状态：**SUPERSEDED 历史草案；从未激活**  
> 规划日期：2026-09-18  
> Superseded 日期：2026-09-20  
> 当前 authority：已接受的 ADR 集、[`target-architecture-snapshot.md`](./target-architecture-snapshot.md)、
> [`architecture-consistency-audit.md`](./architecture-consistency-audit.md) 与
> [`coding-agent-v1-implementation-roadmap.md`](./coding-agent-v1-implementation-roadmap.md)。
>
> 本文保留为决策演进记录。其 Conversation-owned isolated workspace、整个非终结 execution 持有
> writer authority、Memory serving 优先级和旧阶段顺序均不是当前目标。不得从本文激活实施；当前
> 实现仍未因新 ADR 发生代码或 Schema 变化。

## 1. 决策摘要

本草案建议将目标产品模型调整为：

```text
Conversation (长期用户容器)
  └── Turn (一次用户输入与最终回复)
        └── RuntimeExecution (一次可中断、可恢复的 Agent 执行)
              ├── model/tool loop
              ├── checkpoint/call journal
              └── execution events/result
```

首版保持 `Turn : RuntimeExecution = 1 : 1`。崩溃、中断或 approval 后继续的是同一个
`RuntimeExecution`，不是新 Turn；用户在一次正常完成后继续发言才创建新 Turn。

`Task` 不作为普通交互的必经实体。未来后台任务、定时任务或多次 attempt 的产品需要得到独立证据后，
可以增加可选关系：

```text
Turn ── may create/steer ── Task ── 1..N RuntimeExecutions
```

本计划保留现有 `AgentRuntime` 作为 RuntimeExecution kernel，不创建第二套 Agent loop。当前内部
`Session` 的产品语义将逐步收窄为 RuntimeExecution；物理表名和兼容 API 可以在迁移期保留。

## 2. 为什么现在必须先做这个重构

当前实现将四种生命周期合并进一个 `Session`：

1. 用户提交的 task；
2. 模型/工具执行及其 FSM；
3. transcript；
4. workspace。

具体表现为：

- `AgentApplication.run_task()` 为每个 task 创建一个新 `Session`；
- `AgentRuntime.initialize()` 要求新 Session 没有消息，并把 task 写成首条 user message；
- `PREPARING_WORKSPACE` 为每个 Session 复制一份 workspace；
- terminal Session 不能接受下一条正常用户消息，`resume` 只恢复未完成的同一次执行；
- SQLite `sessions` 同时保存 task、workspace、Runtime state、budget counters 和 final answer。

这套结构适合一次性 coding job，但不是 Hermes 式长期 Conversation。它已经造成下游路径依赖：

- 同一用户对前一轮的自然追问需要跨 Session 恢复，Memory 被迫承担本应由 transcript 提供的连续性；
- workspace 跟随 execution 销毁或分叉，下一轮不能自然观察上一轮修改；
- `COMPLETED` 容易同时被理解为执行结束、任务完成和会话结束；
- Phase 2 先设计 Product Run/Coordinator，再补 Terminal/Conversation，产品主干顺序倒置；
- P2-R1 的“跨 Session continuity”尚未区分同一 Conversation 内连续性与跨 Conversation Memory。

P2-R1 尚未实施，P2-M3、MCP、多 Agent 和 Terminal 也尚未开始。此时纠正身份与所有权，比这些模块
依赖现有 Session 语义后再迁移成本更低。

## 3. 目标与非目标

### 3.1 目标

- Conversation 成为长期用户可理解的容器，拥有 transcript、repository identity 和隔离 workspace；
- 每条正常用户输入形成一个 Turn，并由现有 Runtime kernel 执行；
- 同一 Conversation 的后续 Turn 能读取已提交 transcript，并看到前一 Turn 的 workspace 结果；
- `continue conversation` 与 `resume execution` 在 API、CLI、存储和文档中具有不同语义；
- Runtime FSM、ToolHarness、Provider adapters、checkpoint、call journal、replay 和 Eval 继续复用；
- 一次性 `run_task()` 和 Runtime IPC v1 保持兼容，可映射为临时 Conversation 的单 Turn；
- SQLite 继续是唯一状态 authority，迁移失败必须原子回滚；
- 为后续 Memory、Skill、MCP、多 Agent 和 Terminal 提供稳定的 Conversation/Turn/Execution 身份。

### 3.2 非目标

CR-R1 不授权：

- persistent Task FSM、任务看板、定时任务或 Cloud queue；
- 新 Agent framework、第二套 Runtime 或重写模型/工具循环；
- Skill、MCP、多 Agent、RAG/vector store 或默认网络；
- 修改 P2-M2 Memory 默认 wiring 或启用已拒绝的 lexical/BM25 Dynamic Recall；
- 直接写 source repository；Conversation workspace 仍是隔离副本；
- 破坏 Runtime IPC v1 或将私有 SQLite schema 暴露为公共协议；
- 把 `RuntimeState.COMPLETED` 宣称为外部 task success；
- 在产品契约和迁移测试批准前大规模重命名文件、表或公共命令。

## 4. 必须保持的工程不变量

1. 所有副作用继续经过 ToolHarness；controller 不能直接执行文件、命令或远程工具。
2. source repository 保持只读事实来源；所有修改发生在 Conversation 的隔离 workspace。
3. Provider-specific request/response 继续只存在于 adapter。
4. Runtime state transition 继续只由 Runtime FSM 提交。
5. SQLite 是 Conversation、Turn、Execution 和 journal 的唯一持久 authority；JSONL 仍是可重建导出。
6. model/tool call 在 crash window 中的 uncertain/reconcile 语义保持不变。
7. 非幂等副作用不自动重放，仍进入 `WAITING_APPROVAL`。
8. 同一 Conversation 首版只允许一个 active writer Turn；竞争请求必须等待或明确失败，不能并发改 workspace。
9. Context 中每条历史消息、摘要或 Memory 注入均可归因，不保存 Provider reasoning 或 Secret。
10. 四份 semantic golden 和现有 P2-M1 producer contract 必须持续通过，除非先批准版本化迁移。

## 5. 目标领域模型

### 5.1 Conversation

Conversation 是用户可恢复、可继续、可新建或归档的长期容器。建议最小字段：

```python
@dataclass(frozen=True)
class ConversationSnapshot:
    conversation_id: str
    status: ConversationStatus       # open | archived
    repository_id: str
    source_path: str                 # private SQLite field
    source_fingerprint: str
    workspace_path: str              # private SQLite field
    workspace_revision: str
    next_turn_ordinal: int
    created_at: str
    updated_at: str
    version: int
```

Conversation 不拥有模型/工具 FSM。`open/archived` 只控制是否接受新 Turn，不复制 Runtime state。
归档不删除 transcript 或 workspace；删除/清理由未来独立 lifecycle 设计处理。

### 5.2 Turn

Turn 表示用户的一次输入及其最终可见结果。建议最小字段：

```python
@dataclass(frozen=True)
class TurnRecord:
    turn_id: str
    conversation_id: str
    ordinal: int
    execution_id: str
    accepted_at: str
    finalized_at: str | None
```

Turn 首版不建立独立 FSM。它的运行状态由关联 RuntimeExecution 投影得到，避免 Turn state 与 Runtime
state 双重 authority。用户输入和 assistant/tool 消息保存在统一 message authority 中，不复制到
Turn 行的文本字段。

### 5.3 RuntimeExecution

RuntimeExecution 是当前 `Session` 的正确语义名称：一次有预算、状态机、checkpoint、call journal 和
terminal result 的 Agent 运行。建议目标字段：

```python
@dataclass(frozen=True)
class RuntimeExecutionSnapshot:
    execution_id: str
    conversation_id: str
    turn_id: str
    objective: str
    state: RuntimeState
    policy: RunPolicy
    workspace_path: str
    workspace_revision_at_start: str
    # existing pending calls, retry, final answer, counters, timestamps, version
```

第一轮实现可以继续使用 SQLite `sessions` 物理表和 `session_id` wire 字段，代码通过明确 alias/adapter
表达它们是 execution identity。只有兼容读写和 golden 都稳定后，才单独决定是否做物理重命名。

### 5.4 Message authority

目标是维持一个消息事实来源：

- 现有 `messages` 表增加 nullable `conversation_id`、`turn_id`；`session_id` 在迁移期继续指向 execution；
- 新 Conversation 消息同时带 Conversation、Turn、Execution identity；
- Conversation transcript 按 `(conversation_id, turn ordinal, message_index)` 查询；
- Runtime replay 继续按 `session_id/execution_id` 查询同一批行；
- legacy 行保持 `conversation_id IS NULL`，通过兼容投影读取，不在迁移时猜测跨 Session 关系；
- Summary、event、model call、tool call 继续 execution-scoped，避免把内部恢复状态塞进 Conversation。

这避免建立第二份可独立修改的 transcript，也避免用 Runtime event 反推产品消息。

## 6. 所有权和生命周期

### 6.1 创建 Conversation

```text
validate source
  → copy source into isolated conversation workspace
  → compute repository/workspace identity
  → atomically insert Conversation snapshot
  → return conversation_id
```

创建失败不能留下可被继续的 Conversation 行；已经创建的临时目录必须安全清理或标为 recoverable
orphan，不能把半初始化 workspace 当作 authority。

### 6.2 运行新 Turn

```text
acquire conversation writer lease
  → verify Conversation=open and no unresolved active execution
  → validate source identity and workspace binding
  → atomically allocate turn ordinal + Turn + RuntimeExecution + user message
  → freeze transcript/context/workspace revision input
  → run existing AgentRuntime
  → atomically append observations/final assistant message
  → update workspace revision and finalize Turn projection
  → release conversation lease
```

Runtime 在每次 execution 中仍使用 bounded context。Conversation transcript 不等于每次都把全量历史
送给 Provider；Context Builder 继续负责 hard retention、summary、budget 和 manifest。

### 6.3 恢复 Execution

`resume_execution(execution_id)` 只恢复 `INTERRUPTED`、`RETRY_WAIT` 或其他既有可恢复边界。它必须：

- 重新取得同一 Conversation writer lease；
- 验证 workspace path、repository identity 和 expected workspace revision；
- 恢复同一 Turn，不追加重复 user message；
- 复用现有 model/tool reconciliation；
- terminal execution、archived Conversation 或不匹配 workspace 必须拒绝。

### 6.4 开始下一 Turn

只有上一 execution 已 terminal，或其 `WAITING_APPROVAL` 已被明确解决后，才能创建下一 Turn。
`FAILED` execution 允许用户发新消息继续 Conversation，但失败事实必须保留在 transcript/context manifest；
不能把“开始新 Turn”伪装成对失败 execution 的 resume。

### 6.5 新建与归档

- `new conversation` 创建新的 transcript 和 workspace identity；
- `archive conversation` 阻止新 Turn，但不删除历史；
- 不从旧 Conversation 隐式复制 transcript；需要带入的信息以后由显式 fork 或 Memory policy 定义；
- CR-R1 不实现 fork/branch，避免在基础 identity 未稳定时引入 lineage。

## 7. 组件保留与改造映射

| 当前组件 | 处理 | 目标责任 |
|---|---|---|
| `AgentRuntime` | 保留并解耦初始化 | 执行一个 RuntimeExecution，不拥有长期 Conversation |
| `Session` / `RuntimeSnapshot` | 分阶段语义重命名或 alias | execution state/checkpoint |
| `AgentApplication.run_task()` | 保留兼容 wrapper | 创建临时 Conversation 并执行一个 Turn |
| `resume_session()` | 兼容 wrapper + 新名称 | 恢复同一个 RuntimeExecution |
| `WorkspaceManager` | 增加 create/bind 两条路径 | Conversation 创建 workspace；Execution 只绑定和校验 |
| `BudgetedContextBuilder` | 扩展输入 identity | 从冻结 Conversation transcript 构建本 Turn context |
| `SQLiteRunJournal` | 增加 Conversation/Turn 原子 mutation | 同一数据库 authority，execution journal 保持 |
| `TrajectoryRecorder` | 保留 | execution-scoped event，附加 correlation identity |
| `MemoryService` | 保留治理基础，推迟 serving | 只在 Conversation 边界明确后重新基线化 scope/source |
| Runtime IPC v1 | 保持兼容 | 继续代表一次 headless execution |
| CLI `run`/`run-scripted` | 保持 | one-shot compatibility path |
| CLI `sessions/show/resume` | 过渡兼容 | 明确显示 legacy session 即 execution；后续再弃用名称 |

## 8. Application API 草案

```python
class AgentApplication:
    def create_conversation(
        self,
        *,
        source: str | Path,
        conversation_id: str | None = None,
    ) -> ConversationSnapshot: ...

    def run_turn(
        self,
        conversation_id: str,
        *,
        message: str,
        backend: ModelBackend,
        policy: RunPolicy | None = None,
    ) -> TurnResult: ...

    def resume_execution(
        self,
        execution_id: str,
        *,
        backend: ModelBackend,
    ) -> TurnResult: ...

    def archive_conversation(self, conversation_id: str) -> ConversationSnapshot: ...

    # Compatibility: ephemeral Conversation + one Turn.
    def run_task(...) -> RunResult: ...

    # Compatibility alias during a documented deprecation window.
    def resume_session(session_id: str, ...) -> RunResult: ...
```

`TurnResult` 应同时返回 `conversation_id`、`turn_id`、`execution_id`、Runtime state、final answer、
workspace revision 和 trace reference。`COMPLETED` 仍只说明 Runtime 正常终止；task oracle success 继续
由 Eval 判断。

## 9. Workspace 策略

Conversation 拥有隔离 workspace 是本重构的关键决定：

- source 只在创建 Conversation 时复制一次；
- 每个 Turn 开始时记录 `workspace_revision_at_start`；
- 每次 committed edit/test observation 更新 execution journal，Turn 结束后更新 Conversation 当前 revision；
- Context summary 的文件事实继续绑定 workspace revision，过时事实按现有规则 stale；
- source fingerprint 在每个 Turn 结束后验证，继续证明原仓库未变；
- Conversation writer lease 保证首版 single-writer；读操作是否允许并发另行评估；
- workspace 丢失、路径越界、revision 不匹配或 symlink containment 失败时 fail closed；
- `run_task()` 的临时 Conversation workspace 保持当前对外可观察行为，不自动写回 source。

## 10. Context 与 Memory 重新分工

完成 CR-R1 后，Context 来源按生命周期区分：

| 来源 | 作用域 | 责任 |
|---|---|---|
| current Turn observations | RuntimeExecution | 当前模型/工具循环 |
| Conversation transcript/summary | Conversation | 同一对话的自然连续性 |
| Core Snapshot | 跨 Conversation 的 user/repository scope | 稳定且已治理的长期事实 |
| Dynamic Recall | 跨 Conversation | 当前目标可能需要的额外 Memory；仍需独立合格后端 |
| History Search | 旧 Conversation/Turn | 按需查找过去具体发生的事情 |

若本草案获批，P2-R1 在 CR-R1 完成前不实施。重新基线化时至少要修改：

- “Session 开始”改成 Conversation 创建和 Turn context build 两个明确边界；
- same-Conversation continuity 不计入 Memory warm-run 收益；
- Memory provenance 使用 Conversation/Turn/Execution 三类 identity，不能继续用一个 session id 代替；
- History Search 默认搜索旧 Conversation/已完成 Turn，不重复搜索当前 Context 已包含的消息；
- Memory proposal 只能来自 committed execution event/turn result；
- P2-M2 既有 record、tombstone、audit 和失败证据继续保留。

## 11. Runtime IPC 与外部兼容

CR-R1 不立即修改 Runtime IPC v1：

- `execution_id` 继续表示外部进程执行；
- `runtime_session_id` 在 v1 wire 上保持原字段名，内部文档明确其语义是 RuntimeExecution identity；
- `task` 继续是 one-shot execution objective；
- `run_id/attempt_id` 继续只作 Platform correlation，不变成 Conversation authority；
- headless v1 每次请求可映射到一个临时 Conversation 的单 Turn；
- 本地 interactive Conversation 先通过 application/controller 组合，不要求 Platform 读取内部 SQLite。

若未来 headless consumer 需要继续一个 Conversation，必须先决定 authority、workspace ownership、身份与
并发语义，再通过 capability/profile 或新协议版本实现。不得把 `conversation_id` 偷塞进未声明 metadata
并改变 v1 语义。

## 12. SQLite 迁移策略草案

### 12.1 Schema 方向

候选 migration v5 仅在 CR-M1 激活后定稿，预期包含：

- `conversations` 表；
- `turns` 表；
- `sessions` 增加 nullable `conversation_id`、`turn_id` 和 workspace start revision；
- `messages` 增加 nullable `conversation_id`、`turn_id`；
- Conversation turn ordinal 唯一约束；
- 一个 Turn 对应一个 execution 的首版唯一约束；
- Conversation/Turn/message 查询索引；
- conversation writer lease 字段或独立 lease 表。

### 12.2 Legacy 数据

- 不猜测多个历史 Session 是否属于同一 Conversation；
- 旧 Session 原样可 list/show/replay/export/resume（若本来可恢复）；
- 兼容层可以把单个 legacy Session 投影成只读 synthetic Conversation，但不得持久回填虚构 lineage；
- 新写入使用 Conversation/Turn identity，旧路径只通过 `run_task()` compatibility composition 写入；
- JSONL 不参与迁移，也不能反向成为 authority。

### 12.3 失败与回滚

- migration 在一个 SQLite transaction 中执行；任一 statement/validation 失败后 schema version 和数据均回滚；
- 测试覆盖 migration 重跑、未知高版本、部分 DDL 失败、约束冲突和进程重启；
- 升级前提供可验证的数据库备份/恢复操作说明；不实现不安全的自动 downgrade；
- 新 binary 必须能读取 legacy v4；旧 binary 遇到 v5 继续按既有 future-schema 规则拒绝；
- 行为切换使用 application composition gate，而不是同时维护两个可写 authority；
- 若 vertical slice 未过门禁，恢复到 legacy `run_task()` 入口，新表保持未启用或从备份恢复。

## 13. 分阶段实施安排

每个阶段必须独立提交，先通过本阶段 success/failure/recovery 门禁，再进入下一阶段。不得把所有 schema、
API、CLI 和 Memory 修改合成一次大提交。

### CR-D1：产品契约与 ADR（无运行行为变化）

交付物：

- [ ] 用户批准本文件的 Conversation → Turn → RuntimeExecution 主模型；
- [ ] 决定 Conversation workspace、legacy Session、Task 非目标和 IPC v1 边界；
- [ ] 新增正式 ADR，记录选 A 而非 mandatory Task/B/C 的理由；
- [ ] 更新 architecture/module/contracts 术语，但明确代码仍未改变；
- [ ] 冻结 CR-E1 fixture 和 identity glossary。

退出门禁：所有 identity、authority、resume/continue、workspace 和兼容语义无歧义；没有代码、Schema 或
默认行为变化。

### CR-E1：行为刻画与失败基线（测试先行）

交付物：

- [ ] 保留现有 169 个默认测试与四份 semantic golden；
- [ ] 增加当前 one-shot 行为的 characterization tests；
- [ ] 增加预期失败的 two-turn fixture，证明当前 terminal Session 不能自然继续；
- [ ] 增加 workspace continuity、new-conversation isolation、resume-vs-continue fixture；
- [ ] 冻结报告 schema：identity、turn result、revision、Token、latency、source invariant。

退出门禁：失败只暴露已知 Conversation 缺口；不得为了制造基线修改 Runtime 或测试 oracle。

### CR-M1：持久化 identity foundation

交付物：

- [ ] migration v5 与 Conversation/Turn repository ports；
- [ ] 原子 create Conversation / allocate Turn / bind Execution mutation；
- [ ] Conversation writer lease 和 optimistic version protection；
- [ ] legacy v4 read/replay/export compatibility；
- [ ] migration backup/restore 操作文档。

必测：

- [ ] create/load/list/archive success；
- [ ] duplicate ID、duplicate ordinal、archived Conversation 拒绝；
- [ ] crash-before-commit 不留下半个 Turn；
- [ ] migration statement failure 完整 rollback；
- [ ] 两个 writer 竞争只有一个取得 Turn；
- [ ] legacy DB 升级后 semantic replay 不变。

### CR-M2：RuntimeExecution 解耦

交付物：

- [ ] 领域层引入 RuntimeExecution 命名和 legacy `Session` adapter；
- [ ] Runtime 初始化接受 controller 冻结的消息/context 输入，不再隐式拥有长期 transcript；
- [ ] workspace prepare 拆成 create 与 bind/validate；
- [ ] event/call/checkpoint 继续 execution-scoped，并附 correlation identity；
- [ ] `resume_execution()` 恢复同一 execution/turn。

必测：

- [ ] 新 execution 从已有 Conversation transcript 构建 Context；
- [ ] Runtime 不直接查询 Conversation/Memory SQLite；
- [ ] workspace missing/revision mismatch/identity mismatch fail closed；
- [ ] model/tool crash windows、retry、approval recovery 与现有行为等价；
- [ ] terminal execution 不能 resume；下一 Turn 不复用前一 execution FSM counters。

### CR-M3：Conversation 两轮纵向切片

交付物：

- [ ] `create_conversation()`、`run_turn()`、`archive_conversation()`；
- [ ] Conversation transcript 查询和 bounded Context composition；
- [ ] Conversation-owned workspace continuity；
- [ ] `TurnResult` identity/result contract；
- [ ] local CLI 的最小 create/send/show 路径，暂不实现完整 TUI。

必测：

- [ ] Turn 2 能引用 Turn 1 的用户与 assistant 内容；
- [ ] Turn 2 能读取 Turn 1 修改的 workspace 文件；
- [ ] 两个 Conversation 的 transcript/workspace 不泄漏；
- [ ] Turn 1 失败后 Turn 2 可明确继续且保留失败事实；
- [ ] archived Conversation 拒绝新 Turn；
- [ ] 每个 Turn 后 source repository fingerprint 不变。

### CR-M4：兼容、恢复与并发收口

交付物：

- [ ] `run_task()` 映射临时 Conversation + 单 Turn；
- [ ] `resume_session()` 兼容映射与弃用说明；
- [ ] `sessions/show` 区分 legacy session 与 execution identity；
- [ ] Runtime IPC v1 golden 保持；
- [ ] Conversation/Turn/Execution replay 和 inspect 输出；
- [ ] 中断、approval、lease loss、process restart 的操作文档。

必测：

- [ ] 现有 calculator/todo smoke 输出和 source invariant 不回退；
- [ ] v0.1-kernel/v0.2-bridge contract vectors 不变；
- [ ] crash 后 resume 不重复 user message、assistant message 或 tool side effect；
- [ ] active/waiting-approval execution 阻止并发新 Turn；
- [ ] lease 失效时停止当前 execution，后继 writer 不被旧 holder 污染；
- [ ] JSONL 仍可从 SQLite committed events 重建。

### CR-E2：产品 Eval 与路线重新基线化

交付物：

- [ ] 冻结多轮 fixture：追问、修正、继续编辑、失败后恢复、新 Conversation 隔离；
- [ ] 报告 task success、Runtime completion、Token/turn、latency/turn、context attribution；
- [ ] 对比 one-shot compatibility path 与 Conversation path，不混淆不同产品目标；
- [ ] 更新 P2-R1 的 Memory scope、fixture 和指标定义；
- [ ] 更新 current-state、roadmap、README、contracts、module design、HANDOFF；
- [ ] 删除临时 feature gate 仅在新路径完全通过且 rollback 方案已验证后进行。

退出门禁：

- [ ] 同一 Conversation 多轮连续性不依赖 Memory；
- [ ] 跨 Conversation 无 transcript/workspace 泄漏；
- [ ] 现有 Runtime 成功、失败、恢复和安全证据无回退；
- [ ] one-shot CLI 与 IPC v1 保持兼容；
- [ ] P2-R1 已按新 identity 重新评审，之后才可单独激活。

## 14. 验收矩阵

| 场景 | Success 证据 | Failure 证据 | Recovery/Rollback 证据 |
|---|---|---|---|
| Conversation 创建 | workspace + row 原子存在 | 非法/重叠 source 拒绝 | crash 后无可用半成品 |
| 新 Turn | ordinal、message、execution 原子绑定 | archived/active writer 拒绝 | 分配前 crash 可重试且不跳号或重复消息 |
| 两轮连续 | transcript 与 workspace 均可见 | 新 Conversation 不可见 | Context build 失败不污染 transcript |
| Runtime crash | checkpoint/call journal 完整 | uncertain side effect 停止 | resume 同一 execution，不创建新 Turn |
| 并发输入 | 单 writer 串行化 | lease timeout/冲突明确 | stale holder 不能提交后继 Turn |
| migration | v4→v5 数据和 replay 保持 | DDL/validation 注入失败 | transaction rollback + backup restore |
| one-shot 兼容 | 旧入口行为等价 | invalid request 分类不变 | legacy replay/export 可用 |
| IPC v1 | golden/schema/sequence 不变 | unknown semantics fail closed | consumer 可识别 crash-before-result |
| source 安全 | 每 Turn source unchanged | path/symlink/revision 违规拒绝 | Conversation workspace 可继续或明确失效 |

## 15. 风险与控制

| 风险 | 后果 | 控制 |
|---|---|---|
| 同时维护两份 transcript | 上下文与展示分叉 | 扩展现有 message authority，不建独立可写副本 |
| Conversation state 与 Runtime state 重复 | 终态冲突 | Turn 不建独立 FSM，状态从 execution 投影 |
| workspace 从 execution 移交时越权 | 跨 Conversation 写入 | conversation identity + lease + path/revision 校验 |
| 过早物理重命名 sessions 表 | migration 和历史工具大面积破坏 | 先语义 alias，物理重命名单独决策 |
| compatibility wrapper 隐藏新缺陷 | 测试只跑旧入口 | CR-E1/M3 必须有独立 two-turn vertical slice |
| Memory 再次替 transcript 补洞 | 指标虚高、产品混乱 | same-Conversation fixture 禁止 Memory 注入 |
| controller 绕过 Runtime/Harness | 安全与审计旁路 | controller 只编排 identity/context/workspace lease |
| IPC 被内部模型拖动 | consumer breaking change | v1 保持 one-shot；Continuation 另做 capability/version 决策 |

## 16. 预计代码改动面

下表是实施影响预测，不代表这些文件已经获准修改：

| 文件/目录 | 预计变化 | 最早阶段 |
|---|---|---|
| `src/coding_agent/domain.py` | Conversation/Turn/RuntimeExecution dataclass、结果与 identity validation | CR-M1/M2 |
| `src/coding_agent/migrations.py` | transaction-safe schema v5 | CR-M1 |
| `src/coding_agent/persistence.py` | Conversation/Turn repository、原子分配、lease、legacy projection | CR-M1 |
| `src/coding_agent/workspace.py` | Conversation workspace create 与 Execution bind/validate 分离 | CR-M2 |
| `src/coding_agent/runtime.py` | 接收冻结 execution input；去除“新 Session 必须为空”和每次复制 workspace 的所有权假设 | CR-M2 |
| `src/coding_agent/context.py` | Conversation/Turn/Execution attribution 与跨 Turn bounded transcript 输入 | CR-M2/M3 |
| `src/coding_agent/application.py` | create/run-turn/resume-execution/archive use cases 与 legacy wrappers | CR-M3/M4 |
| `src/coding_agent/cli.py` | 最小 Conversation create/send/show；旧命令兼容提示 | CR-M3/M4 |
| `src/coding_agent/protocol/*` | v1 行为原则上不改，只补内部映射与 compatibility assertions | CR-M4 |
| `src/coding_agent/memory/*` | CR-R1 不改 serving；仅在 CR-E2 后重新评审 identity/provenance 接口 | CR-E2/P2-R1 |
| `tests/test_persistence.py` | v5 migration、原子性、rollback、legacy tests | CR-M1 |
| `tests/test_m2_recovery.py` | Execution resume、workspace revision 和 crash window 回归 | CR-M2/M4 |
| 新 Conversation 测试模块 | two-turn、isolation、single-writer、archive 和 compatibility vertical slice | CR-E1/M3/M4 |
| `tests/protocol/*` 与 goldens | 证明 Runtime IPC v1 没有 breaking change | CR-M4 |
| architecture/contracts/roadmap/HANDOFF | 只在对应行为落地后更新事实与 checklist | 每阶段收口 |

`ToolHarness`、内置工具、Provider adapter 和 sandbox 不应因本重构改变接口；若实现过程中发现必须修改，
应停止当前阶段并补充设计依据与独立回归门禁，不能作为顺手重构合入。

## 17. 实施期间的提交与审查边界

建议每个阶段至少形成一个独立、可回滚提交：

1. `docs: decide conversation runtime identities`；
2. `test: characterize conversation continuity gap`；
3. `feat: add conversation and turn persistence`；
4. `refactor: separate runtime execution from conversation ownership`；
5. `feat: add two-turn conversation vertical slice`；
6. `test: close conversation recovery and compatibility gates`；
7. `docs: rebase memory and phase 2 roadmap on conversation model`。

每次审查必须列出：变更的 authority、迁移版本、成功/失败/恢复测试、兼容影响、可执行 rollback、
仍未实现项。不得在同一提交中顺带加入 Skill、MCP、多 Agent、Memory serving 或新的工具能力。

## 18. 本次用户审查点

实施前需要用户明确批准或修改以下四项：

1. **主模型**：`Conversation → Turn → RuntimeExecution`，普通交互不强制创建 persistent Task；
2. **workspace ownership**：隔离 workspace 归 Conversation，多 Turn 复用；source repository 仍不写；
3. **兼容策略**：先保留 `sessions` 物理表和旧 API 名称，通过 alias/wrapper 迁移，不立即大重命名；
4. **路线顺序**：CR-R1 完成并重新评审后才实施 P2-R1，P2-M3 继续顺延。

在这四项获得批准前，本文件只是一份规划草案；当前代码、Schema v4、Memory wiring、Runtime IPC v1
和发布声明均不改变。
