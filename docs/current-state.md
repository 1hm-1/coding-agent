# 当前实现状态

> 基线日期：2026-09-17
> 已完成：M0、M1、M1.5、M2.1、M2.2、M2.3、M3.1、M3.2、M3.3、M4.1、M4.2、Phase 2 P2-M1/P2-M2
> 当前阶段：Phase 2 P2-M2 Layered Memory 与冻结 benchmark 检索优化已完成；P2-M3 尚未激活
> 当前附加门禁：Release/Evidence Hardening 已完成（文档、指标、Git/CI、coverage、类型检查、评测证据）。
> 固定发布基线：`v0.1.0`（复现命令与边界见 [`releases/v0.1.0.md`](./releases/v0.1.0.md)）。
> Phase 2 P2-D0 设计与 P2-M1 producer 已完成。除“尚未实现”章节外，本文只描述已经存在并通过测试的行为。

## 1. 已实现能力

### Runtime

- `AgentRuntime` 使用显式 handler map 和 `ALLOWED_TRANSITIONS` 推进状态。
- 当前状态：`CREATED`、`PREPARING_WORKSPACE`、`BUILDING_CONTEXT`、`CALLING_MODEL`、`DISPATCHING_TOOL`、`RECORDING_OBSERVATION`、`INTERRUPTED`、`WAITING_APPROVAL`、`RETRY_WAIT`、`COMPLETED`、`FAILED`。
- `step()` 执行一个状态动作；`run()` 只负责驱动到终态。
- 有 step、model call、tool call 三类预算。
- Runtime 失败会进入 `FAILED` 并尽量记录 `run_finished`；中断会先提交 `INTERRUPTED` checkpoint，未知写副作用会停在 `WAITING_APPROVAL`。

### Runtime IPC（P2-M1 已完成）

- 当前开发版本为 `0.2.0.dev0`；固定发布 `v0.1.0` 不包含 Runtime IPC。
- `protocol-info --protocol-version 1 --output json` 提供无副作用 capability discovery，stdout
  只包含一条 Schema-valid JSON record，并返回四份 producer-authority Schema 的实时 SHA-256。
- `run-headless --protocol-version 1 --request-file <absolute-private-path>` 验证大小、UTF-8 JSON、
  Schema、backend、capability、权限、source revision 与 attempt-root containment；Secret 值和语义未知
  extension fail closed。
- 内部 committed event 通过显式 allowlist/minimal-payload projector 输出连续 JSONL；绝对私有路径、
  Secret 和完整模型/工具输出不进入 stdout。合法执行恰有一个末尾 result，`COMPLETED` 映射
  `succeeded`，但不等同于外部 task oracle success。
- SIGINT 与 deadline 在 Runtime 安全边界协作取消；先提交 `INTERRUPTED` checkpoint，再输出
  `cancelled`/`timed_out` result，并验证活动 sandbox 子进程清理。SQLite 仍是恢复 authority。
- 稳定进程退出码为 invalid request=64、unsupported protocol=65、internal/output failure=70、
  private I/O=74；启动失败不污染协议 stdout。
- 当前公布 `structured_events`、`cooperative_interrupt`、`checkpoint_resume`、`scripted_backend`、
  `real_model_backend`。P2-M1 验收通过 111/111 默认 unittest、Runtime IPC goldens/vectors、Ruff、
  26 个配置范围源码文件 mypy、compileall、76.8% coverage、wheel/sdist、独立 wheel discovery 与
  calculator/todo smoke。

### Layered Memory（P2-M2 已完成）

- Working memory 继续由 session/messages/context 管理；新增的长期层只包含 episodic 与 semantic
  memory，scope 为 session/repository/user。Procedural memory 仍属于未来 P2-M3 Skill。
- `MemoryRecord` v1 保存 scope/kind/content、committed Runtime provenance、repository revision、
  confidence/expiry、status、supersedes、content hash 和 optimistic version。SQLite schema v4 的
  `memory_records`、`memory_events`、`memory_retrievals` 分别作为记录、生命周期和检索审计 authority。
- 所有写入先是 `proposed`；schema、journal provenance、scope ownership、revision、Secret、完整工具
  输出、宿主绝对路径、嵌入式指令和去重检查通过后，仍需显式 activate。支持 reject、stale、
  supersede 和 delete；delete 清空原文，只保留 tombstone/hash/audit。
