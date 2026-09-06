# 当前实现状态

> 基线日期：2026-09-06
> 已完成：M0、M1、M1.5、M2.1、M2.2、M2.3、M3.1、M3.2、M3.3、M4.1、M4.2  
> 当前阶段：M5.1 最小只读 `search_files` 已由 Eval 证据批准并实现
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
- 非法响应、HTTP 错误、timeout、rate limit 和脚本耗尽映射为分类 `BackendError`；Provider
  usage 字段严格要求非负整数。Backend 意外抛出非 `BackendError` 时也会以 `protocol_error`
  原子关闭 model journal，不遗留 `running`；retry/fallback 由 Runtime 按持久 policy 驱动。
- 每次接收到的 `ModelRequest` 会保存在 backend 实例中，供测试验证 observation 回填。
- provider API key 只从对应环境变量读取，不写入 request journal、event、JSONL 或错误文本。
- OpenAI-compatible adapter 支持显式发送 `thinking: disabled`；当前 Runtime 不持久化或回传
  Provider 的 `reasoning_content`，因此 DeepSeek 的工具循环必须关闭 thinking。

### Tools

- 有 `read_file`、`edit_file`、`search_files`、`restricted_test`、`run_command`。
- 三个文件工具的模型 schema/系统提示明确要求 workspace-relative path、禁止 `/` 开头和
  `..`，并要求优先使用 `repository_snapshot.file_paths` 中的精确路径。
- 系统提示在实现位置未知但任务提供特征 symbol/literal/key/error 时要求先用 `search_files`，
  避免顺序盲读；该提示经紧预算 context/compression 回归和真实 Provider A/B 验证。
- `search_files` 只做大小写敏感的 UTF-8 字面量搜索，复用 `READ` 权限和 `READ_ONLY`
  恢复模式；不支持 regex/glob/Shell/Git/网络，并限制扫描文件数、字节数、结果数、单文件
  大小和 wall time。
- `ToolHarness` 统一执行注册查找、受限 JSON Schema 校验、权限检查、deadline、异常映射、输出限制和 audit callback。普通 handler 默认在可杀死的 Linux worker 进程组中运行；sandbox
  工具由 SandboxExecutor 强制 wall timeout，二者均不再只做事后超时判定。
- 权限枚举的真实名称是 `READ`、`WRITE`、`EXECUTE_TEST`、`EXECUTE_COMMAND`。
- `restricted_test` 只接收可信 profile 名称；当前默认 profile 是 `python_unittest`。
- `run_command` 只接收可信 command profile、结构化 `argv` 和受限相对 `cwd`；不接受
  shell 字符串、shell interpreter 或 profile 外 executable。
- 没有 `run_shell` 或 Git inspection 工具。

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
- `ExecutionSpec`、`ExecutionResult`、capability snapshot、native runtime sample fingerprint、
  limits、execution id、output truncation 和 cleanup 状态通过 `ToolResult` 进入
  `tool_call_finished`；公共事件投影不保存完整 workspace 路径或环境值。
- native backend 的 fingerprint 只覆盖声明的运行时样本，不是完整 rootfs digest 或已拉取的 OCI image；M4.1 不提供
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
- recent history 中的 assistant tool-call turn 与其全部 tool result 作为原子组裁剪；完整组无法适配预算或历史组不完整时 fail closed，避免向 Provider 发送不合法的 tool-call 消息序列。
- 超过 `0.85 * input_budget` 才尝试有界 compression，目标为 `0.65 * input_budget`；schema/required-fact 校验失败会记录 `compression_rejected` 并保留原始历史。
- Repository snapshot 只从 isolated workspace 构建，包含有上限文件列表、Git diff summary、已读文件 hash、workspace revision 和最近测试摘要。
- `evaluate` 读取严格版本化 JSON suite，按 case/repetition 创建新 workspace，运行可信 test/file/diff/result oracle，分别统计 task success 与 Runtime completion。
- Eval case 显式标记 `task` 或 `negative_control`；当前固定 suite 为 10 个正常任务和 4 个
  负控制。能力主指标是正常任务的 `oracle_success_rate` 和
  `end_to_end_success_rate = oracle_success && runtime_completed`；负控制明确排除在能力分母
  外，旧 `task_success` 字段仅作为 schema-v1 兼容别名。
