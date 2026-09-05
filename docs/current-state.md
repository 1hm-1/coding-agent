# 当前实现状态

> 基线日期：2026-09-05  
> 已完成：M0、M1、M1.5、M2.1、M2.2、M2.3、M3.1、M3.2、M3.3、M4.1、M4.2  
> 下一阶段：M5 Eval-driven Capability Expansion（条件阶段）  
> 当前附加门禁：Release/Evidence Hardening 已完成（文档、指标、Git/CI、coverage、类型检查、评测证据）。  
> 本文只描述已经存在并通过测试的行为。

## 1. 已实现能力

### Runtime

- `AgentRuntime` 使用显式 handler map 和 `ALLOWED_TRANSITIONS` 推进状态。
- 当前状态：`CREATED`、`PREPARING_WORKSPACE`、`BUILDING_CONTEXT`、`CALLING_MODEL`、`DISPATCHING_TOOL`、`RECORDING_OBSERVATION`、`INTERRUPTED`、`WAITING_APPROVAL`、`RETRY_WAIT`、`COMPLETED`、`FAILED`。
- `step()` 执行一个状态动作；`run()` 只负责驱动到终态。
- 有 step、model call、tool call 三类预算。
- Runtime 失败会进入 `FAILED` 并尽量记录 `run_finished`；中断会先提交 `INTERRUPTED` checkpoint，未知写副作用会停在 `WAITING_APPROVAL`。

### Model

- 提供 `ScriptedBackend`、OpenAI-compatible 和 Anthropic adapter，并可用 `FallbackBackend` 组合。
- Backend 按固定 JSON 序列返回 tool calls 或 final answer。
- 非法响应、HTTP 错误、timeout、rate limit 和脚本耗尽映射为分类 `BackendError`；retry/fallback 由 Runtime 按持久 policy 驱动。
- 每次接收到的 `ModelRequest` 会保存在 backend 实例中，供测试验证 observation 回填。
- provider API key 只从对应环境变量读取，不写入 request journal、event、JSONL 或错误文本。

### Tools

- 有 `read_file`、`edit_file`、`restricted_test`、`run_command`。
- `ToolHarness` 统一执行注册查找、受限 JSON Schema 校验、权限检查、deadline、异常映射、输出限制和 audit callback。
- 权限枚举的真实名称是 `READ`、`WRITE`、`EXECUTE_TEST`、`EXECUTE_COMMAND`。
- `restricted_test` 只接收可信 profile 名称；当前默认 profile 是 `python_unittest`。
- `run_command` 只接收可信 command profile、结构化 `argv` 和受限相对 `cwd`；不接受
  shell 字符串、shell interpreter 或 profile 外 executable。
- 没有 `run_shell`、文件搜索或 Git inspection 工具。

### Workspace

- 每个 session 复制源目录到 `<agent-home>/workspaces/<session-id>/repo`。
- 复制时忽略 `.git`、`.agent-data`、`__pycache__` 和 `.pytest_cache`。
- 副本内初始化独立 Git baseline。
- 文件工具拒绝绝对路径、`..`、`.git` 元数据和指向 workspace 外的 symlink。
- Runtime 在结束时比较源目录 fingerprint，并在 trajectory 中记录 `source_unchanged`。

### Sandbox（M4.1/M4.2）

- `restricted_test` 仍只接受可信 `TestProfile` 名称；profile 固定 argv、working
  directory、network、environment 和 `ResourceLimits`，模型不能提交 command/argv。
- Linux 默认使用 rootless user/mount/PID/network namespace、最小 chroot rootfs、只读系统
  mounts、受控 tmpfs 和唯一可写 session workspace；不具备所需能力时使用 fail-closed
  executor，不回退到宿主 `subprocess`。
- 默认无网络，环境变量是 allowlist；运行身份设置 `no-new-privileges` 并清空 Linux
  capabilities。wall/CPU/memory/PID/writable-storage/stdout/stderr 均有上限，超时或资源
  耗尽后清理整个执行进程组。
- `ExecutionSpec`、`ExecutionResult`、capability snapshot、native rootfs content identity、
  limits、execution id、output truncation 和 cleanup 状态通过 `ToolResult` 进入
  `tool_call_finished`；公共事件投影不保存完整 workspace 路径或环境值。
