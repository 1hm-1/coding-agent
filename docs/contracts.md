# 当前运行契约

> 适用基线：M4.2。本文描述当前代码；M5 能力扩展仍是条件阶段。

## 1. Runtime 状态契约

当前合法迁移：

| From | To |
|---|---|
| `CREATED` | `PREPARING_WORKSPACE`、`INTERRUPTED`、`FAILED` |
| `PREPARING_WORKSPACE` | `BUILDING_CONTEXT`、`INTERRUPTED`、`FAILED` |
| `BUILDING_CONTEXT` | `CALLING_MODEL`、`INTERRUPTED`、`FAILED` |
| `CALLING_MODEL` | `DISPATCHING_TOOL`、`COMPLETED`、`RETRY_WAIT`、`INTERRUPTED`、`FAILED` |
| `DISPATCHING_TOOL` | `RECORDING_OBSERVATION`、`WAITING_APPROVAL`、`INTERRUPTED`、`FAILED` |
| `RECORDING_OBSERVATION` | `DISPATCHING_TOOL`、`BUILDING_CONTEXT`、`INTERRUPTED`、`FAILED` |
| `INTERRUPTED` | `resume_target_state`、`RETRY_WAIT`、`FAILED` |
| `WAITING_APPROVAL` | `DISPATCHING_TOOL`、`RECORDING_OBSERVATION`、`FAILED` |
| `RETRY_WAIT` | `CALLING_MODEL`、`INTERRUPTED`、`FAILED` |
| `COMPLETED` | 无 |
| `FAILED` | 无 |

`COMPLETED` 和 `FAILED` 是终态。`INTERRUPTED` 是已持久化的安全边界，
`WAITING_APPROVAL` 需要显式 resolution，`RETRY_WAIT` 保存 retry 时间；三者都不能被当作完成。

`step()` 的契约：

- 终态调用不产生新事件；
- 非终态调用最多执行当前 handler 的一个状态动作；
- step 预算在 handler 前检查；
- 非法迁移抛出 `InvariantViolation`；
- `run()` 捕获未分类异常，尽量转换为 `runtime_exception` 失败轨迹。

## 2. Message 契约

| role | content | 关键 metadata |
|---|---|---|
| `user` | 原始任务文本 | 无 |
| `assistant` | 模型文本，可为空 | `tool_calls`、`finish_reason`、`usage` |
| `tool` | JSON 序列化的完整 `ToolResult` | `tool_call_id` 必须对应调用 |
| `system` | 稳定 M1 policy | 只存在于构建后的 ModelRequest，不写入 Session.messages |

每次工具结果必须先记录 `tool_call_finished`，再在 `RECORDING_OBSERVATION` 中追加 tool message。模型的下一次请求必须看到该 observation。

## 3. ModelBackend 契约

```python
class ModelBackend(Protocol):
    @property
    def name(self) -> str: ...

    def complete(self, request: ModelRequest) -> ModelResponse: ...
```

`ModelRequest` 不得包含源仓库路径或 API key。当前 metadata 只含 `session_id`。

`ModelResponse` 必须满足：

- tool call id 在整个 session 内唯一；
- tool call arguments 是 JSON object；
- 没有 tool call 时必须有非空 final text；
- usage 缺失时按零处理；
- usage 中已出现的 token 字段必须是非负整数；`null`、布尔值、负数、浮点数或字符串均为
  `protocol_error`；
- provider 特有字段只能放入 `provider_metadata`。

Backend 协议错误应抛 `BackendError(kind=...)`，不能把错误伪装成 final answer。当前
`timeout`、`rate_limit` 和 `provider_unavailable` 可重试，`authentication`、
`invalid_request`、`protocol_error` 和 `content_blocked` 不可重试。`retry_after` 只作为
持久 retry policy 的输入。

## 4. Context 契约

`ContextBuildInput` 是 Context Engine 的唯一输入，包含 task、Runtime state、policy、
isolated repository snapshot、latest validated summary、provider/model 和 pending/active
call。`ContextBuilder.build()` 返回 `BuiltContext`；Runtime 只消费其 `messages`，并把
`manifest()` 放入 `context_built` event。

