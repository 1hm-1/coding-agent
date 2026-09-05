# M2：持久恢复与真实模型实施计划

> 状态：M2.1、M2.2、M2.3 已完成  
> 前置门禁：M1/M1.5 原有 22 个测试和四份 golden 全部通过  
> 实施原则：按 M2.1 → M2.2 → M2.3 顺序合并，不做大爆炸式重写

## 1. M2 目标

M2 把当前“单进程可运行”的 Runtime 升级为：

1. SQLite 是 session、message、event、checkpoint 和调用 journal 的恢复事实来源；
2. 状态边界中断后可以继续，且不会盲目重复未知副作用；
3. Runtime 可在不含 provider 分支的前提下使用 OpenAI-compatible 和 Anthropic adapter；
4. 瞬时 provider 故障具有有界、可观察、可恢复的 retry/fallback；
5. JSONL 继续作为可读导出轨迹，M1.5 golden 继续有效。

M2 不实现完整上下文压缩、Eval Harness、通用 Shell 或 OS 级沙箱。

## 2. 子阶段与顺序

| 子阶段 | 目标 | 必须独立通过后才能继续 |
|---|---|---|
| M2.1 | SQLite persistence foundation | storage contract、原子迁移、JSONL 可重建 |
| M2.2 | checkpoint、interrupt、resume、副作用恢复 | crash matrix、重复副作用测试 |
| M2.3 | 两个真实 adapter、retry、fallback | adapter contract、错误分类、live smoke 可选 |

不要在 M2.1 同时接真实模型；否则数据库错误、恢复错误和 provider 错误无法归因。

## 3. M2.1：SQLite persistence foundation

### 3.1 新增模块

```text
src/coding_agent/
├── persistence.py          # protocol、SQLite implementation、transactions
├── migrations.py           # ordered schema migrations
└── export.py               # committed DB events → JSONL
```

如果实现足够小，`migrations.py` 可以先放在 `persistence.py`；不要创建没有消费者的抽象层。

### 3.2 数据库位置和连接设置

```text
<agent-home>/state.db
```

每个连接必须执行：

```sql
PRAGMA foreign_keys = ON;
PRAGMA journal_mode = WAL;
PRAGMA busy_timeout = 5000;
```

写入使用短事务。绝对不能在数据库事务中等待模型、工具或测试子进程。

### 3.3 Schema v1

建议最小表：

```text
schema_migrations(
  version INTEGER PRIMARY KEY,
  applied_at TEXT NOT NULL
)

sessions(
  id TEXT PRIMARY KEY,
  task TEXT NOT NULL,
  source_path TEXT NOT NULL,
  workspace_path TEXT,
  state TEXT NOT NULL,
  policy_json TEXT NOT NULL,
  source_fingerprint TEXT NOT NULL,
  final_answer TEXT,
  failure_json TEXT,
  step_count INTEGER NOT NULL DEFAULT 0,
  model_calls INTEGER NOT NULL DEFAULT 0,
  tool_calls INTEGER NOT NULL DEFAULT 0,
  last_event_sequence INTEGER NOT NULL DEFAULT 0,
  version INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
)

messages(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
  message_index INTEGER NOT NULL,
  role TEXT NOT NULL,
  content TEXT NOT NULL,
  tool_call_id TEXT,
  metadata_json TEXT NOT NULL,
  created_at TEXT NOT NULL,
  UNIQUE(session_id, message_index),
  UNIQUE(session_id, tool_call_id)
)

events(
  event_id TEXT PRIMARY KEY,
  session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
  sequence INTEGER NOT NULL,
  schema_version INTEGER NOT NULL,
  event_type TEXT NOT NULL,
  state TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  created_at TEXT NOT NULL,
  UNIQUE(session_id, sequence)
)

checkpoints(
  session_id TEXT PRIMARY KEY REFERENCES sessions(id) ON DELETE CASCADE,
  state TEXT NOT NULL,
  snapshot_json TEXT NOT NULL,
  updated_at TEXT NOT NULL
)
```

`model_calls`、`tool_calls` journal 表在 M2.2 的 migration v2 加入，避免 M2.1
一次承担全部恢复语义。

### 3.4 权威写入接口

目标不是把当前 JSONL writer 包一层 SQLite，而是让状态和事件在同一事务中提交：