- native backend 的 identity 是当前运行时内容哈希，不是已拉取的 OCI image；M4.1 不提供
  通用 Shell、approved network 或跨平台等价实现。
- M4.2 的 `run_command` 使用 `EXECUTE_COMMAND` 和 `NON_IDEMPOTENT` journal mode；profile
  固定 executable allowlist、image、环境、limits 和 cwd 默认值，模型只能提交 argv。
  非零退出作为 observation；schema、allowlist、cwd、approval 和 crash recovery 均有测试。

### Trajectory

- 默认 EventStore 是 `<agent-home>/state.db` 中的 SQLite journal；SQLite 是 session、message、event 和 checkpoint 的恢复事实来源。
- 每个 mutation 使用短事务，以 session version 和 event sequence 做乐观并发校验；提交前失败会整体回滚。
- JSONL 是提交事件的可删除导出投影，可通过 `export-trace` 从 SQLite 重建。
- 每条事件包含 schema version、UUID、session、连续 sequence、事件类型、状态、时间和 payload。
- Replay 校验 sequence、状态迁移以及 `run_finished` 的 model/tool 计数。
- M2.2 的 `model_calls`/`tool_calls` journal 保存 intent、attempt、result 和 reconciliation hash；resume 通过 lease 和 checkpoint 重建内存 Session。
- M3 的 `summaries` 表保存结构化派生摘要、source event range/hash、workspace revision、stale 和 `superseded_by` lineage；原始 events/messages 不被摘要覆盖。
- `context_built` manifest 记录 provider/model、预算、实际与压缩前 Token 估计、section 顺序、硬保留引用、summary、workspace revision 和计数器来源。
- Semantic projection 去除随机字段，汇总 Token、tool order/status、test outcomes、failure kind 和 source invariant。

### Context 与 Evaluation

- `BudgetedContextBuilder` 按 `system → task_runtime → repository → summary → recent` 装配上下文；model capability registry 提供显式上下文上限、protocol margin 和 exact/命名 fallback counter。
- 超过 `0.85 * input_budget` 才尝试有界 compression，目标为 `0.65 * input_budget`；schema/required-fact 校验失败会记录 `compression_rejected` 并保留原始历史。
- Repository snapshot 只从 isolated workspace 构建，包含有上限文件列表、Git diff summary、已读文件 hash、workspace revision 和最近测试摘要。
- `evaluate` 读取严格版本化 JSON suite，按 case/repetition 创建新 workspace，运行可信 test/file/diff/result oracle，分别统计 task success 与 Runtime completion。
- Eval 报告包含 calls、input/output/compression tokens、run/model/tool/test/compression latency、failure taxonomy、recovery、permission 和 source-invariant 指标；`report.json` 的比较投影排除随机 session ID/trace path。

### Hardening evidence

- 四份 semantic golden：成功、测试失败后恢复、权限拒绝、Runtime failure。
- `todo_cli` 展示一次 `false → true` 的测试恢复轨迹。
- Harness 对测试超时和 handler 未预期异常有测试。
- 当前测试数量：75；在当前 capability probe 成功的环境中全部通过；`tests/live_provider_smoke.py` 为凭据门控的显式测试，不计入默认 discovery。能力受限 runner 会对 7 个 native-only case 显式 skip。
- SQLite M2.1 测试覆盖 migration 幂等/未来版本拒绝、snapshot round-trip、原子 mutation、乐观冲突、提交前回滚和 DB→JSONL 重建。
- calculator smoke 产生 48 条连续事件；todo fixture 产生 72 条连续事件并保持 `false → true`。
- Ruff 强制基线 `E4/E7/E9/F` 仍显式写入 `pyproject.toml`；已使用 `uv` 安装 Ruff 0.16.6，`ruff check src tests examples/todo_cli` 通过。
- M2.2 recovery tests 覆盖 interrupt 后复用已保存模型响应、四个工具 crash window、edit hash reconciliation、三种人工 resolution、lease takeover 和 source/workspace resume rejection。
- M2.3 adapter tests 覆盖 OpenAI-compatible/Anthropic text、tool call、usage、Unicode、HTTP error、protocol error、retry/fallback 和 secret 不落盘；无 provider 凭据，因此未执行 live API smoke。
- M3.1/M3.2 tests 覆盖 exact/fallback counter、unknown model、section boundary/hard retention、deterministic manifest、summary round-trip、stale invalidation、required-fact rejection、compression fallback 和 raw-event preservation。
- M3.3 tests 覆盖 strict manifest/containment、fresh repetition、test/file/diff/result oracle、eval infrastructure failure 分类、task/runtime 分离、recovery suite、指标聚合和 paired A/B。
- M4.1 tests 覆盖 capability fail-closed、namespace workspace/rootfs/secret/network/symlink/proc/device
  边界、wall/CPU/memory/PID/storage/output 限制、子进程清理、并行 session 和 SQLite
  tool-observation recovery。