- 确定性 lexical + metadata retrieval 对词形做小型归一，按 query/record coverage 加权、常见词
  降权、末尾信息词加权，并使用最低相关性、scope 稳定优先级和相对 score floor 动态截断；仍限制
  top-k/Token 并过滤 scope、revision、expiry、stale/deleted。没有 embedding、vector 或 RAG framework。
- S3.5 在不访问 Holdout 的前提下增加通用 term 边界标点规范化，保留 identifier 内部标点；L1/L2
  12-case 所有质量/Token 指标不变，自建 punctuation development recall `0/1→1/1` 且 negative
  injection 仍为 0。检索策略随后冻结，默认 Memory 路径仍关闭。
- `BudgetedContextBuilder` 只在显式配置 retriever/query factory 时增加 `memory` section；内容标记为
  不可信参考数据。模型只接收紧凑 notice 与内容列表；`context_built` manifest 仍记录 retrieval id、
  memory/schema/record version、score、完整 provenance、retrieval Token 估算、实际 Context Token 成本
  和最终是否注入；record 级实际成本使用与注入相同的 renderer 计算，unbounded preview 不重复写
  retrieval audit。
- 本阶段交付形态是 Python composition：调用方可将 `SQLiteMemoryStore`、`MemoryService`、
  `LexicalMemoryRetriever` 和 query factory 组装到 `BudgetedContextBuilder`；默认
  `AgentApplication` 与 `run-headless` 不创建、查询或注入 Memory，也不公布 Memory IPC capability。
- 已将 cold/warm Runtime benchmark 固定为 12 个 case：4 个明确相关、2 个无匹配、2 个词面相似
  但语义无关、1 个错误 user scope、1 个错误 repository/revision、1 个 stale/deleted 和 1 个
  诱导指令拒绝负例。同进程 before/after A/B 复现旧基线并验证候选：trusted task oracle 保持
  cold 8/12 → warm 12/12，relevant recall 1.0→1.0，precision 2/3→1.0，irrelevant injection
  1/3→0；scope/revision/stale-deleted 泄漏为 0，原始 3-case warm 3/3。retrieval cost 116→81，
  Memory context/额外模型 Token 803→130，下降 83.8%；after scripted model total 为
  2740→2870。Token per successful task after 为 cold 342.5、warm 239.17（仅 model），计入
  retrieval 后 warm 245.92。当前一轮实测 wall latency：before cold/warm mean 为
  `45.834164333731074ms`/`47.033260334198225ms`（`+1.1990960004671507ms`），after
  cold/warm mean 为 `45.834164333731074ms`/`45.95990916732262ms`
  （`+0.12574483359154698ms`）。检索 latency mean 为
  `0.056614917411934584ms`→`0.06059541647118749ms`
  （`+0.003980499059252907ms`）；延迟受本机调度影响，但回退没有从证据中删除。这不是
  Provider 收益证据。
  脱敏摘要见 [`docs/evidence/memory-cold-warm-2026-09-16.summary.json`](./evidence/memory-cold-warm-2026-09-16.summary.json)。
- 2026-09-18 对同一 12-case development suite 的补采 cold/warm 结果已脱敏保存于用户保管目录
  `p2-m2-memory-benchmark-2026-09-18/redacted-summary.json`，SHA-256 为
  `991ff28b84f708651ccb3bb4e85549f017f74cb31feffe039fcbb7f328b84889`。after 实测 task success
  为 cold `8/12`、warm `12/12`，relevant recall `1.0`，precision `1.0`，irrelevant injection
  `0`，无关行为变化率 `0`；retrieval Token 总计 `81`，Memory context Token 总计 `130`，
  scripted model total 为 cold/warm `2740/2870`，warm Token per successful task 为
  `239.16666666666666`（计入 retrieval 为 `245.91666666666666`）。本次保存运行的 wall latency
  mean 为 cold/warm `48.789092167377625ms`/`48.00096650069463ms`，retrieval latency mean 为
  `0.06890908480272628ms`。该 benchmark 使用 trusted deterministic oracle 和 renderer estimator，
  不代表真实 Provider 或模型收益，且不改变默认 Application/headless 关闭的结论。