- `evaluate` 支持显式 provider override，在不修改固定 scripted manifest 的情况下对同一
  组 fixture/oracle 运行真实模型 baseline；实际 backend 配置会写入 manifest snapshot，
  secret 仍只从环境变量读取。
- Eval 报告区分 `tool_attempts`、通过准入边界的 `tool_executions`、
  `invalid_tool_calls` 和 `repeated_failure_batches`，并继续记录 Token、延迟、failure taxonomy、
  recovery、permission 和 source invariant；比较投影排除随机 session ID/trace path。

### Hardening evidence

- 四份 semantic golden：成功、测试失败后恢复、权限拒绝、Runtime failure。
- `todo_cli` 展示一次 `false → true` 的测试恢复轨迹。
- Harness 对测试超时和 handler 未预期异常有测试。
- 当前测试数量：93；在当前 capability probe 成功的环境中全部通过；`tests/live_provider_smoke.py` 为凭据门控的显式测试，不计入默认 discovery。能力受限 runner 会对 7 个 native-only case 显式 skip。
- SQLite M2.1 测试覆盖 migration 幂等/未来版本拒绝、snapshot round-trip、原子 mutation、乐观冲突、提交前回滚和 DB→JSONL 重建。
- calculator smoke 产生 48 条连续事件；todo fixture 产生 72 条连续事件并保持 `false → true`。
- Ruff 强制基线 `E4/E7/E9/F` 仍显式写入 `pyproject.toml`；已使用 `uv` 安装 Ruff 0.16.6，`ruff check src tests examples/todo_cli` 通过。
- M2.2 recovery tests 覆盖 interrupt 后复用已保存模型响应、四个工具 crash window、edit hash reconciliation、三种人工 resolution、lease takeover 和 source/workspace resume rejection。
- M2.3 adapter tests 覆盖 OpenAI-compatible/Anthropic text、tool call、usage、Unicode、HTTP error、protocol error、retry/fallback 和 secret 不落盘；用户已用 DeepSeek 完成 opt-in live API smoke，并在上下文修复后完成 3 次探索性 live Eval。
- M3.1/M3.2 tests 覆盖 exact/fallback counter、unknown model、section boundary/hard retention、tool-call group preservation、deterministic manifest、summary round-trip、stale invalidation、required-fact rejection、compression fallback 和 raw-event preservation。
- M3.3 tests 覆盖 strict manifest/containment、fresh repetition、test/file/diff/result oracle、eval infrastructure failure 分类、task/runtime 分离、recovery suite、指标聚合和 paired A/B。
- M4.1 tests 覆盖 capability fail-closed、namespace workspace/rootfs/secret/network/symlink/proc/device
  边界、wall/CPU/memory/PID/storage/output 限制、子进程清理、并行 session 和 SQLite
  tool-observation recovery。
- M4.2 tests 覆盖结构化 argv schema、executable allowlist、cwd containment、profile
  策略、approval fail-closed、非零退出 observation、direct-argv native 边界和
  non-idempotent crash recovery。
- 历史 M5 评测门禁曾扩展为 13-case、6-fixture 的 `budgeted` suite：valid runs=13、
  `infrastructure_failure=0`、`source_invariant_rate=1.0`、`runtime_completion_rate=0.9231`、
  `task_success_rate=0.6923`、`recovery_rate=1.0`；任务失败均属于预设的 scripted/oracle
  场景，budgeted 报告另记录了 3 次有界的 `compression:summarizer_unconfigured` fallback，
  没有真实模型 failure coverage。
- 固定 suite 现扩展到 14-case、7-fixture。live compressed 定向验证使用真实 Provider 摘要器、
  最多两次有界压缩和完整嵌套 summary schema；3 次运行不再出现 context/schema 失败，oracle
  3/3、端到端 2/3，剩余一次是模型重复读取导致 `tool_budget_exhausted`。
- M5.1 完成后的 `budgeted` 离线验收为 14/14 valid、基础设施失败 0；10 个正常任务
  oracle/Runtime/end-to-end 均为 10/10，4 个负控制 observed failure 为 4/4，source invariant
  和 recovery 均为 100%。
- 路径契约修复后，pipeline 定向 10 次没有 invalid path、重复失败批次或工具预算耗尽。原始
  9/10 oracle 中唯一失败是 `lower().strip()` 与 `strip().lower()` 的等价实现，已用
  `contains_any` 修复过窄 oracle。
