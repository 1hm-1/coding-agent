# 模块设计

> 文档类型：代码边界与依赖规则  
> 当前基线：M5.1
> 下一变化：继续以 Eval failure coverage 决定后续 M5 能力

本文回答三个问题：功能应该放在哪个模块、模块之间允许传递什么、错误由谁处理。当前可执行契约见 [`contracts.md`](./contracts.md)，目标架构与安全边界见 [`architecture.md`](./architecture.md)。

## 1. 设计原则

- Domain 类型不依赖 CLI、数据库、Provider SDK 或具体工具。
- Runtime 只编排状态，不直接访问文件、进程、网络和数据库实现。
- Application 是 composition root，负责组装依赖和用例级前置检查。
- Tool Harness 是所有工具副作用的唯一入口。
- WorkspaceGuard 是文件路径授权边界，不只是便利函数。
- Trajectory/Persistence 保存事实；Replay 只读事实，不重新执行副作用。
- Provider 差异只存在于 adapter，不能扩散进 Runtime。
- 新抽象必须至少有一个当前消费者或明确属于正在实施的里程碑。

## 2. 当前依赖方向

```text
cli
 └── application
      ├── runtime ──────── domain
      │    ├── context ─── domain
      │    ├── models ──── domain
      │    ├── tools ───── domain + workspace
      │    └── trajectory ─ domain + persistence protocol
      ├── workspace
      ├── test_profiles
      ├── sandbox ──────── sandbox protocol + policy + Linux backend
      └── persistence ─── domain + migrations

tests → public modules above
```

依赖箭头必须保持向内：具体入口依赖稳定协议，稳定协议不反向依赖具体入口。例如 `domain.py` 不得 import `application.py`，`runtime.py` 不得 import 某个真实 Provider SDK。

## 3. 当前模块职责

| 模块 | 当前职责 | 不应承担 |
|---|---|---|
| `domain.py` | 状态、消息、模型请求/响应、工具调用/结果、Session、RuntimeSnapshot、Context/Summary 值对象、错误类型 | I/O、环境变量、SQL、CLI 格式化 |
| `runtime.py` | FSM、预算检查、模型/工具编排、observation、终态 | 文件编辑、启动子进程、Provider 分支 |
| `application.py` | 创建 session、检查路径、打开 SQLite journal、组装 backend/tools/workspace/recorder | 状态迁移细节、工具实现 |
| `context.py` | TokenCounter、model capability registry、section budget、passthrough/budgeted context assembly | 持久化、模型调用、文件写入 |
| `models/base.py` | 稳定 `ModelBackend` 协议 | Provider SDK 类型 |
| `models/scripted.py` | 按脚本返回确定性响应、校验脚本消费、恢复 response ordinal | 网络和 provider retry |
| `models/openai_compatible.py` | OpenAI-compatible messages/tools/usage/HTTP 映射 | Runtime 状态、retry 循环 |
| `models/anthropic.py` | Anthropic system/content blocks/usage/HTTP 映射 | Runtime 状态、retry 循环 |
| `models/fallback.py` | retryable failure 后选择下一个 backend | 自行循环 retry、任务质量判断 |
| `models/errors.py` | HTTP status、Retry-After 和安全错误文本分类 | 状态迁移、持久化 |
| `models/retry.py` | 有界 exponential backoff 计算 | 睡眠、状态迁移 |
| `tools/base.py` | 工具定义、上下文、结果协议和 registry | 具体授权策略和业务编排 |
| `tools/harness.py` | lookup、schema、权限、prepare/hash、deadline、错误归一化、输出限制、审计 | 模型决策、workspace 创建 |
| `tools/builtin.py` | `read_file`、`search_files`、`edit_file`、`restricted_test`、`run_command` handler；将可信 profile/结构化 argv 交给 SandboxPolicy/Executor | 通用 Shell、源仓库直接访问、启动 host subprocess |
| `test_profiles.py` | 应用可信测试 profile，固定 argv/cwd/env/network/limits/image | 接收模型传入的任意命令 |
| `command_profiles.py` | 可信 command profile、executable allowlist、argv/cwd 上限和 elevated-resource admission metadata | shell parsing、执行进程、直接授权模型命令 |
| `sandbox/base.py` | `ExecutionSpec`、`ExecutionResult`、`ResourceLimits`、capability protocol | 启动进程、路径策略、provider 格式 |
| `sandbox/policy.py` | fail-closed capability、identity、workspace、argv、environment 和 network admission | 实际 mount/process 生命周期 |
| `sandbox/local_container.py` | Linux rootless namespace executor、capability probe、result normalization、cleanup metadata | 模型决策、工具 schema、通用 shell |
| `sandbox/runner.py` | namespace 内私有 rootfs、mount、limits、直接 argv、进程树监控/清理 | 被应用直接 import；不能成为通用命令入口 |
| `workspace.py` | 创建隔离副本、路径防逃逸、fingerprint、Git baseline | 判断任务是否修复成功 |
| `trajectory.py` | 兼容 JSONL store、record、replay、semantic projection | 恢复执行、再次调用工具、SQLite SQL |
| `persistence.py` | SQLite schema v3、snapshot/message/event/checkpoint、model/tool journal、summary、lease 原子 mutation | 模型/工具调用、状态迁移决策、JSONL 格式化 |
| `migrations.py` | 有序、幂等、未知未来版本拒绝的 schema migration | session 业务状态、运行时编排 |
| `export.py` | 已提交 DB events 到 JSONL 的原子 projection 导出 | 状态决策、replay 规则 |
| `compression.py` | 摘要模型调用边界、event lineage、schema/required-fact 验证、stale 判定 | 权限判定、Runtime 状态迁移、覆盖原始事件 |
| `evaluation.py` | versioned suite、containment、trusted oracle、fresh run、metrics、A/B report | 改变 Runtime 完成语义、绕过 Harness、生产流量实验 |
| `cli.py` | 参数解析、run/resume/interrupt/resolve/inspect/evaluate、打印结构化结果 | 创建隐藏的运行时全局状态 |

