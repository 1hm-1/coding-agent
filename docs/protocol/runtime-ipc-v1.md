# Runtime IPC Protocol v1

> 状态：P2-M1 producer 已于 2026-09-16 完成；Agent Platform consumer 不在本仓库实现。
> Authority：本文件与仓库根目录 `protocol/v1/*.schema.json` 是 Runtime IPC v1 的唯一生产者规范。
> Consumer：Agent Platform 只引用、固定并验证本协议，不维护第二份 wire schema。

## 1. 目的与边界

Runtime IPC v1 定义 Agent Platform worker 与 Coding Agent headless runner 之间的进程协议：

```text
Platform RuntimeAdapter
  → private request file + direct argv + minimal environment
  → Coding Agent headless process
  ← stdout JSONL events/result + stderr diagnostics
```

本协议不定义：

- Coding Agent 内部 SQLite、checkpoint 或 trajectory schema；
- Platform REST、Redis、PostgreSQL 或 SSE schema；
- Provider HTTP payload；
- MCP wire protocol；
- Terminal/TUI 输出。

内部事件必须先经过 public projector、大小限制和脱敏，才能进入 IPC。平台不能根据内部数据库或人类日志
推断 Runtime 状态。

## 2. 版本模型

必须区分三种版本：

| 字段 | 示例 | 含义 |
|---|---|---|
| `runtime_version` | `0.2.0` | Coding Agent 软件发布版本 |
| `protocol_version` | `1` | 本文定义的 wire schema/语义版本 |
| internal schema versions | snapshot v2 / DB v3 | Runtime 私有持久化版本，不跨 IPC 暴露 |

所有 request、event、result 和 protocol-info document 都包含整数 `protocol_version=1`。

兼容规则见 [`compatibility.md`](./compatibility.md)：新增可选能力或未知 informational event 不要求升级；
删除必需字段、改变类型/语义、改变排序或终态规则必须增加 protocol version。

## 3. 传输与进程约束

### 3.1 发现能力

无副作用 discovery 命令：

```bash
coding-agent protocol-info --protocol-version 1 --output json
```

stdout 只输出一份符合 `protocol/v1/capabilities.schema.json` 的 JSON；stderr 只用于诊断。

### 3.2 启动执行

headless producer 命令：

```bash
coding-agent run-headless \
  --protocol-version 1 \
  --request-file /private/attempt/input/request.json
```

约束：

- Adapter 使用 direct argv，禁止 shell string 和 `shell=True`；
- request file 位于该 attempt 的私有根目录，创建时权限至多 `0600`；
- request file 符合 `execution-request.schema.json`，不得包含 Secret 值；
- stdout 每行一个 UTF-8 JSON object，除协议记录外不得输出其他内容；
- stderr 是有上限的诊断流，不能成为业务状态来源；
- 进程必须处于独立进程组，Platform 才能清理完整子进程树；
- Platform 不在数据库事务中等待进程或读取 stdout。

### 3.3 JSONL 顺序

`event` 和最终 `result` 共享同一个从 1 开始、严格连续递增的 `sequence`：

```text
event sequence=1
event sequence=2
...
result sequence=N   # 最后一条协议记录
EOF
```

允许零个 informational event，但合法执行必须恰好有一个最终 result。result 后出现任何记录、重复 result、
sequence 缺口/倒序或 stdout 非 JSON 内容都属于 `runtime_protocol_error`。

## 4. Capability negotiation

`protocol-info` 返回：

```json
{
  "protocol_version": 1,
  "kind": "protocol_info",
  "runtime_name": "coding-agent",
  "runtime_version": "0.2.0.dev0",
  "supported_protocol_versions": [1],
  "capabilities": [
    "structured_events",
    "cooperative_interrupt",
    "checkpoint_resume",
    "scripted_backend",
    "real_model_backend"
  ],
  "backend_kinds": ["scripted", "openai-compatible", "anthropic"],
  "sandbox_modes": ["native_linux", "fail_closed"],
  "schema_digests": {
    "capabilities": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
    "execution_request": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
    "event_envelope": "cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc",
    "terminal_result": "dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd"
  },
  "limits": {
    "max_request_bytes": 1048576,
    "max_record_bytes": 262144
  },
  "extensions": {}
}
```

已保留的 capability identifiers：

```text
structured_events
cooperative_interrupt
checkpoint_resume
scripted_backend
real_model_backend
native_linux_sandbox
memory
skills
mcp
multi_agent
terminal_approval
```

规则：