推荐用一个原子 mutation 窄腰，避免 Runtime 分别写 session、checkpoint 和 event：

```python
@dataclass(frozen=True)
class JournalMutation:
    session_id: str
    expected_version: int
    expected_state: RuntimeState
    snapshot_after: RuntimeSnapshot
    event_type: EventType
    payload: JsonObject
    message_to_append: Message | None = None

@dataclass(frozen=True)
class CommitResult:
    event: Event
    committed_version: int

class RunJournal(Protocol):
    def create_session(
        self,
        snapshot: RuntimeSnapshot,
        initial_message: Message,
    ) -> tuple[Event, ...]: ...

    def commit(self, mutation: JournalMutation) -> CommitResult: ...
    def load_snapshot(self, session_id: str) -> RuntimeSnapshot: ...
    def list_messages(self, session_id: str) -> list[Message]: ...
    def list_events(self, session_id: str) -> list[Event]: ...
```

`RuntimeSnapshot` 在 M2.1 先包含当前 `Session` 可恢复字段及显式 serialization version；M2.2 再增加 active call、retry 和 approval 字段。接口名可根据实现微调，但不得退化为多个无原子性的 public write 方法。

必须满足这些原子性：

- transition mutation 同一事务更新 session state、checkpoint、version、sequence 并插入 event；
- message mutation 同一事务插入 message、更新 checkpoint/sequence 并插入 event；
- 更新使用 `expected_state` 和 `version`，冲突必须显式失败，不能 last-write-wins；
- sequence 使用 sessions 中的计数器分配，不能用无锁的 `SELECT MAX(sequence)`。

内存与数据库的提交顺序：先从当前内存状态构造 `snapshot_after`，提交成功后再把 committed snapshot 应用到内存 Session。数据库 commit 失败时，内存 Session 不得假装已经迁移。M2.1 期间可以保留兼容 adapter，但 Runtime 不能同时把 JSONL 和 SQLite 当作两个独立 authority。

### 3.5 JSONL 角色变化

M2.1 后：

```text
SQLite events = 恢复事实来源
JSONL          = 可删除、可重建的导出投影
```

JSONL 只能在数据库事务提交后写。导出失败不能回滚已经提交的 Runtime 状态；必须提供 `export-trace --session-id` 从 DB 重建。

### 3.6 M2.1 checklist

- [x] migration v1 和重复运行幂等测试；
- [x] 遇到未知更高 schema version 时拒绝启动；
- [x] create/load session、message、event、checkpoint round-trip；
- [x] state + checkpoint + event 原子 transition；
- [x] expected state/version 冲突测试；
- [x] crash-before-commit 不留下半条状态；
- [x] DB events 可导出并通过现有 replay；
- [x] 删除 JSONL 后能从 DB 重建相同 semantic projection；
- [x] 将 O(n²) JSONL sequence 分配移出主写路径；
- [x] 四份 M1.5 golden 继续通过。

### 3.7 M2.1 建议实施顺序

每一步通过目标测试后再继续：

1. **Migration runner**：建立空库、应用 v1、重复应用无变化、拒绝未知更高版本。
2. **Serialization**：为 policy、message、failure、pending calls 和 snapshot 做显式 JSON round-trip；拒绝未知 snapshot version。
3. **SQLite journal contract**：实现 create/load/list 与 transaction helper，打开 foreign keys/WAL/busy timeout。
4. **Atomic mutation**：实现 expected state/version 检查、事务内 sequence 分配、session/checkpoint/event/message 原子写。
5. **Runtime integration**：通过 journal 提交 session 创建、消息、状态和普通事件；提交失败不得留下错误的内存状态。
6. **JSONL exporter**：按 sequence 从 DB 生成现有 Event envelope；重复导出结果在语义上相同。
7. **Compatibility regression**：运行原有 22 个测试加 M2.1 storage tests、四份 golden、calculator/todo run + replay。
8. **Documentation closure**：只在上述门禁全绿后更新 current-state、roadmap、repository structure 和 HANDOFF。

M2.1 最小新增测试建议：

```text
tests/test_persistence.py
  migration fresh/idempotent/future-version
  session-message-event-snapshot round-trip
  atomic transition commit/rollback
  expected state/version conflict
  sequence continuity
  JSONL export and rebuild

tests/test_vertical_slice.py
  existing scripted run through SQLite authority
  exported trace replays to existing semantic golden
```

