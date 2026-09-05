# M1：确定性 Coding Loop 实施说明

## 1. 实施目标

M1 只证明一条完整、可重复验收的纵向链路：

```text
task submitted
  → isolated workspace created
  → ScriptedBackend requests read_file
  → observation recorded
  → ScriptedBackend requests edit_file
  → observation recorded
  → ScriptedBackend requests restricted_test
  → test result recorded
  → ScriptedBackend returns final answer
  → run completed with replayable trajectory
```

这一里程碑验证 Runtime 与副作用边界是否成立，不追求工具数量和模型能力。M1 不实现通用 Shell、真实模型调用、checkpoint resume、上下文压缩或批量评测。

## 2. Implementation checklist

### 项目骨架

- [x] 建立无第三方运行时依赖的 Python 包与 CLI 入口。
- [x] 提供一个不依赖网络的 scripted run 示例。
- [x] 所有运行数据写入显式 `agent_home`，不污染源仓库。

### FSM Runtime

- [x] 定义 `CREATED`、`PREPARING_WORKSPACE`、`BUILDING_CONTEXT`、`CALLING_MODEL`、`DISPATCHING_TOOL`、`RECORDING_OBSERVATION`、`COMPLETED`、`FAILED` 状态。
- [x] 用允许迁移表和每状态 handler 表达控制流。
- [x] `step()` 每次只推进一个状态动作，`run()` 只是终态前的驱动器。
- [x] 对最大 step、模型调用数和工具调用数设置预算。
- [x] 非法迁移和预算耗尽产生结构化失败事件。

### ScriptedBackend

- [x] 消费固定 JSON response 序列。
- [x] 统一返回 final text、tool calls、usage 和 finish reason。
- [x] 请求耗尽或脚本无效时返回分类错误。
- [x] 保存接收到的请求，供 contract test 验证 observation 是否正确回填。

### Workspace isolation

- [x] 每个 session 创建唯一 workspace 副本。
- [x] 复制时忽略源 `.git` 和运行产物，在副本内初始化独立 Git baseline。
- [x] 源仓库路径不暴露给 backend 或工具 handler。
- [x] 所有文件路径经过 `WorkspaceGuard`，拒绝绝对路径、`..` 和外部 symlink。
- [x] E2E 比较运行前后源仓库 fingerprint，证明原仓库未改变。

### Tool Harness

- [x] 工具通过 `ToolRegistry` 显式注册，重名和非法 schema 启动失败。
- [x] Harness 统一执行 schema validation、permission、timeout、异常映射、输出限制和审计日志。
- [x] 只注册 `read_file`、`edit_file`、`restricted_test`。
- [x] `edit_file` 只允许唯一精确替换，并使用同目录临时文件原子落盘。
- [x] `restricted_test` 只接受预先注册的 test profile 名称，不接受任意 argv 或命令字符串。
- [x] 超时后终止测试进程组，返回结构化结果。

### Event trajectory

- [x] 每个 session 输出 schema-versioned JSONL trace。
- [x] 记录 session、状态迁移、workspace、model call、tool call/result、message 和 run completion/failure。
- [x] event sequence 单调连续。
- [x] `replay()` 校验 sequence 与状态迁移，并重建最终状态和调用统计。

### Tests

- [x] FSM transition unit tests。
- [x] schema、permission、路径逃逸和非唯一编辑负例。
- [x] restricted test profile/timeout tests。
- [x] trajectory replay consistency test。
- [x] 完整 read-edit-test-final E2E。

## 3. M1 模块变化