`BudgetedContextBuilder` 固定按以下顺序装配：

```text
system → task_runtime → repository → summary → recent
```

input budget 为 model capability context limit 减 reserved output、provider protocol
margin 和可配置 margin。`counter` 明确为 `provider_exact` 或 `named_estimator`；未知模型
没有显式 fallback 时失败为 `unknown_model_capability`。system、task/runtime、repository、
pending/active call、最近 tool result 和最近 test result 是硬保留内容；硬内容无法适配时
失败为 `context_required_content_exceeds_budget`。

Repository snapshot 只从 isolated workspace 生成，最多包含配置上限的文件路径、Git diff
summary、已读文件 content hash、workspace revision 和最近测试事实；它不提供绕过
ToolHarness 的源文件读取能力。

## 5. Compression 与 Summary 契约

只有 unbounded assembled input 超过 `high_watermark_ratio * input_budget` 才触发摘要调用，
默认高水位为 `0.85`，目标为 `0.65`。每次 Runtime 最多使用配置的 compression call budget。
摘要模型仍实现 `ModelBackend`，不能返回 tool call。

`SummaryRecord` 是 SQLite 中的派生缓存，不是 checkpoint 或权限依据。它必须包含
`schema_version=1`、session、source event start/end/hash、workspace revision、goals、
constraints、decisions、files_read、edits、tests、errors、unresolved 和创建时间；摘要
通过 required-fact verifier 后才可被下一个 context manifest 引用。摘要失败记录
`compression_rejected`，保留 raw messages/events；文件 hash 或 workspace revision 变化时
记录 `summary_invalidated`，旧摘要可通过 `superseded_by` 链失效。

## 6. Tool 契约

### Definition

```python
ToolDefinition(
    name: str,
    description: str,
    input_schema: dict,
    permission: Permission,
    timeout_seconds: float,
    recovery_mode: RecoveryMode,
    timeout_enforcement: Literal["process", "sandbox"],
)
```

Schema 根必须是 object，且 `additionalProperties` 必须为 `false`。当前 validator 支持 object、array、string、integer、number、boolean、enum、required、长度/数量和数值边界的有限子集。

### 执行顺序

```text
lookup → schema → permission → deadline → process/sandbox termination boundary → handler
       → exception mapping → output limit → audit finish
```

普通 handler 默认在独立 Linux 进程组中执行；deadline 到达后 Harness 以 `SIGKILL` 终止整个
worker 进程组并等待回收。`restricted_test` 和 `run_command` 显式使用 `sandbox` 模式，由
SandboxExecutor 的 wall timeout 与进程组清理提供可终止边界。无法建立所声明边界时 fail closed，
不退回无强制终止能力的同步调用。

### ToolResult

| 字段 | 语义 |
|---|---|
| `status` | Harness/工具执行状态 |
| `data` | JSON object 结构化结果 |
| `error` | `{kind,message}` 或 `null` |
| `duration_ms` | 单次工具执行耗时 |
| `truncated` | 输出是否被截断 |

当前 status：`success`、`invalid_arguments`、`permission_denied`、`timeout`、`not_found`、`execution_error`。

### 四个工具

`read_file`：

```json
{"path":"relative/path.py","start_line":1,"end_line":100}
```

- `path` 必填；行号从 1 开始；文件上限 2 MiB。

`edit_file`：

```json
{"path":"relative/path.py","old_text":"exact old","new_text":"exact new"}
```

- 三字段必填；`old_text` 必须恰好出现一次；目标必须是非 symlink 普通文件；使用同目录临时文件和 `os.replace()` 原子更新。

`restricted_test`：

```json
{"profile":"python_unittest"}
```

- 模型不能提交 argv、command、cwd 或 environment；
- `status=success,data.passed=false` 表示测试正常执行但断言失败；
- `status=timeout` 表示测试基础设施超时；
- timeout/resource limit 使用 sandbox executor 的进程组 KILL，并等待 namespace runner
  回收；测试断言失败仍保持 `status=success,data.passed=false`。