- 上一轮收口门禁（加入 L3.5 v2 静态契约测试前）通过 139/139 默认 unittest（L3 后 137 个无回退，新增 2 个 live Memory A/B
  harness contract test）、四份既有 semantic golden、Ruff、34 个配置范围源码文件 mypy 与 compileall；
  最近一次完整 coverage 仍为 L3 前的 78.5% statement coverage、
  wheel/sdist、独立 wheel Memory import、calculator/todo smoke 与多任务 benchmark；本轮未运行
  真实 Provider。
- S2 复核发现并修复了 benchmark before renderer 下 record 级 Context Token 归因不一致；总 section
  Token、模型输入和既有 A/B 数字未受影响。复核结论为有条件通过：Memory 继续显式 opt-in，L3
  非同源 holdout 已完成但未达到 recall/injection 门槛；真实 Provider A/B 后才评估默认启用，
  P2-M3 仍未激活。
- L3 非同源 holdout 已固定在提交 `8ebc800` 的实现上，manifest SHA-256 为
  `3d4bffb06a19ee219534d4649d93e983e7ecd14cebfd90b84ae64feb1f7e2ed1`。18 个 case 分布为
  6 个 semantic relevant、4 个 hard negative、2 个 shared-pool competition、2 个 scope isolation、
  1 个 repository revision、1 个 stale/deleted、1 个无相关 Memory 和 1 个 prompt-injection 负例；
  三个共享池规模为 6/7/5，并额外复跑最初 `5298ba0` 的三任务共享池兼容 arm。首轮冻结结果为
  warm task success 13/18、relevant recall `0.6666666666666666`、precision
  `0.8333333333333334`、irrelevant injection `0.16666666666666666`；scope/revision/stale-deleted
  泄漏均为 0，无相关 Memory 行为变化为 0，manifest Token 与 renderer 一致，兼容 arm warm 3/3。
  因此本基线未达到 recall >= 0.85 和 irrelevant injection <= 0.15，不能把 L3 结果写成质量门禁通过。
  后续重复运行仅补采观测 latency：cold/warm mean 为 `41.255672772725426ms`/`42.08423031700982ms`，
  warm-cold mean `+0.8285575442843935ms`；retrieval mean `0.18361411154425392ms`。完整脱敏摘要见
  [`memory-retrieval-holdout-2026-09-16.summary.json`](./evidence/memory-retrieval-holdout-2026-09-16.summary.json)。
- S3 复核认定 L3 不是一例一池且包含 Runtime task result，但 task/memory 多为关键词近同构改写，
  deterministic oracle 直接持有 expected answer，故不能视为强非同源 Provider 证据。失败只分类，
  没有依据 L3 修改检索器；后续 development set 已明确为另一组 L1/L2 冻结 12-case 数据，未来
  算法修改必须先冻结全新的 Holdout v2。
- 新增显式 `evaluate-memory-live`：真实 Provider off/on pair 共用 model、任务、预算、源码指纹和
  trusted oracle，并交替 arm 顺序。Memory 来自先前完成且 journal event 可验证的 Runtime result；
  报告覆盖 success/completion、Token、retrieval、P50/P95 latency、工具与失败、first relevant action
  和安全不变量，同时拒绝 Secret/绝对路径且不保存 reasoning。当前仅有离线 harness contract test，
  尚无 live Provider 净收益结果；默认 Application/headless/IPC 仍未接入 Memory。
- L3.5 Holdout v2 已在 S3.5 算法冻结后完成唯一一次有效执行：正文仍由用户在仓库外保管，共享仓库
  保留 manifest/hash/冻结说明及 S3.6 脱敏 term-level 诊断，不保存原始 prose；20 个 case 分布在 4 个新任务领域、4 个共享 Memory pool，包含
  paraphrase、hard negative、冲突记忆、无记忆、scope/revision、stale/deleted 和 prompt-injection
  负例。manifest SHA-256 为
  d1d9c45d9d06aea211780fa1d6b3d9baf4154891f1a3344ce1ae1cb658907d6f；主 Holdout 不含原始
  5298ba0 三任务兼容 arm。algorithm `f1d03cf` 与 runner `e7287d2` 已写入 manifest，20/20
  case 有效；原始结果和脱敏 summary 仍在用户保管目录。