## 4. 核心接口边界

以下是语义接口，不要求实现逐字一致；修改签名时必须保持职责不变并更新契约测试。

### 4.1 Runtime

```python
class AgentRuntime:
    def initialize(self) -> None: ...
    def resume(self) -> None: ...
    def step(self) -> RuntimeState: ...
    def run(self) -> RunResult: ...
```

- `initialize()` 记录会话开始事实，不执行完整任务。
- `step()` 只运行当前状态对应的一个 handler，并返回新状态。
- `run()` 驱动 `step()` 直到终态或预算耗尽。
- `resume()` 只消费已持久化 checkpoint/call journal；不根据 JSONL 尾部猜测副作用。

### 4.2 Model

```python
class ModelBackend(Protocol):
    @property
    def name(self) -> str: ...

    def complete(self, request: ModelRequest) -> ModelResponse: ...
```

`ModelRequest` 只包含模型所需的消息和工具 schema，不包含可以绕过 Harness 的对象引用。`ModelResponse` 必须满足 final text 与 tool calls 的响应不变量；非法响应由 Runtime 分类为失败。

### 4.3 Context

```python
class ContextBuilder(Protocol):
    def build(self, request: ContextBuildInput) -> BuiltContext: ...

class TokenCounter(Protocol):
    def count_messages(self, provider: str, model: str, messages: Sequence[Message]) -> int: ...
```

`PassthroughContextBuilder` 返回完整历史，作为 M3 A/B baseline；`BudgetedContextBuilder`
使用 capability registry、命名 token counter 和固定 system/task-runtime/repository/
summary/recent sections。工具 schema 仍由 Runtime 在创建 `ModelRequest` 时从 Registry
取得，不由 ContextBuilder 注入。Context Engine 只构建 isolated workspace 的 bounded
snapshot，不建立绕过 ToolHarness 的读取路径。

### 4.4 Tool

```python
@dataclass(frozen=True)
class ToolDefinition:
    name: str
    description: str
    input_schema: JsonObject
    permission: Permission
    timeout_seconds: float
    recovery_mode: RecoveryMode

class ToolRegistry:
    def register(self, definition: ToolDefinition, handler: ToolHandler) -> None: ...

class ToolHarness:
    def execute(self, call: ToolCall, context: ToolContext) -> ToolResult: ...
```