- M4.2 tests 覆盖结构化 argv schema、executable allowlist、cwd containment、profile
  策略、approval fail-closed、非零退出 observation、direct-argv native 边界和
  non-idempotent crash recovery。
- M5 首次能力扩展门禁已运行包含 calculator 与 todo-cli 两个 fixture 的 7-case `budgeted`
  suite：valid runs=7、`infrastructure_failure=0`、`runtime_completion_rate=0.8571`、
  `task_success_rate=0.7143`；
  恢复 case 的 `recovery_triggered=true` 且 `recovery_events=1`；失败仅为预设的
  `invalid_script_response` 和 `oracle_failure`，暂无新增 search/Git/更广执行能力的证据。
- Release/Evidence Hardening 已加入 `RESUME_STARTED` recovery event、70% coverage 门槛（本次
  75 个默认测试实测 73.3%）、release surface 的 mypy 检查、GitHub Actions CI 和凭据门控的
  live provider smoke；CI 会先报告 native capability，能力不足时 skip native-only case 和
  sandbox-dependent eval，不把 fail-closed 结果当作 native security 通过。

## 2. 当前调用链

```text
CLI
  → AgentApplication.run_task()
  → 创建内存 Session + SQLite journal
  → AgentRuntime.initialize()
  → CREATED ... BUILDING_CONTEXT
  → isolated repository snapshot + context sections + optional summary compression
  → CONTEXT_BUILT manifest
  → CALLING_MODEL
  → ModelBackend.complete()（Scripted/OpenAI-compatible/Anthropic/Fallback）
  → model_calls 保存 request/response 或分类错误
  → ToolHarness.execute()
  → WorkspaceGuard / trusted TestProfile+CommandProfile / SandboxPolicy
  → LinuxNamespaceExecutor（capability probe、namespace、rootfs、limits、cleanup）
  → tool observation 追加到 Session.messages
  → final answer
  → 提交 DB run_finished
  → 从 DB 导出 JSONL projection
```

当前 M2 已将 session、messages、pending tool calls、active call、retry metadata 和 approval 边界持久化；SQLite 是恢复 authority，JSONL 只用于可读导出和回放，不参与状态决策。`resume` 会校验 source fingerprint、workspace 身份并获取 session lease。
M3 在此之上把 summary 作为可废弃的 SQLite 派生缓存，把 `BuiltContext.messages` 作为模型输入；summary 不参与权限、完成状态或 tool result 判定。

## 3. 已验证命令

在 `/home/hmli/code/coding-agent` 中执行：

```bash
PYTHONPATH=src python3 -m unittest discover -v
```

```bash
PYTHONPATH=src python3 -m coding_agent.cli \
  --agent-home /tmp/coding-agent-todo-demo \
  run-scripted \
  --source examples/todo_cli \
  --task "Fix the empty input crash and run tests." \
  --script examples/todo_cli_scripted_run.json
```

```bash
.venv/bin/ruff check src tests examples/todo_cli
```

```bash
PYTHONPATH=src python3 -m unittest tests.test_m2_recovery tests.test_models -v
```

```bash
PYTHONPATH=src python3 -m coding_agent.cli \
  --agent-home /tmp/coding-agent-m3-evals \
  evaluate --suite examples/eval_suite.json --variant budgeted
```

```bash
PYTHONPATH=src python3 -m compileall -q src tests examples/todo_cli
```