- capabilities 是描述已启用实现的集合，不是路线图愿望；
- Platform 在启动前验证 `required_capabilities` 是其子集；
- 不满足时不启动进程，分类为 `runtime_capability_missing`；
- 新增 capability identifier 是向后兼容变化；
- capability snapshot 与 attempt 关联，不能因进程运行中动态 discovery 悄然改变；
- `backend_kinds` 和 `sandbox_modes` 同样按字符串标识协商；consumer 忽略未知值，但不能假定它可用；
- `schema_digests` 是四个 producer Schema 文件内容的 SHA-256，Platform 必须与 pinned release 校验；
- `v0.1.0` 尚未实现此命令，不得伪造 capabilities。

## 5. Execution request

权威 Schema：`protocol/v1/execution-request.schema.json`。

示例：

```json
{
  "protocol_version": 1,
  "kind": "execution_request",
  "execution_id": "exec-01J...",
  "correlation": {
    "run_id": "run-01J...",
    "attempt_id": "attempt-01J..."
  },
  "task": "Fix the failing parser test.",
  "source": {
    "path": "/private/attempt/source",
    "expected_revision": "<tree_fingerprint SHA-256>",
    "read_only": true
  },
  "runtime": {
    "agent_home": "/private/attempt/runtime-home",
    "timeout_seconds": 900
  },
  "backend": {
    "kind": "scripted",
    "script_path": "/private/attempt/input/script.json"
  },
  "policy": {
    "max_steps": 80,
    "max_model_calls": 20,
    "max_tool_calls": 40,
    "max_output_tokens": 4096,
    "allowed_permissions": ["read", "write", "execute_test"]
  },
  "required_capabilities": ["structured_events", "cooperative_interrupt"],
  "metadata": {
    "request_origin": "agent-platform"
  },
  "extensions": {}
}
```

### 5.1 身份

- `execution_id` 由 Platform attempt 生成，进程生命周期内唯一；
- `run_id`/`attempt_id` 只用于 correlation，不授予权限；
- `runtime_session_id` 由 Coding Agent 创建并只在 event/result 中返回；
- Phase 2 的 `agent_id` 是 Runtime 内部身份，不替代上述 ID。

### 5.2 Source 与 runtime home

- `source.path` 和 `runtime.agent_home` 是 Adapter 与子进程之间的私有绝对路径；
- request file 必须位于 `<attempt-root>/input/request.json`；source、runtime home 与 scripted
  backend input 必须 containment 在同一 attempt root，且 source/runtime home 不得重叠；
- Platform 先把 `trusted_source_ref` 解析为受信 source，再生成 IPC request；
- `source.read_only` 在 v1 必须为 `true`；Coding Agent 继续复制到自己的 isolated workspace；
- 两个路径都必须位于 Platform 分配/允许的 execution roots；
- Coding Agent 仍执行自身 containment、symlink 和 source fingerprint 校验；
- 绝对路径不能进入公共 event/result、API、SSE 或普通日志。

### 5.3 Backend 与 Secret

- `api_key_env` 只保存环境变量名称，不保存其值；
- `backend.kind` 必须同时存在于已协商的 `backend_kinds`，当前计划支持 `scripted`、`openai-compatible` 和 `anthropic`；
- Platform 只将 allowlist 中被该 backend 需要的 Secret 注入子进程环境；
- `script_path` 只能用于 `scripted` backend；
- 非 scripted backend 必须提供 `model`；
- Provider-specific HTTP payload 仍只存在于 Coding Agent adapter；
- request、stdout、stderr 和 retained artifacts 都要经过 Secret 泄漏测试。

### 5.4 Policy

Platform 可以请求更小预算，不能通过 request 绕过 Coding Agent 内部硬上限。有效权限是：

```text
platform request
∩ runtime deployment policy
∩ agent/profile policy
```

缺少权限时返回结构化 Runtime failure/observation，而不是扩大权限。

`extensions` 是 v1 为 Phase 2 profile/Skill/Memory/MCP 选择保留的有界、namespaced 扩展对象。扩展值
只能包含 Schema 允许的浅层标量/数组/对象，必须使用配置或 Secret reference，禁止嵌入 Secret 值。
consumer 必须忽略不理解且未被 required capability 声明的扩展；producer 遇到影响语义但不支持的
扩展必须 fail closed，不能静默忽略。

## 6. Event envelope

权威 Schema：`protocol/v1/event-envelope.schema.json`。