- 首轮 observation-only 实测：task success `0/20`，relevant recall `0.0`，precision `1.0`，
  irrelevant injection `0.0`，scope/revision/stale-deleted leakage `0`，无关 Memory 行为变化 `0`，
  prompt-injection policy bypass `0`，manifest Token mismatch `0`，compatibility warm `3/3`；
  retrieval Token `0`、renderer estimator model Token `5518`、cold/warm latency mean
  `1.7627652014198247ms`/`1.456806949863676ms`，Token per successful task 为 `null`。这不是
  真实 Provider 或模型收益证据，relevant recall 未达到 `0.85`，Memory 继续只提供显式 Python
  composition，默认 Application/headless/IPC 保持关闭。
- 本轮 L3.5 runner 收口实际通过 146/146 unittest、Ruff 与 git diff --check；三次无有效评价结果的
  基础设施失败均保留 failure record。完整脱敏记录见
  [`memory-retrieval-holdout-v2-2026-09-18.md`](./evidence/memory-retrieval-holdout-v2-2026-09-18.md)。
- S3.6 对已消费 v2 做离线零召回漏斗：12/12 relevant target 均通过 scope/status/expiry/revision，
  2 个无 informative lexical overlap，10 个 raw score 为 `0.05931280`–`0.30628391` 且低于 `0.60`
  绝对阈值；没有候选到达 relative floor、Token 或 top-k。runner task fallback、label 和 aggregate
  metric 未发现缺陷，三次基础设施失败也未污染最终 store/audit。结论为 **Lexical ceiling**：停止
  堆 alias，v2 只保留诊断证据、另建 development set，算法变化前冻结 v3；检索算法和默认 Memory 路径均未修改。
  完整逐案证据见
  [`s3-6-memory-holdout-v2-zero-recall-2026-09-18.md`](./evidence/s3-6-memory-holdout-v2-zero-recall-2026-09-18.md)。
- L3.7 新增 [`memory_retrieval_backend_development.json`](../examples/memory_retrieval_backend_development.json)，明确标记为
  `development_data`：40 个实际 case、8 个代码仓库/任务领域、8 个共享 Memory pool，分类为
  10 zero-overlap paraphrase、10 low-overlap paraphrase、8 同主题 hard negative、4 冲突/过时、
  4 无相关 Memory 和 4 scope/revision/stale/injection 边界。每条 query 有 relevant Memory IDs，
  case 与 record 都提供 fact_type/entities/concept_keys/repository_component/validity/revision
  结构化 ground truth；metadata 不携带完整答案、工具指令或权限信息。v2 的 20 个 case 以
  manifest/hash 引用保留为 reference-only development 子集，正文仍在用户保管位置，不创建 Holdout v3，
  且本项未修改检索算法。S3.7 spike 现以 label-free request projection 比较 frozen lexical、content
  BM25 和 structured BM25，并为真实 embedding 留显式 unavailable adapter。development recall 为
  `0.375/0.7083/0.7083`，irrelevant injection 为 `0/0.6531/0.6731`；三次 deterministic digest
  一致，但 BM25 误注入阻断生产选择。`retrieval.py` 与 `f1d03cf` 文件 hash 一致，默认 Memory 路径
  保持关闭。完整证据见
  [`s3-7-memory-retrieval-backend-spike-2026-09-18.md`](./evidence/s3-7-memory-retrieval-backend-spike-2026-09-18.md)。
- S3.7 收口通过 161/161 unittest（四份 semantic golden 不变）、Ruff、36 文件 mypy、compileall
  与 `git diff --check`；未运行 coverage，最近一次 78.5% 证据不变。
- S3.5 算法冻结收口通过 143/143 unittest（含四份 semantic golden）、Ruff、34 文件 mypy、
  compileall 与 git diff --check；L1/L2 Runtime benchmark 的 recall/injection/retrieval/model Token、
  leakage、兼容 arm 和 manifest attribution 均无回退。未运行 coverage，最近一次 78.5% 证据不变。

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
- Eval 可用重复 `--case-id` 固定正常任务子集，并用 `--ab-variants` 选择压缩开/关配对；
  `task_metrics` 单独给出正常任务的 total/mean/P50/P95，`total_tokens` 包含摘要器 Token，
  `end_to_end_latency_ms` 包含 workspace、Agent 和 oracle。A/B 在每个 pair 内交替先运行的 arm，
  并给出正常任务配对汇总。