- 独立 `search_lab` 在固定 8-call 预算下，no-search 端到端 0/10；最小只读
  `search_files` 候选提升到 6/10、oracle 8/10，直接 read 从 75 降至 49、invalid calls 从
  5 降至 0，但输入 Token 从 73,073 增至 147,027。该证据批准 M5.1，同时保留仍有 4 次失败
  和 Token 成本上升的限制。
- 独立 search suite 随后扩大到 3 个不同结构的仓库。当前提示与 search-first 提示各运行
  15 次 DeepSeek：端到端 12/15→13/15、oracle 12/15→14/15、模型调用 79→73、工具尝试
  106→101、输入 Token 234,638→207,658（-11.5%），first-search 中位位置 4→1。候选仍有
  2 次 8-call 耗尽，因此保留提示但不提高预算。
- `task_runtime.remaining_budgets` 过去错误地在每轮重复 policy 初始上限；现由 Session 已使用
  step/model/tool 计数计算真实剩余值，并保持旧 `ContextBuildInput` 投影缺字段时按 0 兼容。
  同时加入命中后停止无关读取、预留一次 restricted test 的紧凑指引。后续 15 次 DeepSeek
  端到端/Runtime 为 14/15，oracle 14/15，预算耗尽从 2 降为 1、工具尝试 101→100；输入
  Token 增加 9.0%、模型调用 73→78，因此只认定 Runtime/预算语义改善，不宣称总体效率改善。
- 非 search capability holdout 复用四类未参与三仓库提示 A/B 的正常任务，scripted 4/4；
  DeepSeek 每 case 3 次共 12/12 端到端成功，基础设施失败、无效调用、权限违规、重复失败批次
  和预算耗尽均为 0。所有运行都执行 edit 和 restricted test，没有形成 Git、patch/edit、
  依赖安装或其他 M5.2 能力的 failure coverage。
- 新增 fixture 覆盖跨文件定位探针、多文件 bug 后测试失败继续定位、指定文件 changed-path
  约束和长历史 compression；compressed variant 的 long-history case 记录
  `compression_input_tokens=220`、`compression_output_tokens=70`，没有 compression rejection。
- Passthrough/budgeted 单变量 A/B 各 13 个 paired runs，task success、Runtime completion、
  source invariant、tool calls 和 Token 完全一致；单次 latency 差异不作为结论。用户已完成
  一次 DeepSeek live smoke，并在上下文修复后完成 3 次 13-case 探索性 live Eval：39/39 valid、
  基础设施失败=0、task success=0.8205（32/39）、Runtime completion=0.8462（33/39）、
  source invariant=1.0、permission violations=0，单次 task success 为 11/13、10/13、11/13。
  该结果混入 scripted 负控制，不能作为最终真实模型成功率；修复前暴露的 tool-call 组截断问题
  已改为原子组裁剪和 fail-closed，修复后不再出现 Provider `invalid_request`。`budgeted` 长历史
  case 仍因没有 summarizer 且硬保留内容略超预算而安全失败；后续 compressed 定向运行已经
  消除该 context failure，并为 `search_files` 形成独立 A/B 证据。Git 工具仍无对应证据。
- Release/Evidence Hardening 已加入 `RESUME_STARTED` recovery event、70% coverage 门槛（M5.1
  的 93 个默认测试启用 subprocess/multiprocessing 合并后实测 75.6%）、23/33 源码文件的 mypy
  检查、`uv.lock`、GitHub Actions CI、calculator/todo
  scripted smoke 和凭据门控的 live provider smoke；CI 会先报告 native capability，能力不足
  时 skip native-only case 和 sandbox-dependent eval，不把 fail-closed 结果当作 native security
  通过。

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
- M5.1 最小只读 `search_files` 已批准并实现；Git inspection、patch/edit 增强和依赖准备仍
  未获 failure coverage 批准；
- 已完成 DeepSeek budgeted 探索、compressed 定向、search A/B、budget-aware follow-up 和
  capability holdout，并保存脱敏摘要；这些仍是小型本地 fixture 证据，不是外部真实仓库基准、
  托管 CI 历史或生产成功率。
- 多 Agent、UI、消息平台、Skill 或 RAG。

## 5. 已知限制与技术债