M2.1 验收证据（2026-09-04）：30 个 unittest 全部通过；calculator 和 todo smoke
均通过，todo 保持 `false → true`；当时 SQLite migration version 为 1。SQLite
提交前故障会回滚 session、checkpoint、event 和 message，删除 JSONL 后可由
`export-trace` 重建。

不要通过同时保留旧 JSONL 直接写入来让旧测试“暂时通过”；兼容应来自 committed DB events 的导出或测试专用旧 store adapter。

## 4. M2.2：checkpoint、interrupt 与 resume

### 4.1 Domain 变化

新增：

```text
RuntimeState.INTERRUPTED
RuntimeState.WAITING_APPROVAL
ToolCallState.PREPARED/RUNNING/SUCCEEDED/FAILED/UNCERTAIN
RecoveryMode.READ_ONLY/RECONCILABLE_WRITE/REPEATABLE_OBSERVATION/NON_IDEMPOTENT
RuntimeSnapshot
```

`RuntimeSnapshot` 至少保存：state、step/model/tool counters、pending calls、active call id、resume target、retry metadata、source/workspace fingerprint 和 context version。

不要直接 pickle Python 对象。使用显式、带版本的 JSON serialization。

### 4.2 调用 journal 表

M2.2 使用 migration v2 扩展 `sessions`：

```text
lease_owner TEXT
lease_expires_at TEXT
interrupt_requested_at TEXT
resume_target_state TEXT
```

开始或恢复运行前，通过单条条件更新获取 lease：只有未持有、已过期或 owner 相同的 session 可成功。Runtime 在状态边界续租；持有 lease 的进程不得在数据库事务内等待外部调用。lease 解决“谁有权推进状态”，tool/model journal 解决“前一次调用是否已产生效果”，两者不能互相替代。

```text
model_calls(
  request_id TEXT PRIMARY KEY,
  session_id TEXT NOT NULL,
  ordinal INTEGER NOT NULL,
  backend TEXT NOT NULL,
  status TEXT NOT NULL,
  request_json TEXT NOT NULL,
  response_json TEXT,
  error_json TEXT,
  started_at TEXT NOT NULL,
  finished_at TEXT,
  UNIQUE(session_id, ordinal)
)

tool_calls(
  call_id TEXT PRIMARY KEY,
  session_id TEXT NOT NULL,
  ordinal INTEGER NOT NULL,
  tool_name TEXT NOT NULL,
  arguments_json TEXT NOT NULL,
  recovery_mode TEXT NOT NULL,
  status TEXT NOT NULL,
  pre_revision TEXT,
  planned_post_revision TEXT,
  result_json TEXT,
  error_json TEXT,
  started_at TEXT,
  finished_at TEXT,
  UNIQUE(session_id, ordinal)
)
```

### 4.3 Tool 两阶段执行

```text
事务 A：validate/prepare → journal PREPARED + event
事务 B：mark RUNNING
外部：执行工具，不持有 DB transaction
事务 C：保存 result + SUCCEEDED/FAILED + event
事务 D：追加 observation + checkpoint next state
```

故障注入点必须覆盖 A/B/C/D 之间的窗口。

### 4.4 恢复规则

| 状态 | 恢复动作 |
|---|---|
| tool `PREPARED` | 尚未执行，可继续 |
| tool `SUCCEEDED/FAILED` 且 observation 已存在 | 复用，不执行 |
| tool result 已存在但 observation 缺失 | 只补 observation |
| `RUNNING + READ_ONLY` | 新 attempt 重做并记录 recovery event |
| `RUNNING + RECONCILABLE_WRITE` | 比较 pre/planned-post/current hash |
| 当前 hash = pre | 可执行 |
| 当前 hash = planned post | 副作用已发生，只补 result |
| 当前 hash 均不匹配 | 标记 `UNCERTAIN`，进入 `WAITING_APPROVAL` |
| `RUNNING + NON_IDEMPOTENT` | 不自动重放，进入 `WAITING_APPROVAL` |

`edit_file` 应在 prepare 阶段计算 pre hash 和预期 post hash；仅靠相同参数或 `call_id` 不能证明副作用是否已经发生。