可用的恢复命令包括 `sessions`、`show`、`interrupt`、`resume` 和
`resolve-call`；真实模型运行使用 `run --provider openai-compatible|anthropic`，API key
从对应环境变量读取。

## 4. 尚未实现

- approved network 的显式 approval UI/授权通道、OCI/container image backend；
- M5 条件能力扩展（search、Git inspection、patch/edit 增强或依赖准备）尚未批准；当前
  固定 eval 没有显示现有工具覆盖率瓶颈；
- 真实 Provider smoke 尚未在本环境执行；已提供手动、凭据门控的 adapter smoke workflow。
- 多 Agent、UI、消息平台、Skill 或 RAG。

## 5. 已知限制与技术债

1. 兼容用 `JsonlEventStore.append()` 每次追加前重新读取整个 session trace，长轨迹仍为 O(n²)；默认 SQLite 主写路径已不再依赖它。
2. M2.2 只保证状态边界安全中断和规定 crash window 的可解释恢复；不能安全抢占任意 Python 文件写入，也不宣称未知副作用 exactly-once。
3. M4.1/M4.2 的 native backend 只在 capability probe 成功的 Linux 环境启用；其他平台或缺少
   namespace/mount 能力时 fail closed。它不是抵御内核/隔离运行时漏洞的完整安全边界。
4. source fingerprint 仍是运行后不变量证据，不替代 workspace mount；M4.1 的测试代码
   只能看到 session workspace，但 workspace 内部仍可能被测试代码任意修改。
5. M4.1/M4.2 使用 native rootfs content identity，不包含 OCI image 构建、SBOM、漏洞
   扫描或依赖安装流程；M4.2 elevated profile 只做 approval fail-closed。
6. Runtime 和 backend 是同步调用。M2 的中断先保证状态边界安全，不应在没有取消语义前假装支持任意时刻抢占；sandbox timeout/崩溃清理已验证。
7. 自研 JSON Schema 只支持内置工具使用的子集，不是完整 Draft 实现。
8. `COMPLETED` 表示 Runtime 正常收到 final answer，不代表任务成功；M3 Eval 已用 oracle 独立评分，但当前固定 suite 只提供离线描述性结果，不代表生产成功率。
9. 当前只支持 Linux/POSIX 进程组终止逻辑；Windows/macOS 等价 sandbox 不属于现阶段验收范围。
10. 项目已初始化 Git 仓库，初始提交 `69a16c6` 已推送到 `origin/main`，并加入 GitHub
    Actions CI 配置；当前仍没有已托管 CI 运行记录，评估器只对每个 isolated fixture 内部
    初始化的 Git baseline 执行 changed-path diff。
11. 当前 coverage 是 statement coverage，发布门槛为 70%；coverage 不等同于安全或任务成功率证明。
12. mypy 当前只对 `command_profiles.py`、`evaluation.py` 和 `sandbox/` release surface 运行；全仓历史代码尚未达到 strict 类型检查标准。
13. live provider smoke 已提供手动 workflow，但本环境无 Provider 凭据，因此未执行真实网络调用。
   GitHub-hosted runner 的 native capability 也可能不足；native-only case 会显式 skip，相关
   sandbox-dependent eval 也会跳过，不能以此替代具备能力环境的 M4.1/M4.2 安全证据。
14. `runtime.py`、`persistence.py`、`evaluation.py` 仍是高集中度大模块；本轮只记录维护风险，不做无验收收益的拆分重构。
15. 7-case、两个 fixture 的评测比单 calculator suite 更有覆盖，但仍是小型 scripted/offline 数据集，不能代表真实 Coding 任务，也不能证明不需要 search/Git。

## 6. 不允许虚构的项目事实

在对应里程碑通过前，不得声称：

- 上下文压缩在未指定固定数据集、baseline、样本数和 task-success delta 时节省了某个比例的 Token；
- Agent 有超出当前离线固定 suite 范围的基准集验证任务成功率；
- restricted test 在所有平台、所有内核配置下都提供完整容器级隔离；
- approved network、OCI image 生命周期或跨平台完整 container sandbox 已实现；
- 多 Agent 比单 Agent 更有效。