`ToolHarness.execute()` 是工具执行唯一入口。Handler 接收已经过 schema/permission 检查的参数，仍必须使用 `WorkspaceGuard` 解析路径。Handler 异常不能穿透到 CLI；Harness 将其归一化为结构化失败。

### 4.5 Workspace

```python
class WorkspaceManager:
    def create(self, source: str | Path, session_id: str) -> WorkspaceManifest: ...
    def get(self, session_id: str) -> WorkspaceGuard: ...

class WorkspaceGuard:
    def resolve(self, relative_path: str, *, must_exist: bool = True) -> Path: ...
    def relative(self, path: Path) -> str: ...
```

Manager 管生命周期，Guard 管单次路径授权。不要把 raw source path 注入工具上下文；工具只应看到 isolated workspace。

### 4.6 Event 与 Replay

```python
class EventStore(Protocol):
    def append(self, event: Event) -> None: ...
    def load(self, session_id: str) -> list[Event]: ...
    def trace_path(self, session_id: str) -> Path: ...

class TrajectoryRecorder:
    def emit(
        self,
        event_type: EventType,
        state: RuntimeState,
        payload: JsonObject | None = None,
    ) -> Event: ...

def replay(events: list[Event]) -> ReplayResult: ...
```

Replay 必须是纯读取与聚合：不得创建 workspace、调用 model 或执行 tool。M2
后 SQLite 是唯一事实保存点；JSONL 只由 committed DB events 导出，不能成为第二个
state authority。

### 4.7 Application

```python
class AgentApplication:
    def run_task(
        self,
        *,
        source: str | Path,
        task: str,
        backend: ModelBackend,
        policy: RunPolicy | None = None,
        session_id: str | None = None,
    ) -> RunResult: ...

    def replay_session(self, session_id: str) -> ReplayResult: ...
```

调用者通过 Application 开始任务、resume、interrupt、resolve、inspect、replay 或 export，
不直接实例化一半依赖。CLI 不直接更新 SQLite。

## 5. 调用所有权和失败所有权

| 场景 | 谁发起 | 谁分类/处理 | 谁记录 |
|---|---|---|---|
| 建立 workspace | Application/Runtime 状态 handler | WorkspaceManager；失败进入 Runtime failure | Runtime trajectory |
| 构建模型请求 | Runtime | ContextBuilder；协议错误由 Runtime 失败 | Runtime trajectory |
| 调用 backend | Runtime | adapter 分类错误；Runtime 调度 retry/fallback | Runtime/M2 journal |
| 执行 tool | Runtime | ToolHarness 返回 `ToolResult` | Harness audit + Runtime event |
| 测试断言失败 | restricted_test handler | 返回成功执行且 `passed=false`，由模型决定下一步 | Tool result/observation |
| 路径/权限/超时错误 | ToolHarness/Guard | 结构化 tool failure，不让进程崩溃 | Tool result/observation |
| 状态迁移非法 | StateMachine | Runtime invariant failure | failure + terminal events |
| task 是否成功 | M3 Eval oracle | Eval runner，不由 Runtime final text决定 | eval result |

一个错误只能有一个主要分类所有者。调用者可以决定下一状态，但不应重新用字符串猜测错误种类。

## 6. M2 模块变化

### 6.1 M2.1：持久化基础

新增或演进：

```text
domain.py                 + RuntimeSnapshot/serialization 边界（最小所需）
persistence.py            + RunJournal protocol + SQLite implementation
migrations.py             + ordered schema migrations（可先内聚于 persistence）
export.py                 + committed events → JSONL
application.py            + 打开 DB、执行迁移、注入 journal
trajectory.py             + replay 保留，JSONL 改为 projection
```

规则：

- SQLite 是恢复事实来源；JSONL 不参与状态决策。
- session/checkpoint/event 需要原子提交。
- 外部调用不在数据库事务内。
- sequence 由事务内 session 计数器分配。
- M2.1 的 v1 迁移是 M2 的持久基础；M2.2/M2.3 在其上增量扩展。