`restricted_test` 属于 `REPEATABLE_OBSERVATION`，因为测试通常可重跑，但仓库代码可能有副作用。M4.1 中重跑仍必须产生新 attempt，不能宣称 exactly-once。

### 4.5 Model 调用恢复

- 调用前持久化 request id 和 request payload；
- 返回后先保存 normalized response，再消费 tool calls/final；
- 若 response 已保存但尚未状态迁移，恢复时消费已存 response，不再次请求；
- 若只存在 RUNNING 且无 response，记录 uncertain attempt，再按 policy 重发；
- request id 保持稳定，但不能假设所有 provider 支持幂等计费。

ScriptedBackend 恢复需要按已完成 model ordinal 定位脚本位置。这个逻辑放在 backend factory/配置恢复中，不在 Runtime 写 `isinstance(ScriptedBackend)`。

### 4.6 Interrupt

M2.2 先实现状态边界安全中断：

- CLI 捕获 SIGINT，设置 interrupt request；
- Runtime 在每个 step 前后、模型返回后、工具返回后检查；
- 先提交 `INTERRUPTED` checkpoint/event，再退出 CLI；
- 长工具仍由自身 timeout/cancellation 负责；
- 不宣称能安全抢占任意 Python 文件写入指令。

外部 `request_interrupt(session_id)` 只设置 `interrupt_requested_at`，不直接覆盖 Runtime state。当前 lease owner 在安全边界读取请求并原子写入 `INTERRUPTED + resume_target_state + event`。若进程已经消失，新 owner 在 lease 过期后按 call journal 做恢复，不假设旧进程执行过清理。

### 4.7 不确定副作用审批

`WAITING_APPROVAL` 必须有显式 resolution，而不是让下一次 `resume` 猜测：

```text
coding-agent resolve-call --session-id ... --call-id ... \
  --resolution effect-not-applied|effect-applied|abort
```

- `effect-not-applied`：允许创建新 attempt 执行；
- `effect-applied`：操作者必须提供或确认可持久化的 result，再只补 observation；
- `abort`：以分类 failure 结束 session；
- resolution 记录 actor、time、reason 和 event，不记录未脱敏身份凭据。

首版 CLI 可以只允许本机操作者，但 domain/persistence 中必须保存 resolution 事实。禁止提供“force resume and rerun everything”。

### 4.8 CLI

建议增加：

```text
coding-agent sessions
coding-agent show --session-id ...
coding-agent resume --session-id ... [backend options]
coding-agent interrupt --session-id ...
coding-agent resolve-call --session-id ... --call-id ... --resolution ...
coding-agent export-trace --session-id ...
```

所有命令都通过 Application use case，不允许 CLI 直接更新 SQLite。

### 4.9 M2.2 checklist

- [x] 任意 FSM 状态边界中断/恢复测试；
- [x] tool intent、effect、result、observation 四个 crash window；
- [x] edit pre/post hash reconciliation；
- [x] unknown write 进入 WAITING_APPROVAL，不自动重放；
- [x] 已保存模型响应不重复请求；
- [x] ScriptedBackend 新 runtime 实例恢复保持正确 ordinal；
- [x] SIGINT 请求后先持久化 INTERRUPTED；
- [x] workspace 缺失或 source invariant 异常时明确拒绝 resume；
- [x] 多进程同时 resume 同一 session 只能一个获得 lease/version；
- [x] lease expiry/takeover 使用 fake clock 测试，旧 owner 后续写入被 version/owner 拒绝；
- [x] uncertain call 的三种人工 resolution 都有持久事件和测试；
- [x] 新 failure cases 加入长期 hardening suite；
- [x] 四份原 golden 继续通过。

## 5. M2.3：真实模型 adapter、retry 与 fallback

### 5.1 依赖和配置

建议使用可注入 transport 的 `httpx`，支持连接/读取/总超时和 mock transport。新增依赖前更新 `pyproject.toml` 并记录版本范围。

非秘密配置通过显式配置对象/CLI：provider、model、base URL、timeout、max tokens。API key 只从 provider 对应 secret 环境变量读取，不进入 DB、event、trace 或异常文本。

### 5.2 Adapter

```text
models/openai_compatible.py
models/anthropic.py
models/fallback.py
models/errors.py（只有错误映射复杂时再拆）
```