### Hardening evidence

- 四份 semantic golden：成功、测试失败后恢复、权限拒绝、Runtime failure。
- `todo_cli` 展示一次 `false → true` 的测试恢复轨迹。
- Harness 对测试超时和 handler 未预期异常有测试。
- 固定 `v0.1.0` 有 93 个默认测试；P2-M1 后为 111 个，P2-M2 冻结基线为 134 个，检索优化后为 136 个，加入 L3 holdout contract test 后为 137 个，S3 live paired harness 后为 139 个，L3.5 manifest 后为 142 个，S3.5 算法冻结后为 143 个，加入 Holdout v2 runner 后为 146 个，S3.7 development suite 后为 153 个，candidate spike 后当前开发树为 161 个，在当前
  capability probe 成功的环境中全部通过。`tests/live_provider_smoke.py` 为凭据门控的显式测试，
  不计入默认 discovery；能力受限 runner 会对 7 个 native-only case 显式 skip。
- SQLite M2.1 测试覆盖 migration 幂等/未来版本拒绝、snapshot round-trip、原子 mutation、乐观冲突、提交前回滚和 DB→JSONL 重建。
- calculator smoke 产生 48 条连续事件；todo fixture 产生 72 条连续事件并保持 `false → true`。
- Ruff 强制基线 `E4/E7/E9/F` 仍显式写入 `pyproject.toml`；已使用 `uv` 安装 Ruff 0.16.6，`ruff check src tests examples/todo_cli examples/mini_repos examples/memory_cold_warm_benchmark.py examples/memory_retrieval_holdout.py` 通过。
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
- 简历指标实验先完成无凭据的确定性校准，随后以当前 Provider 可用的 `deepseek-flash` 完成
  live 25-run 和 10-pair compression A/B。稳定性实验 25/25 valid、基础设施失败 0、端到端
  24/25（96%），唯一失败是固定 search task 的 `tool_budget_exhausted`；P50/P95 端到端延迟为
  5.34/9.04 秒，总 Token 345,551。A/B 两臂均 10/10 valid，budgeted/compressed 端到端为
  9/10→10/10，但 compressed 总 Token 增加 339,864、每对平均延迟增加 6.49 秒，因此不能宣称
  compression 节省 Token。计划模型 `deepseek-v4-flash` 已不在 Provider model list 中，且运行时
  worktree 非 clean；精确 content/artifact hash 与限制见 [`resume-benchmark.md`](./resume-benchmark.md)。
- Release/Evidence Hardening 已加入 `RESUME_STARTED` recovery event、70% coverage 门槛（M5.1
  的 93 个默认测试启用 subprocess/multiprocessing 合并后实测 75.6%）、23/33 源码文件的 mypy
  检查、`uv.lock`、GitHub Actions CI、wheel/sdist build、calculator/todo
  scripted smoke 和凭据门控的 live provider smoke；CI 会先报告 native capability，能力不足
  时 skip native-only case 和 sandbox-dependent eval，不把 fail-closed 结果当作 native security
  通过。
- GitHub Actions run `34035706601` 已对提交 `cf82f3c` 完成首次完整托管 CI，Python 3.10 和
  3.11 quality jobs 均为 success；安装、Ruff、mypy、capability report、coverage tests、
  compile、两个 smoke 和 offline Eval steps 在公开 API 元数据中均为 success。匿名 job log
  下载返回 403，因此该证据不额外声称 native-only case 一定执行而非按 workflow 规则 skip。

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
.venv/bin/ruff check src tests examples/todo_cli examples/mini_repos examples/memory_cold_warm_benchmark.py examples/memory_retrieval_holdout.py
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
PYTHONPATH=src python3 -m compileall -q src tests examples/todo_cli examples/mini_repos examples/memory_cold_warm_benchmark.py examples/memory_retrieval_holdout.py
```

```bash
PYTHONPATH=src .venv/bin/python examples/memory_cold_warm_benchmark.py
```

```bash
PYTHONPATH=src .venv/bin/python examples/memory_retrieval_holdout.py
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
- 多 Agent、终端交互层、Skill/MCP、procedural memory、向量检索或 RAG；这些能力已有 Phase 2
  设计但代码未实现；episodic/semantic memory 已由 P2-M2 实现；