| 模块 | M1 实现 | 延后但保留的接口边界 |
|---|---|---|
| `domain.py` | FSM、Message、ToolCall/Result、ModelRequest/Response、Event、RunPolicy | checkpoint 字段和恢复策略 |
| `runtime.py` | 显式 handler map、预算、确定性 tool-observation loop | retry、interrupt/resume、approval |
| `models/base.py` | `ModelBackend` protocol | 真实 provider adapters |
| `models/scripted.py` | JSON response 序列和故障注入 | provider fallback |
| `workspace.py` | 隔离复制、路径 guard、source/workspace fingerprint | container/overlay workspace |
| `tools/base.py` | definition、registry、受限 JSON Schema | plugin discovery |
| `tools/harness.py` | permission、cooperative timeout、错误和审计边界 | approval provider、持久 tool journal |
| `tools/builtin.py` | read/edit/restricted-test | search、git inspection、通用 shell |
| `test_profiles.py` | profile 名称到固定 argv 的映射 | 项目级可信配置加载 |
| `trajectory.py` | JSONL append、load、replay、metrics | SQLite event store、OpenTelemetry sink |
| `application.py` | composition root 和 run use case | session repository、resume use case |
| `cli.py` | scripted run 与 replay | live model、eval batch commands |
| `persistence.py` | 不在 M1 实现 | checkpoint/session SQLite store |
| `context.py` | 仅实现 `ContextBuilder` seam 与 passthrough builder | token budget、压缩和摘要 lineage |
| `evals/` | 不在 M1 实现；E2E fixture 作为基线 | manifest、oracle、batch/A-B report |

### M1 依赖图

```text
CLI → Application → Runtime → ModelBackend
                         ├──→ ToolHarness → WorkspaceGuard
                         │              └→ RestrictedTestProfiles
                         └──→ TrajectoryWriter
```

Runtime 只接触抽象协议。以后把 JSONL writer 换成 SQLite-backed event store、把 ScriptedBackend 换成真实 adapter，不改变 FSM 的模型/工具循环语义。

## 4. 受限测试执行设计

模型只能提交：

```json
{"profile": "python_unittest"}
```

应用启动时由可信代码注入 profile：

```python
TestProfile(
    name="python_unittest",
    argv=("python3", "-m", "unittest", "discover", "-v"),
    timeout_seconds=30,
)
```

模型不能控制 executable、环境变量或 Shell 语法。执行器使用：

- `shell=False` / argv 模式；
- `cwd=workspace`；
- 最小环境变量；
- 独立进程组；
- wall-clock timeout；
- timeout 后 `SIGTERM`，短暂等待后 `SIGKILL`；
- stdout/stderr 上限。

这避免 M1 演化成通用 OS command executor。它提供对“Agent 修改后是否通过测试”的真实反馈，但不是恶意仓库代码的 OS 级安全沙箱；容器、网络隔离和资源配额属于 M4。

## 5. Future extension contracts

M1 只保留下列窄接口，不创建无消费者的框架代码：

```python
class ModelBackend(Protocol):
    def complete(self, request: ModelRequest) -> ModelResponse: ...

class EventStore(Protocol):
    def append(self, event: Event) -> None: ...
    def load(self, session_id: str) -> list[Event]: ...

class ContextBuilder(Protocol):
    def build(self, task: str, messages: Sequence[Message]) -> Sequence[Message]: ...
```

- checkpoint recovery：未来的 SQLite `SessionStore` 实现事件和 checkpoint 的事务提交。
- real model adapters：实现同一个 `ModelBackend`，provider 细节不进入 Runtime。
- context engine：替换 M1 的 passthrough builder，FSM 调用位置不变。
- eval harness：直接使用 Application 创建隔离 run，并从 EventStore 计算指标。

## 6. First milestone acceptance criteria

以下条件必须同时成立：

1. `PYTHONPATH=src python3 -m unittest discover -v` 全部通过。
2. 示例任务从源 fixture 创建隔离副本，完成 read-edit-test-final，最终状态为 `COMPLETED`。
3. fixture 源目录的内容 fingerprint 在运行前后完全相同。
4. workspace 中目标修改存在，restricted test 返回 `passed=true`。
5. 模型第二次及后续请求包含前一个工具的结构化 observation。
6. trace 包含连续 sequence，并能 replay 出相同最终状态、模型调用数和工具调用数。
7. 绝对路径、`..`、外部 symlink、未知字段和缺失权限全部被拒绝。
8. Runtime 注册表中只有三个工具，不存在 `run_shell`。
9. 测试工具不接受 argv、command 或 executable 参数。
10. README 明确 M1 已实现内容与 checkpoint/context/eval/真实 adapter 尚未实现的边界。