- handler 只把可信 profile 转换为 `ExecutionSpec`；不会把模型提交的 command、argv、cwd
  或环境变量传给执行器。

`run_command`：

```json
{
  "profile": "python_project",
  "argv": ["python3", "-m", "compileall", "src"],
  "cwd": "."
}
```

- `profile` 来自可信 `CommandProfileRegistry`；profile 固定 executable allowlist、image、
  environment、network、limits 和默认 cwd。
- `argv` 必填，最多 32 项，每项最多 4096 字符；executable 必须精确命中 profile
  allowlist。`cwd` 可选，必须是最多 256 字符的 workspace 内相对目录。
- 不接受 `command`、shell interpreter 或 shell parsing；argv 直接进入 `ExecutionSpec`。
- `status=success,data.command_succeeded=false` 表示命令正常退出但 exit code 非零；
  timeout/resource/sandbox failure 使用同一结构化结果映射。
- 使用 `EXECUTE_COMMAND` permission 和 `NON_IDEMPOTENT` recovery；RUNNING crash 恢复时
  进入 `WAITING_APPROVAL`，不会自动重跑未知副作用。
- network 或超过默认 command limits 的 profile 没有显式一次性 approval 时以
  `permission_denied/command_approval_required` fail closed；当前没有默认网络或 approval UI。

### Sandbox Executor（M4.1/M4.2）

窄接口由 `coding_agent.sandbox` 提供：

```python
@dataclass(frozen=True)
class ResourceLimits:
    wall_seconds: float
    cpu_seconds: float
    memory_bytes: int
    writable_bytes: int
    pids: int
    stdout_bytes: int
    stderr_bytes: int

@dataclass(frozen=True)
class ExecutionSpec:
    argv: tuple[str, ...]
    workspace: Path
    working_directory: str
    environment: Mapping[str, str]
    network: Literal["none", "approved"]
    limits: ResourceLimits
    profile_name: str

class SandboxExecutor(Protocol):
    def capabilities(self) -> SandboxCapabilities: ...
    def execute(self, spec: ExecutionSpec) -> ExecutionResult: ...
```

`ExecutionResult.status` 只有 `exited`、`timeout`、`resource_exhausted`、`sandbox_error`。
`SandboxPolicy` 要求 capability probe 的 mount/PID/network namespace、chroot/read-only
rootfs、workspace bind、环境 allowlist、process/output/resource limits 全部存在；默认
拒绝 network 和未 pin 的 base identity。`ExecutionSpec.to_dict()` 只保存环境 key，不保存
环境值或私有 host workspace path；完整 wire payload 只在父进程到私有 runner 的 stdin
边界内使用。

当前 Linux backend 使用 rootless user/mount/PID/network namespace、最小 chroot rootfs、
只读 system mounts、受控 tmpfs、no-new-privileges 和 capabilities drop。没有能力时返回
结构化 `sandbox_capability_unavailable`，不回退到宿主 `subprocess`。native rootfs 的
兼容字段名仍为 `image_digest`，但 native backend 的 `identity_kind` 是
`native_runtime_sample_fingerprint`：它只覆盖内核标识和声明的关键运行时文件，不是完整
rootfs 内容摘要，也不是 OCI image 生命周期承诺。

## 7. Permission 契约

| Permission | 当前工具 |
|---|---|
| `READ` | `read_file` |
| `WRITE` | `edit_file` |
| `EXECUTE_TEST` | `restricted_test` |
| `EXECUTE_COMMAND` | `run_command` |

未授权工具不会出现在 ModelRequest tool schema 中。即使模型仍提交该工具名，Harness 也必须再次执行权限检查，形成 `permission_denied` observation。

## 8. Workspace 契约

