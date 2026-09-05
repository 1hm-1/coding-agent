# Coding Agent 架构设计

> 文档类型：目标架构与不变量  
> 当前实现：M4.2，见 [`current-state.md`](./current-state.md)  
> 下一实施阶段：M5 条件阶段，见 [`roadmap.md`](./roadmap.md)

## 1. 状态标记

本文使用：

- **[CURRENT]**：当前代码已实现并有测试；
- **[M2]**：M2 已完成的持久恢复与模型边界实现；
- **[M3]**：Context/Eval 阶段实现；
- **[M4]**：OS 级隔离阶段实现；M4.1 foundation 与 M4.2 structured execution 已完成；
- **[TARGET]**：长期不变量或最终形态。

看到目标接口时不要假设代码已经存在。当前精确 API 以 [`contracts.md`](./contracts.md) 为准。

## 2. 系统目标

本项目实现一个面向本地代码任务的单 Agent Coding System。模型是不可靠的决策组件；真实副作用、状态、权限、恢复和评测由确定性工程组件控制。

它要证明的不是“模型能写代码”，而是：

1. 模型决策如何进入受控执行循环；
2. 工具调用如何校验、授权、超时、记录和恢复；
3. 原仓库如何与任务修改隔离；
4. 进程中断后如何根据持久事实继续；
5. 上下文变长后如何保留关键事实；
6. 如何通过轨迹和 oracle 区分 Runtime 成功与任务成功。

## 3. 非目标

- 不为了展示复杂度默认采用多 Agent；
- 不在核心 Runtime 中依赖 LangChain/LangGraph 等 Agent framework；
- 不在 Runtime 稳定前建设 UI 或消息平台；
- 不用 LLM judge 替代可程序化的代码测试和文件断言；
- 不提供模型可自由控制的通用 Shell；M4.2 只提供固定 profile 下的结构化 argv 执行；
- 不把一次用户指令自动升级为跨会话长期偏好。

## 4. 核心不变量

### 4.1 控制流

- [TARGET] Runtime 状态只能经显式允许迁移表改变。
- [CURRENT] `step()` 每次执行一个状态 handler；`run()` 只是驱动器。
- [CURRENT] Context input budget 来自 model capability、reserved output 和 protocol margin；
  required content 无法适配时显式失败。
- [CURRENT] Tool/Backend/Context 错误通过分类结果影响状态，不能依赖错误字符串匹配。

### 4.2 副作用

- [TARGET] Runtime 不直接读写代码或启动进程，所有副作用经过 Tool Harness。
- [CURRENT] 文件工具只持有 `WorkspaceGuard`。
- [CURRENT] 模型请求不包含源仓库路径。
- [CURRENT] 外部副作用前后保存 tool intent/result journal。
- [CURRENT] 不确定的非幂等写操作不得自动重放。

### 4.3 工作区

- [TARGET] 用户源仓库不得成为 Agent 的写入目标。
- [CURRENT] 任务使用复制出的独立 workspace；文件工具路径被强约束；结束时验证 source fingerprint。
- [CURRENT] 可信 test/command profile 固定了可执行文件和资源策略；M4.1 将仓库测试代码放入 Linux rootless
  namespace，sandbox 不挂载原 source，只挂载可写任务 workspace，并用默认禁网和资源
  配额形成 OS 边界；M4.2 的 `run_command` 沿用同一边界并只接受结构化 argv。
- [CURRENT LIMIT] fingerprint 仍是审计不变量，不替代 OS boundary；M4.1 只支持 capability
  probe 成功的 Linux 环境，不宣称抵御内核/隔离运行时漏洞或跨平台等价。

### 4.4 状态和轨迹

- [CURRENT] 每个 session 的 SQLite event sequence 连续，session/checkpoint/event/message mutation 在短事务中提交。
- [CURRENT] SQLite 是 session、message、event 和 checkpoint 的恢复事实来源；`RuntimeSnapshot` 使用显式 JSON version。
- [CURRENT] JSONL 是可删除、可重建的导出投影，replay 不把它作为状态 authority。
- [CURRENT] M2 状态边界支持 interrupt/resume；SQLite lease 和调用 journal 决定恢复权限与动作。
- [CURRENT] M3 summary 是带 event range/hash、workspace revision 和 stale/supersede
  lineage 的 SQLite 派生缓存；原始 events 仍是 authority。