### 6.2 M2.2：中断与恢复（已实现）

新增：调用 journal、显式 `RuntimeSnapshot`、`INTERRUPTED`/`WAITING_APPROVAL`、resume use case 和副作用 reconciliation；lease 和 resolution 写入也由 Application use case 负责。

建议 Application 暴露不同用例，而不是让 CLI 手工拼 Runtime：

```python
run_task(...)
interrupt_session(session_id)
resume_session(session_id, ...)
show_session(session_id)
```

恢复逻辑根据 checkpoint 和 call journal 重建内存对象。不要根据 JSONL 尾部、日志字符串或 workspace 当前内容单独猜测。

### 6.3 M2.3：真实模型（已实现）

新增：

```text
models/openai_compatible.py
models/anthropic.py
models/errors.py
models/retry.py
models/fallback.py
```

Adapter 将 Provider 响应映射为 domain 类型；retry policy 只读统一错误分类。Runtime 不 import SDK，也不访问 API key。Live smoke 测试必须显式 opt-in；本次无 provider 凭据，默认离线 contract tests 通过。

## 7. M3 已实现与 M4 扩展点

### M3 Context 与 Eval（已实现）

- `context.py`：token 预算、capability、section allocation 和 repository assembly；
- `compression.py`：结构化摘要、source lineage、required facts 和 stale invalidation；
- `evaluation.py`：版本化任务定义、test/file/diff/result oracle、批量执行和指标汇总。

是否从单文件 `context.py` 拆 package，以代码规模和职责数量决定；不要仅为了目录美观迁移。

### M4 隔离

- `sandbox/base.py`：执行请求与结果协议；
- `sandbox/policy.py`：能力、身份、路径、环境和默认禁网的 fail-closed admission；
- `sandbox/local_container.py`：Linux rootless namespace、只读 rootfs、workspace mount、
  配额和 cleanup，缺能力不回退 host process；
- `sandbox/runner.py`：只由 backend 在 namespace 内调用的私有 runner；
- `tools/builtin.py` 的 test/command handler 仍通过 Harness，先记录 intent/running，再
  执行并把 normalized result 作为 observation；
- `command_profiles.py` 只允许可信 executable allowlist 和固定 profile 策略；网络/扩容
  profile 没有 approval 时 fail closed。
- `run_command` 只能沿用结构化 argv、命令策略和同一 sandbox 边界，不能退化成 shell。

## 8. 新代码放置决策表

| 需求 | 放置位置 |
|---|---|
| 新状态/跨层值对象 | `domain.py` |
| 新状态 handler 或预算规则 | `runtime.py` |
| 新 CLI 子命令 | `cli.py`，业务组装在 `application.py` |
| Provider 特有字段/SDK | `models/<provider>.py` |
| 工具通用校验/超时 | `tools/harness.py` |
| 单个工具行为 | `tools/builtin.py` 或规模足够后独立文件 |
| 路径 containment | `workspace.py`，不要复制到 handler |
| SQL/事务/迁移 | `persistence.py`/`migrations.py` |
| JSONL 导出 | `export.py`，不进入 Runtime |
| 指标/任务 oracle | `evaluation.py`，不进入 Runtime terminal 判断 |
| Sandbox execution contract/policy | `sandbox/base.py`、`sandbox/policy.py`；backend 细节留在 `sandbox/local_container.py` |

## 9. 模块变更完成检查

- 边界类型是否仍可 JSON 序列化并有 schema/version 语义？
- 是否新增了绕过 Tool Harness 或 WorkspaceGuard 的副作用路径？
- 是否把 Provider/CLI/SQL 细节泄漏到 Runtime？
- 失败是否有结构化分类、事件和测试？
- OS boundary 是否 capability-probed、fail-closed，且没有 host subprocess fallback？
- 是否更新 [`contracts.md`](./contracts.md)、[`current-state.md`](./current-state.md) 和对应里程碑 checklist？
- 全量测试、golden、两个 smoke 是否仍通过？