- Source 与 agent home 不能互相包含。
- Session id 只能包含字母、数字、`-`、`_`。
- 工具只能获得 `WorkspaceGuard`，不能获得 source path。
- `resolve()` 拒绝绝对路径、父级穿越、`.git` 元数据和解析后越界。
- 外部 symlink 在复制阶段删除；运行中创建的外部 symlink在访问阶段拒绝。
- 当前 fingerprint 忽略 `.git`，按路径和文件内容计算。

## 9. Event 契约

通用 envelope：

```json
{
  "schema_version": 1,
  "event_id": "uuid",
  "session_id": "uuid",
  "sequence": 1,
  "event_type": "session_created",
  "timestamp": "ISO-8601 UTC",
  "state": "created",
  "payload": {}
}
```

当前事件：

| Event | 触发时机 | 必要 payload |
|---|---|---|
| `session_created` | 新 session 初始化 | task、source_name、backend |
| `message_added` | 消息写入内存 session | message、message_index |
| `state_transition` | 合法状态迁移 | from、to、reason |
| `workspace_created` | 隔离复制完成 | workspace_path、removed symlinks |
| `context_built` | ModelRequest messages 组装完 | message_count、估算 token、compressed |
| `model_call_started` | backend 调用前 | request_id、backend、计数 |
| `model_call_succeeded` | 有效 response 返回 | finish_reason、tool count、usage |
| `model_call_failed` | 分类 BackendError | request_id、kind、message |
| `model_call_uncertain` | 恢复时发现 RUNNING 无 response | request_id、attempt、kind |
| `tool_call_started` | M1 兼容 JSONL store 的 Harness 前 | call、ordinal |
| `tool_call_prepared` | 工具 intent/hash 已准备 | call、ordinal、recovery_mode、pre/post revision |
| `tool_call_running` | 外部工具调用前或恢复重试前 | call、ordinal、attempt、recovery；sandbox tools 另含 execution_id |
| `tool_call_finished` | Harness 后 | 完整 result；sandbox tool result 含 status/limits/identity/cleanup |
| `tool_call_uncertain` | hash reconciliation 无法确认副作用 | call_id、recovery_mode、current_revision |
| `tool_result_reattached` | 只补回已保存 result | call_id、recovered |
| `approval_requested` | 不确定副作用暂停 | call_id、reason |
| `call_resolved` | 操作者写入 resolution 事实 | call_id、resolution、actor、reason |
| `retry_scheduled` | retry 等待已持久化 | kind、retry_count、next_retry_at、backend |
| `fallback_selected` | 选择下一个 backend | from、to |
| `resume_started` | resume use case 开始恢复 | session_id、state |
| `compression_started` | 达到 high-water 且开始摘要 | source range/hash、workspace revision、target tokens |
| `compression_finished` | 摘要通过 schema/fact 校验并持久化 | summary id、source range/hash、workspace revision、usage |
| `compression_rejected` | 摘要未配置、失败或校验拒绝 | reason、source range（若有） |
| `summary_invalidated` | workspace/file fact 与摘要不再一致 | summary id、workspace revision、reason |
| `run_finished` | Runtime 进入终态 | final_state、计数、source invariant、failure |

状态迁移 event 的 envelope `state` 是目标状态；其他 event 的 `state` 是事件发生时状态。Sequence 对每个 session 从 1 连续递增。

## 10. M2/M3 持久化与恢复契约