1. 兼容用 `JsonlEventStore.append()` 每次追加前重新读取整个 session trace，长轨迹仍为 O(n²)；默认 SQLite 主写路径已不再依赖它。
2. M2.2 只保证状态边界安全中断和规定 crash window 的可解释恢复；不能安全抢占任意 Python 文件写入，也不宣称未知副作用 exactly-once。
3. M4.1/M4.2 的 native backend 只在 capability probe 成功的 Linux 环境启用；其他平台或缺少
   namespace/mount 能力时 fail closed。它不是抵御内核/隔离运行时漏洞的完整安全边界。
4. source fingerprint 仍是运行后不变量证据，不替代 workspace mount；M4.1 的测试代码
   只能看到 session workspace，但 workspace 内部仍可能被测试代码任意修改。
5. M4.1/M4.2 使用 native runtime sample fingerprint，不是完整 rootfs digest，也不包含 OCI image 构建、SBOM、漏洞
   扫描或依赖安装流程；M4.2 elevated profile 只做 approval fail-closed。
6. Runtime 和 backend 是同步调用。M2 的中断先保证状态边界安全，不应在没有取消语义前假装支持任意时刻抢占；sandbox timeout/崩溃清理已验证。
7. 自研 JSON Schema 只支持内置工具使用的子集，不是完整 Draft 实现。
8. `COMPLETED` 表示 Runtime 正常收到 final answer，不代表任务成功；M3 Eval 已用 oracle 独立评分，但当前固定 suite 只提供离线描述性结果，不代表生产成功率。
9. 当前只支持 Linux/POSIX 进程组终止逻辑；Windows/macOS 等价 sandbox 不属于现阶段验收范围。
10. 项目已初始化 Git 仓库，初始提交 `69a16c6` 已推送到 `origin/main`，并加入 GitHub
    Actions CI 配置；当前仍没有已托管 CI 运行记录，评估器只对每个 isolated fixture 内部
    初始化的 Git baseline 执行 changed-path diff。
11. 当前 coverage 是 statement coverage，发布门槛为 70%；coverage 不等同于安全或任务成功率证明。
    CLI 通过 subprocess smoke 纳入合并数据；namespace runner 为保持 sandbox 环境 allowlist 不注入
    宿主 coverage hook，当前仍显示 0%，其行为证据来自 native integration/security tests。
12. mypy 当前覆盖 models、tools、context、domain、workspace、command profiles、evaluation 和
    sandbox，共 23/33 个源码文件；其余 Runtime/Application/Persistence/CLI/Compression 历史代码
    尚未达到全仓类型检查标准（当前全仓扫描剩余 76 个错误，集中在 6 个文件）。
13. live provider smoke 已提供手动 workflow；本地首次 DeepSeek 尝试已到达 Provider，但因
   smoke 原先只有 16 个输出 token，最终 `content` 为空而失败。现已增加输出预算，并支持
   DeepSeek `thinking: disabled` 配置；adapter smoke 已成功，修复上下文组裁剪后也已完成 3 次
   `budgeted` live Eval。后续已用真实 Provider 摘要器运行 compressed 定向验证；该小样本仍
   不代表通用 Coding 成功率。
   GitHub-hosted runner 的 native capability 也可能不足；native-only case 会显式 skip，相关
   sandbox-dependent eval 也会跳过，不能以此替代具备能力环境的 M4.1/M4.2 安全证据。
14. `runtime.py`、`persistence.py`、`evaluation.py` 仍是高集中度大模块；本轮只记录维护风险，不做无验收收益的拆分重构。
15. 当前固定 14-case/7-fixture suite 与独立 3-repository search suite 比单 calculator 更有
    覆盖，但仍是小型 scripted/live synthetic 数据集；不能代表真实 Coding 任务，也不能证明
    不需要 Git 或其他能力。

## 6. 不允许虚构的项目事实

在对应里程碑通过前，不得声称：

- 上下文压缩在未指定固定数据集、baseline、样本数和 task-success delta 时节省了某个比例的 Token；
- Agent 有超出当前离线固定 suite 范围的基准集验证任务成功率；
- restricted test 在所有平台、所有内核配置下都提供完整容器级隔离；
- approved network、OCI image 生命周期或跨平台完整 container sandbox 已实现；
- 多 Agent 比单 Agent 更有效。