```json
{
  "protocol_version": 1,
  "kind": "event",
  "execution_id": "exec-01J...",
  "runtime_session_id": "runtime-session-...",
  "sequence": 4,
  "timestamp": "2026-09-07T10:00:00Z",
  "type": "tool.call_finished",
  "payload": {
    "tool_name": "restricted_test",
    "status": "success",
    "duration_ms": 42.1
  }
}
```

v1 稳定核心事件：

```text
runtime.started
runtime.state_changed
model.call_started
model.call_finished
tool.call_started
tool.call_finished
runtime.interrupt_acknowledged
runtime.finished
```

Phase 2 可以增加 informational 事件，例如 `agent.started`、`memory.retrieved` 或 `skill.selected`。
v1 consumer 必须能够忽略未知 `type`，但仍校验 envelope、sequence、大小和脱敏。只有 terminal result
决定 Platform attempt 的业务终态；event 不是命令，也不能单独证明完成。

Payload 规则：

- 只包含完成 UI/诊断投影所需的最小字段；
- 不含完整 Prompt、完整 Tool output、Secret 或宿主绝对路径；
- 大输出保存为受控 artifact，event 只带相对引用/hash；
- 内部随机 event id 不作为平台幂等键，平台以 `attempt_id + sequence` 去重；
- public projector 必须有 success/redaction/oversize tests。

## 7. Terminal result

权威 Schema：`protocol/v1/terminal-result.schema.json`。

成功示例：

```json
{
  "protocol_version": 1,
  "kind": "result",
  "execution_id": "exec-01J...",
  "runtime_session_id": "runtime-session-...",
  "sequence": 12,
  "timestamp": "2026-09-07T10:01:00Z",
  "status": "succeeded",
  "final_output": "Implemented the fix and tests pass.",
  "failure": null,
  "metrics": {
    "model_calls": 3,
    "tool_calls": 6,
    "input_tokens": 12000,
    "output_tokens": 800,
    "duration_ms": 60123.4,
    "agent_count": 1,
    "delegation_count": 0,
    "extensions": {}
  },
  "artifacts": [
    {
      "kind": "trajectory",
      "relative_path": "artifacts/trajectory.jsonl",
      "media_type": "application/x-ndjson",
      "sha256": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
      "size_bytes": 4096
    }
  ]
}
```

规则：

- `succeeded` 表示 Runtime 到达 `COMPLETED` 并产生 final output，不等于外部 oracle 已证明代码正确；
- `failed` 表示 Runtime 形成了可解释失败结果；
- `timed_out` 表示 Runtime 自己在协议内确认 deadline 终止；Platform 强杀由 Adapter 另行分类；
- `cancelled` 表示 cooperative cancellation 已确认，进程和子进程已经停止；
- `failure.code` 是稳定机器码，`safe_message` 已脱敏，`retryable` 只是 Runtime 建议；
- Platform 是否创建新 attempt 仍由平台 retry policy 决定；
- artifact path 必须相对 attempt root，禁止绝对路径和 `..`；
- result 必须是 stdout 最后一条协议记录。

## 8. Cancellation 与 interruption

v1 的 Linux 基线流程：

```text
Platform request_cancel(execution_id)
  → 对 Runtime process group 发送一次 SIGINT
  → runner 幂等记录 cancel request
  → Runtime 在安全状态边界提交 INTERRUPTED checkpoint
  → event runtime.interrupt_acknowledged
  → 停止/清理活动子进程
  → result status=cancelled
  → exit 0
```

如果 grace period 内没有得到合法 result：

```text
SIGTERM process group
  → bounded wait
  → SIGKILL process group
  → Platform 根据 deadline/cancel intent 分类
```

语义边界：

- Platform cancellation 是停止当前 attempt 的意图；
- Runtime interruption 是保存本地安全 checkpoint 的机制；
- cancelled attempt 不等于其 checkpoint 可在其他 Worker 恢复；
- 重复 cancel 幂等，不产生多个互相矛盾的 terminal result；
- result 与 Platform 强杀竞态时，以 Platform 成功持久化的 lease-fenced decision 为权威；
- 任意时刻抢占 Python 文件写入或 exactly-once 副作用不属于 v1 承诺。

## 9. 进程退出码与 Platform 映射

退出码描述 runner/协议能否完成交换，不直接等于代码任务是否成功：