### 4.5 评测

- [CURRENT] Semantic golden 验证 Runtime 行为，不含随机字段。
- [CURRENT] Runtime `COMPLETED` 与 task success 分离；程序化 oracle 独立判断，失败
  run 保留在有效分母，eval infrastructure failure 单独统计。

## 5. 当前架构

```text
CLI
 │
 ▼
AgentApplication (composition root)
 │
 ├── ContextBuilder ── Passthrough / Budgeted ── optional CompressionEngine
 ├── ModelBackend ── Scripted/OpenAI-compatible/Anthropic/Fallback
 ├── ToolRegistry ── ToolHarness ── WorkspaceGuard
 │                         ├──────── Trusted TestProfile
 │                         ├──────── CommandProfile / structured argv
 │                         └──────── SandboxPolicy ── LinuxNamespaceExecutor
 ├── WorkspaceManager ───────────── isolated copied repo
 └── AgentRuntime ── TrajectoryRecorder ── SQLite Event Journal
                                      ├──── JSONL exporter
                                      └──── Replay / Semantic Golden
```

当前仍由一个进程驱动一个 live Session，但每个已提交 mutation 同步保存到 SQLite；M2
通过 lease、checkpoint 和调用 journal 支持状态边界跨进程 resume。`ScriptedBackend` 让决策完全可复现，因此 M1/M1.5 的失败能归因于 Runtime/Harness，而不是模型随机性。
M3 的 Eval Runner 在每个 case/repetition 使用新 SQLite、workspace 和 session；报告从
committed events 汇总调用、Token、延迟、失败、恢复、权限和 source invariant。

## 6. 目标架构

```text
CLI / Eval Runner
       │
       ▼
Application Use Cases
       │
       ├──── Session/Checkpoint Store ─── SQLite (authority)
       │                │
       │                └─────────────── JSONL exporter / Replay
       │
       ├──── Workspace Manager ───────── isolated task workspace
       │                                  └── M4 Sandbox Executor
       │
       └──── Agent Runtime (explicit FSM)
                    │
                    ├── Context Engine ─ recent/task/repo/summary
                    ├── ModelBackend ─── Scripted/OpenAI/Anthropic/Fallback
                    └── Tool Harness ─── registry/policy/timeout/journal
                                              │
                                              └── read/edit/test/approved tools

SQLite events ──→ Metrics / Eval Oracle / Failure Report / Trace Export
```

“窄腰”是 `ModelRequest/Response`、`ToolCall/Result`、`RuntimeSnapshot` 和 `Event`。Provider、工具和存储可以替换，但 Runtime 不应出现它们的实现细节。

## 7. 当前确定性数据流

1. CLI 解析 `agent_home`、source、task 和 script。
2. `AgentApplication` 检查 source 与 agent home 不重叠。
3. 创建 UUID session、计算 source fingerprint、组装依赖。
4. Runtime 将 `session_created` 和初始 user message 与 checkpoint 一起提交到 SQLite。
5. `WorkspaceManager` 复制 source，移除外部 symlink，初始化独立 Git baseline。
6. Context Engine 从 isolated workspace 生成 bounded repository snapshot，按固定 sections
   装配；高水位时通过 `ModelBackend` 生成经过验证的 summary。
7. Runtime 记录 context manifest 和 model call start，调用 `ScriptedBackend` 或真实 adapter。
8. 若模型返回 tool calls，Runtime 记录 assistant message 并进入工具调度。
9. Harness 依次执行 lookup、schema、permission、deadline、handler、错误映射和输出限制；
   `restricted_test` 和 `run_command` handler 额外通过 SandboxPolicy 和 SandboxExecutor
   执行固定 profile。`run_command` 的非零退出作为 observation，不等同于 Harness failure。
10. Runtime 记录 ToolResult，并在下一状态追加结构化 tool observation。
11. 若还有 pending call 继续调度；否则重新构建 context 并调用模型。
12. 模型返回非空 final text 后，Runtime 检查 source fingerprint，进入 `COMPLETED`。
13. 每个状态/消息/普通事件先以 journal mutation 提交；`run_finished` 记录状态和计数，Replay 从 SQLite 事件重新计算并交叉校验。

完整 M1 todo 场景为：read → first edit → test false → corrective edit → test true → final。

## 8. 状态机

### 8.1 当前状态