OpenAI-compatible adapter 负责 chat messages、`tools[].function.parameters`、tool call arguments JSON、finish reason 和 usage 的双向转换。

Anthropic adapter 负责 system 分离、content blocks、`tool_use` / `tool_result`、stop reason 和 usage 的双向转换。

Runtime 只看 `ModelRequest`、`ModelResponse` 和 `BackendError`。

### 5.3 统一错误分类

| Kind | 示例 | Retryable |
|---|---|---|
| `timeout` | connect/read timeout | 是 |
| `rate_limit` | HTTP 429 | 是，尊重 retry-after |
| `provider_unavailable` | 502/503/504 | 是 |
| `authentication` | 401/403 | 否 |
| `invalid_request` | 400/422 | 否 |
| `protocol_error` | malformed JSON/tool args | 否 |
| `content_blocked` | provider safety refusal | 否，作为可解释结果处理 |

Adapter 只分类，不自行循环重试。Runtime 进入持久 `RETRY_WAIT`，保存 attempt、target state 和 next retry time。

### 5.4 Retry/fallback policy

- 指数退避必须有上限和 jitter；测试中注入 clock，不实际 sleep；
- 总 model calls、retry attempts 和 wall time 都受预算限制；
- fallback 只处理 retryable infrastructure error；
- authentication、invalid request、protocol error 不 fallback；
- “答案质量不好”不自动 fallback；
- 每次 retry/fallback 都产生明确事件和 provider metadata。

### 5.5 Adapter tests

每个 adapter 使用录制/手写 HTTP fixture 和 mock transport 覆盖：

- 纯文本 final；
- 单个和多个 tool calls；
- tool result 回填；
- usage；
- Unicode；
- malformed arguments；
- timeout、429、5xx、401 和未知响应格式；
- secret 不进入日志。

真实 API live test 可选、人工触发、默认跳过，不作为普通测试通过条件。

### 5.6 M2.3 checklist

- [x] 两个 adapter 通过同一 contract suite；
- [x] Runtime 中无 provider 名称条件分支；
- [x] 错误分类和 retryability 完整测试；
- [x] retry state 可跨进程恢复且使用 fake clock 测试；
- [x] fallback 只在允许类别触发；
- [x] API key/authorization header 不进入持久化和 trace；
- [x] ScriptedBackend 与全部 golden 继续通过；
- [x] 受控 live smoke 条件已记录：2026-09-05 检查 `OPENAI_API_KEY`/`ANTHROPIC_API_KEY`
  均未配置，故未执行真实 Provider 请求。

## 6. M2 完成定义

M2 只有在以下条件全部满足时才能在 roadmap 标记完成：

1. SQLite 是恢复事实来源，JSONL 可删除后重建。
2. `resume` 在状态边界和规定 crash window 中通过。
3. 已确认副作用不重复；未知写副作用不会自动执行。
4. 两个 adapter 通过相同 contract tests。
5. Retry/fallback 策略可解释、持久且有预算。
6. M1/M1.5 全部测试和四份 golden 保持通过。
7. `current-state.md`、`contracts.md`、README 和 roadmap 已更新。
8. 未提前加入 context compression、Eval Harness、通用 Shell 或多 Agent。

M2 验收证据（2026-09-05）：47 个 unittest 全部通过；Ruff
`E4/E7/E9/F` 全部通过；四份 M1.5 semantic golden 未改变且继续通过；SQLite
schema version 为 2。`tests/test_m2_recovery.py` 覆盖模型/工具 crash window、
interrupt/resume、reconciliation、lease 和三种 resolution；`tests/test_models.py`
覆盖两个离线 adapter contract、错误分类、retry/fallback 以及凭据脱敏。calculator
和 todo smoke 均通过，todo 测试轨迹保持 `false → true`。真实 live smoke 因没有
Provider 凭据未执行。

## 7. 实现时的禁止项

- 不用 pickle 保存 Runtime 对象；
- 不在 DB transaction 内调用模型或工具；
- 不用 `SELECT MAX(sequence)` 在并发写入下分配序号；
- 不把 JSONL 当 checkpoint；
- 不对不确定写操作自动重放；
- 不在 Runtime 判断 provider；
- 不记录 API key、Authorization header 或完整敏感 prompt；
- 不为迁就新实现删除 M1.5 failure/golden cases。