| Exit | Runtime 含义 | 是否应有 result | Platform 基础映射 |
|---:|---|---|---|
| `0` | 协议正常结束，包括 succeeded/failed/timed_out/cancelled | 必须有一个 | 按 result.status |
| `64` | request/schema/config 非法 | 否 | `runtime_start_failed`，默认不重试 |
| `65` | protocol version 不支持 | 否 | `runtime_protocol_error`，不重试 |
| `70` | headless runner 未处理异常 | 否 | `runtime_failed`，是否重试由平台策略 |
| `74` | 启动前 workspace/private I/O 失败 | 否 | `runtime_start_failed` |
| signal/其他 | 进程崩溃或被强杀 | 不可信 | 结合 cancel/deadline 映射 |

确定规则：

- exit 0 但缺少/重复/非法 result：`runtime_protocol_error`；
- 非零退出但已经输出 result：`runtime_protocol_error`，不能选择性相信两者之一；
- 被 Platform deadline 强杀：`runtime_timeout`；
- cancel 已请求且清理完成但来不及产生 result：只能由 Adapter 明确记录为强制 cancellation，不能伪造
  Runtime cooperative acknowledgement；
- stderr 文本永远不参与分类；
- Runtime failure code 保存为附属字段，不直接拼接成平台异常类型。

## 10. Secret、路径和输出规则

禁止出现在 request/event/result/artifact manifest/普通日志中的内容：

- API key、Authorization header、provider token；
- repository credential；
- Secret value 或完整进程环境；
- Platform 数据库 DSN；
- 未脱敏宿主 source/workspace/agent-home 绝对路径；
- 未截断的完整 Prompt、Provider response 或 Tool output。

私有 request 中允许 `source.path` 和 `runtime.agent_home`，但：

- 文件权限和目录 containment 必须校验；
- 不复制到 PostgreSQL、Redis payload、SSE 或公共 artifact；
- 错误消息中的路径用逻辑标识替代；
- Platform 与 Runtime 两边都做 redaction test，不能只依赖一侧。

stdout/stderr/单条 record/总 artifact 大小均由部署策略设置硬上限。超限必须产生结构化错误或 Platform
Adapter failure，不能无限缓存到内存。

## 11. Producer contract tests

Coding Agent P2-M1 producer 提供以下自动化 contract tests：

### Success

- `protocol-info` 符合 Schema；
- scripted task 输出连续 events 和唯一 result；
- Runtime `COMPLETED` 映射 succeeded；Runtime `FAILED` 映射 failed；
- v0.1 单 Agent semantics 通过 v1 bridge；
- 后续 v0.2 内部能力开启后仍通过相同基础 golden。

### Failure

- request 缺字段、未知字段、错误类型、超大文件；
- unsupported protocol/capability；
- source/runtime-home containment；
- Secret/path/output redaction；
- internal event payload 无法安全投影；
- stdout writer failure 和 terminal result serialization failure。

### Cancellation/recovery

- cancel before Runtime start；
- cancel during model/tool boundary；
- repeated cancel；
- child process cleanup；
- checkpoint committed before cancelled result；
- crash after events but before result；
- result written once。

Golden 不包含随机 UUID、时间或绝对路径；使用 semantic projection 比较核心语义。

## 12. Consumer contract tests

Agent Platform 使用同一批 Schema/golden，至少验证：

- FakeRuntimeAdapter 和真实 CodingAgentAdapter 具有相同基础 result contract；
- v1 request 生成正确且不含 Secret；
- events 以 attempt+sequence 去重并按序持久化；
- unknown informational event 可安全投影/忽略；
- malformed JSON、非协议 stdout、缺少/重复 result；
- exit/result 冲突；
- timeout、cancel、process group cleanup；
- environment allowlist 和 attempt directory containment；
- Platform pin 的每个 Runtime release 都通过 compatibility matrix。

## 13. 变更流程

任何协议变化必须：

1. 先更新本权威文档和 JSON Schema；
2. 分类为 additive-compatible 或 breaking；
3. 更新 [`compatibility.md`](./compatibility.md)；
4. 增加 producer golden/negative tests；
5. 在 Agent Platform 更新支持矩阵和 consumer tests；
6. 两边通过后才发布/启用；
7. `current-state.md` 必须区分 producer 已实现能力与仍未实现的 Platform consumer。

当前 `0.2.0.dev0` 开发树已实现 `protocol-info`、`run-headless` 和 streaming producer IPC，
并通过 111 项默认测试、producer goldens/vectors、静态/覆盖率/构建门禁。Platform
`CodingAgentAdapter` 仍不在本仓库实现；固定发布 `v0.1.0` 也仍不包含本协议。