```text
CREATED
  → PREPARING_WORKSPACE
  → BUILDING_CONTEXT
  → CALLING_MODEL
       ├── final ─────────────────────────────→ COMPLETED
       ├── backend/invariant/budget failure ──→ FAILED
       └── tool calls
              ↓
       DISPATCHING_TOOL
              ↓
       RECORDING_OBSERVATION
              ├── more pending tools ─────────→ DISPATCHING_TOOL
              └── no pending tools ───────────→ BUILDING_CONTEXT
```

ToolResult 为 permission denied、timeout 或 execution error 时仍进入 observation；模型有机会改变策略。测试断言失败表达为 `status=success, passed=false`。

### 8.2 M2 状态扩展

- `INTERRUPTED`：已提交安全停止 checkpoint；
- `WAITING_APPROVAL`：存在不确定/高风险副作用，需要外部决定；
- `RETRY_WAIT`：持久化等待 retry 时间和目标状态。

状态扩展必须同步修改 enum、允许迁移表、handler、持久序列化、replay 和 golden migration。

## 9. Tool Harness

### 9.1 当前调用管线

```text
ToolCall
 → registry lookup
 → bounded JSON-schema validation
 → permission policy
 → handler deadline
 → handler
 → exception normalization
 → output-size enforcement
 → audit callback + Runtime events
 → ToolResult observation
```

当前权限：`READ`、`WRITE`、`EXECUTE_TEST`、`EXECUTE_COMMAND`。当前工具：`read_file`、
`edit_file`、`restricted_test`、`run_command`。

### 9.2 M2 恢复管线

M2 在 schema/permission 后增加 prepare/journal：

```text
validate → prepare → persist intent → mark running
         → external effect → persist result → append observation
```

读操作可重做；edit 通过 pre/post hash reconciliation；不确定非幂等操作进入 approval。不能用“参数相同”推断写操作幂等。

### 9.3 未来工具扩展原则

优先顺序：扩展现有工具 → 可信 profile/专用工具 → M5 经 eval 证明的能力。新增模型工具会增加每次模型请求的 schema 成本，因此必须有真实任务和 eval 证据。

## 10. Workspace 与安全边界

### 10.1 当前防护

- agent home/source 双向包含检查；
- 独立目录复制，不使用源仓库 Git worktree metadata；
- 忽略源 `.git`，副本初始化自己的 baseline；
- path `resolve()` 后做 root containment；
- 拒绝绝对路径、`..`、`.git` 和外部 symlink；
- 文件大小、工具输出和测试时间有上限；
- 测试只能选择应用注入的固定 profile。

### 10.2 M4.1/M4.2 当前边界

- Linux capability probe 失败时执行被拒绝，不回退宿主进程；其他平台同样 fail closed。
- namespace 中只挂载 session workspace；系统 rootfs 只读，临时目录和 workspace 受限。
- 默认无网络、环境 allowlist、no-new-privileges/drop capabilities，且有 wall/CPU/memory/
  PID/storage/output 限制与进程组清理。
- `run_command` 只接受 registry 中的 command profile、精确 executable allowlist、受限
  argv 和 workspace-relative cwd；shell executable、shell 字符串和未知参数不会进入 executor。
- profile 若要求网络、超出基线资源或显式 approval，会在当前没有授权通道时 fail closed；
  不把 approval 当作已实现的 UI/外部授权流程。
- 没有 syscall 过滤、OCI image lifecycle、SBOM/漏洞扫描，也不提供通用 Shell 或 approved
  network；这些属于后续范围/部署责任。

因此当前可准确称为“Linux namespace OS isolation foundation + 受限测试入口”，不能称为
完整生产安全 sandbox。M4 threat model 与验收见 [`m4-implementation-plan.md`](./m4-implementation-plan.md)。

## 11. 持久化与恢复 [M2 current]

M2.1 的 SQLite 使用 WAL、foreign keys、busy timeout 和 schema migrations。Session、checkpoint 与对应 event 必须在一个短事务中提交；外部调用永远不在事务内。RuntimeSnapshot 使用显式 JSON version，JSONL 只能作为 DB 导出。

M2.2 加入 tool intent/result journal、lease、interrupt/resume 和 reconciliation。恢复读取数据库事实，而不是根据最后一行日志猜测；不确定写操作进入 approval。JSONL 导出失败不影响已经提交的 Runtime 状态，并可以从 DB 重建。