- Agent Platform consumer adapter；本仓库只交付 producer contract、Schema、golden 与可复用
  vectors。`v0.1.0` 不支持 IPC；P2-M1 实现只存在于 `0.2.0.dev0` 开发树。

### Phase 2 当前边界

- [`v2-product-architecture.md`](./v2-product-architecture.md) 固定未来模块边界：当前 `AgentRuntime` 保持单任务执行内核，其上再增加产品层、协调器、记忆、Skill 和能力网关；
- [`protocol/runtime-ipc-v1.md`](./protocol/runtime-ipc-v1.md) 固定 Platform 只能通过版本化 headless IPC 使用 Runtime，不能读取内部 SQLite 或私有 trajectory；
- `protocol/v1/*.schema.json` 是 request、capabilities、event envelope 和 terminal result 的
  producer authority；四类 document 均由自动测试验证，vectors 随 wheel/sdist 发布；
- 初始多 Agent 拓扑计划采用 Manager/Explorer/Implementer/Reviewer，并坚持单写者 workspace 规则；
- P2-M1—P2-M6 必须逐阶段实现和验收，不允许一次性把设计目录全部脚手架化；
- P2-M1 与 P2-M2 已冻结；P2-M2 Memory 仍只通过显式 Python composition 提供，默认
  `AgentApplication`、`run-headless` 与 Runtime IPC 不创建、查询或注入 Memory；P2-M3
  Profiles/Skill Runtime 是下一候选，但尚未激活。

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
10. 项目已初始化 Git 仓库，M5.1 提交 `cf82f3c` 已推送到 `origin/main`，GitHub Actions
    run `34035706601` 在 Python 3.10/3.11 上成功；公开 API 只提供 run/job/step 状态，匿名
    下载详细日志返回 403。评估器仍只对每个 isolated fixture 内部初始化的 Git baseline
    执行 changed-path diff。
11. 当前 coverage 是 statement coverage，发布门槛为 70%；coverage 不等同于安全或任务成功率证明。
    CLI 通过 subprocess smoke 纳入合并数据；namespace runner 为保持 sandbox 环境 allowlist 不注入
    宿主 coverage hook，当前仍显示 0%，其行为证据来自 native integration/security tests。
12. mypy 当前覆盖 memory、models、tools、context、domain、workspace、command profiles、evaluation、
    sandbox 和 P2-M1 protocol，共 34 个配置源码文件；其余 Runtime/Application/Persistence/CLI/Compression 历史代码
    尚未达到全仓类型检查标准（本次全仓扫描剩余 75 个错误，集中在 6 个文件）。
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
16. 简历 benchmark 的 scripted 校准不是模型能力证据；live 数字只适用于记录的五个小型
    fixture、`deepseek-flash` 和 worktree content snapshot。压缩 A/B 没有产生 Token 节省，
    不得将 10% 的小样本 Runtime completion 差异外推成一般收益。
17. P2-M2 Memory benchmark 使用 12 个冻结 case 的确定性 trusted oracle 和 scripted Token
    估算；检索/Context A/B 达到 recall=1.0、precision=1.0、irrelevant injection=0，额外模型
    Token 从 803 降到 130。当前实测检索 mean 从 `0.056614917411934584ms` 增至
    `0.06059541647118749ms`，该回退已保留。随后新增的 L3 非同源 18-case holdout 固定在
    `8ebc800`，首轮 relevant recall=`0.6666666666666666`、irrelevant injection=
    `0.16666666666666666`，未达到门槛；该失败结果已冻结，不能据此宣称真实模型净收益或启用
    默认 Application/headless Memory。

## 6. 不允许虚构的项目事实

在对应里程碑通过前，不得声称：

- 上下文压缩在未指定固定数据集、baseline、样本数和 task-success delta 时节省了某个比例的 Token；
- Agent 有超出当前离线固定 suite 范围的基准集验证任务成功率；
- restricted test 在所有平台、所有内核配置下都提供完整容器级隔离；
- approved network、OCI image 生命周期或跨平台完整 container sandbox 已实现；
- 多 Agent 比单 Agent 更有效。