- 默认 `AgentApplication` 打开 `<agent-home>/state.db`，启动时运行有序 migration；当前 schema version 为 3。
- SQLite `sessions`、`messages`、`events`、`checkpoints`、`model_calls`、`tool_calls` 和 `summaries` 是当前 session、恢复和摘要事实来源；默认连接打开 `foreign_keys=ON`、`journal_mode=WAL`、`busy_timeout=5000`。
- `RuntimeSnapshot` 使用显式 `snapshot_version=2` JSON；包含 pending/active call、resume target、retry metadata、approval/interruption 字段、policy、failure 和计数，不使用 pickle。
- `create_session()` 原子建立 session、初始 checkpoint、user message 和 `session_created`/`message_added` 事件。
- `JournalMutation` 用 `expected_state` 与 `expected_version` 做乐观并发校验；session state、checkpoint、version、event sequence 以及可选 message 在同一短事务中提交。冲突和提交前故障都不得留下部分写入。
- 每个 session 的 event sequence 由 `sessions.last_event_sequence` 在写事务内递增，不使用无锁的 `SELECT MAX(sequence)`。
- DB commit 成功后才允许写 JSONL；JSONL 是可删除、可重建的 projection。`export-trace` 按 DB sequence 重建 envelope，replay 不读取 JSONL 做状态决策。
- lease 只允许一个 owner 推进 session；过期 takeover 后旧 owner 的 mutation 会被拒绝。
- `resume` 先验证 source fingerprint 和 isolated workspace，再根据 call journal 处理 PREPARED、RUNNING、已保存 result 和 UNCERTAIN。
- restricted test/run command 的每次 attempt 记录稳定 execution id；sandbox metadata、limit kind、output truncation、workspace revisions 和 cleanup 状态进入 ToolResult/event，SQLite journal 仍是恢复 authority。
- edit 使用 pre/post content hash；既不匹配时只能进入 `WAITING_APPROVAL`。resolution 只能是
  `effect-not-applied`、`effect-applied`（必须提供 result）或 `abort`。
- JSONL 仍是 DB committed events 的可删除 projection，不参与恢复决策。

`summaries` 行保存摘要 JSON 及其 lineage 索引。摘要更新不改变原始 event sequence；
supersede 只写新摘要并给旧摘要加 `superseded_by`，stale 标记会让 Context Engine 停止
使用该摘要。

## 11. Evaluation 契约

Eval suite 是严格 schema version 1 的 JSON manifest。case 的 fixture/backend fixture
相对 suite root 解析并做 containment；每个 repetition 使用新的 agent home、SQLite 和
workspace。可信 oracle 包括 `test_profile`、`file`、`changed_paths` 和 `result_schema`；
oracle 自身配置或 fixture 错误计为 `eval_infrastructure_failure`，不算 Agent task failure。

报告区分 `task_success`（全部 oracle 通过）与 `runtime_completed`（Runtime 进入
`COMPLETED`），并汇总 calls、input/output/compression tokens、run/model/tool/test/
compression latency、failure reasons、recovery、permission 和 source invariant。原始
`runs.jsonl` 保留 session/trace 定位字段；`report.json` 的 comparison runs 去除随机
session ID/trace path。A/B 只比较相同 case/repetition key，并输出 paired diff。
case 通过 `case_type=task|negative_control` 显式分类；正常任务成功率与负控制 observed
failure rate 分别统计，历史全体 run 指标仅为兼容字段。

## 12. Replay 与 golden 契约

Replay 不执行外部动作，只做：

- sequence/transition 校验；
- model/tool/test/Token 聚合；
- `run_finished` 状态和计数交叉校验；
- source invariant、failure kind 和最终测试结果提取。

Semantic projection 允许进入 golden 的字段：状态迁移、工具顺序、工具状态聚合、测试结果序列、稳定计数、source invariant、failure kind。

禁止进入 golden：event UUID、session UUID、timestamp、绝对路径、duration、临时目录名和整段 stdout/stderr。

## 13. 失败分类

| 失败 | 表达方式 | 是否自动结束 Runtime |
|---|---|---|
| Schema/路径/权限 | ToolResult observation | 否 |
| 测试断言失败 | `success + passed=false` | 否 |
| 工具 timeout/handler exception | ToolResult observation | 否 |
| Backend 协议错误 | BackendError + `FAILED` | 是 |
| retryable provider fault | `RETRY_WAIT` + retry/fallback events | 否，直到预算耗尽 |
| 不确定写副作用 | `WAITING_APPROVAL` + call resolution | 否，直到显式 resolution |
| 预算耗尽 | `FAILED` | 是 |
| FSM invariant | `FAILED` 或直接抛出（若记录系统自身失效） | 是 |

`COMPLETED != task success` 是必须保留的语义。M3 oracle 独立计算 task success。