详细 schema、事务和 crash window 见 [`m2-implementation-plan.md`](./m2-implementation-plan.md)。

## 12. Model Backend [M2]

Runtime 只依赖同步 `ModelBackend.complete(ModelRequest) -> ModelResponse`。M2 保持当前同步边界，先把持久状态和错误语义做正确；若未来改 async，必须作为单独架构变更评测。

Adapter 负责 provider JSON、tool protocol、usage 和 HTTP 错误归一化。Retry 由 Runtime 调度，fallback 只对 retryable infrastructure error 生效。质量差、认证失败和无效请求不能静默切换 provider。

Secrets 只在 adapter 请求边界存在，不写入 Session、DB、Event 或普通日志。

## 13. Context Engineering [M3 CURRENT]

目标上下文顺序稳定：

1. system policy/tool schemas；
2. task state 和预算；
3. repository snapshot/diff/last test；
4. structured summary；
5. recent raw messages/observations。

压缩由 token 高水位触发，不按固定消息条数。摘要必须保存 source event range/hash、workspace revision、目标、约束、修改、测试、错误和未完成项。文件变化后旧事实标 stale；实时工具结果优先。

当前实现使用 `ContextBuildInput`、`ContextSection`、`BuiltContext` 和
`TokenCounter`；model capability registry 选择上下文上限及 exact/命名 fallback counter。
压缩策略用 required-fact retention、task success 和 token reduction 做 A/B。M1 的
passthrough builder 保持为 baseline；摘要失败时 raw history 保留。详细契约和验收记录见
[`m3-implementation-plan.md`](./m3-implementation-plan.md)。

## 14. Eval Harness [M3 CURRENT]

EvalCase 由版本化 manifest、只读 fixture、task、policy、backend config、required facts 和 oracle 组成。每次 repetition 创建新 session/workspace。

Oracle 优先：测试命令、文件内容、Git diff/允许修改范围；LLM judge 只做开放式辅助。指标从持久事件计算，不从 Runtime 内存临时计数读取。

至少报告 task success、Runtime state、model/tool calls、tokens、run/model/tool/test/
compression latency、failure reason、recovery rate、source invariant 和越权尝试。所有
Agent 失败 run 留在有效分母并保留 trace；manifest/fixture/oracle 配置错误单独记为
`eval_infrastructure_failure`。`report.json` 的比较投影不含随机 session ID/trace path。

## 15. Observability

需要区分：

- Event：可版本化、可回放的业务事实；
- Log：面向调试，允许更详细但必须脱敏；
- Metric：从 Event 派生的聚合值；
- Trace export：便于人/外部工具消费的 JSONL。

不能把不可恢复的 debug log 当 event，也不能让 metrics 成为唯一事实来源。

## 16. 关键取舍

| 选择 | 收益 | 代价/后续 |
|---|---|---|
| 自研小 FSM | 状态和失败可解释 | 自己维护迁移/恢复契约 |
| 单 Agent | 行为可归因，成本低 | 只在 eval 证明瓶颈后考虑并行 |
| ScriptedBackend 基线 | 完全确定性 | 不代表真实模型质量 |
| SQLite 本地 authority | 部署简单、事务明确 | 不面向多节点高写入吞吐 |
| JSONL 导出 | 人可读、易归档 | 兼容 `JsonlEventStore` 仍有 O(n²)，主写路径已由 M2 移出 |
| 文件复制 workspace | 不写源 Git metadata | 大仓库慢；M4 用 overlay/container |
| 可信 test/command profile | M1/M4 可真实验证修改和结构化执行 | 不是完整 OS/OCI 安全产品 |
| 不提供通用 Shell | 降低模型可控风险面 | M4 仅解决结构化 profile 执行；通用 Shell 仍不在范围 |
| `COMPLETED != success` | 区分系统可靠性与任务质量 | M3 Eval oracle 已独立统计 |
| Golden 语义投影 | 稳定回归 | 协议变化需要显式版本迁移 |

## 17. 决策门禁

以下变更必须先更新设计再编码：

- 增加多 Agent；
- 增加通用 Shell 或网络访问；
- 改变 Event/Golden schema；
- 改变 source isolation threat model；
- 改变同步 ModelBackend 边界；
- 将 `COMPLETED` 当作 task success；
- 引入 Agent framework 或外部状态服务。
