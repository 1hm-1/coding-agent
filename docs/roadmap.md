# 开发路线图

> 路线图状态基线：2026-09-05  
> 当前完成：M0、M1、M1.5、M2.1、M2.2、M2.3、M3.1、M3.2、M3.3、M4.1、M4.2、Release/Evidence Hardening  
> 当前实施项：**M5.1 最小只读 `search_files` 已完成；其余能力继续按证据门控**

路线图用“可验证门禁”而不是代码量定义完成。每个里程碑必须保持已有回归基线，新增能力必须同时提供成功、失败和恢复证据。

## 1. 状态总览

| 阶段 | 状态 | 证明的问题 | 主要证据 |
|---|---|---|---|
| M0 Architecture Baseline | 已完成 | 要构建什么，边界在哪里 | architecture/module/structure/roadmap |
| M1 Deterministic Vertical Slice | 已完成 | 完整任务能否经 FSM 和 Harness 跑通 | vertical slice + E2E tests |
| M1.5 Engineering Hardening | 已完成 | 失败是否可控、轨迹是否可验证 | fault injection/replay/golden/demo |
| M2.1 Persistence Foundation | 已完成 | 状态和事件能否原子持久化 | SQLite contract + rollback/export tests |
| M2.2 Interrupt/Resume | 已完成 | 中断后能否安全继续 | crash-window/reconciliation tests |
| M2.3 Real Model Adapters | 已完成 | 真实 Provider 能否在统一边界内工作 | adapter/retry/fallback contract tests |
| M3 Context & Evaluation | 已完成 | 长任务上下文和效果能否被量化 | context/compression tests + fixed eval suite/report |
| M4 OS Isolation | 已完成（M4.1/M4.2） | 不可信执行能否受 OS 强边界约束 | namespace/escape/network/resource + structured argv tests |
| Release/Evidence Hardening | 已完成 | 交付证据、质量门禁和评测范围是否可复核 | Git/CI/coverage/mypy/live-smoke/eval evidence |
| M5 Capability Expansion | M5.1 完成，其余条件阶段 | 哪些新工具真正提高任务覆盖率 | eval-driven decision record |

## 2. 全局里程碑门禁

任何阶段标记完成前必须满足：

- 需求被写成可执行或可审计的 acceptance criteria；
- 主路径、预期失败、超时/中断等关键异常都有测试；
- 原有全量测试和 semantic golden 继续通过，或存在经过批准的版本化迁移；
- 轨迹足以解释运行结果，错误不只存在于 stderr；
- 文档区分“已实现”和“计划”，`current-state.md` 已同步；
- 未把 source repository 变成写入目标；
- 没有为了过测试删除失败样本、golden 或指标分母中的失败 run。

## 3. M0：Architecture Baseline（已完成）

交付：

- 目标架构与安全边界；
- 模块依赖和责任边界；
- 当前/未来仓库结构；
- 分阶段路线图和开发原则。

完成标准：新开发者能说清 Runtime、Harness、Workspace、Persistence、Context 和 Eval 的边界，以及为什么 M1 不提供通用 Shell。

## 4. M1：Deterministic Vertical Slice（已完成）

范围：

- 显式 FSM 和允许迁移；
- `ScriptedBackend`；
- 独立复制 workspace；
- 统一 Tool Harness；
- `read_file`、`edit_file`、`restricted_test`；
- JSONL trajectory 和 replay；
- read → edit → test → final 的 E2E。

验收结果记录在 [`m1-vertical-slice.md`](./m1-vertical-slice.md)。M1 的 OS 隔离只到应用层路径防护，不能宣传为安全 sandbox。

## 5. M1.5：Engineering Hardening（已完成并长期保留）

M1.5 不是临时脚手架。它建立的 fault injection、replay validation、semantic golden 和真实小仓库 fixture 将作为后续所有阶段的回归门禁。

当前证据：

- tool permission denial 进入结构化失败路径，不导致进程 crash；
- 测试 `passed=false` 会作为 observation 返回，backend 可以二次修复；
- handler exception、timeout、非法 backend response 有稳定分类；
- replay 重建 final state、metrics、tool/test summary 并做一致性检查；
- 四份 golden 覆盖 success、recovery、permission denial、runtime failure；
- todo fixture 展示 source unchanged、workspace modified、false → true。

详细记录见 [`m1.5-engineering-hardening.md`](./m1.5-engineering-hardening.md)。

## 6. M2：Persistent Runtime and Model Boundary

M2 必须按三个可独立验收的子阶段推进。完整接口、表结构和 crash matrix 见 [`m2-implementation-plan.md`](./m2-implementation-plan.md)。

### 6.1 M2.1 SQLite Persistence Foundation（已完成）

只做：

- migration v1；
- sessions/messages/events/checkpoints 的最小持久结构；
- state + checkpoint + event 原子提交；
- optimistic state/version conflict；
- committed events 导出当前 JSONL envelope；
- 删除 JSONL 后从 DB 重建相同 semantic projection。

不做：resume、真实 Provider、context compression、eval batch、容器。

退出条件：

- migration 首次/重复执行/未知高版本行为有测试；
- round-trip 和 crash-before-commit rollback 通过；
- DB sequence 连续且并发冲突显式失败；
- JSONL 不再是恢复事实来源；
- 当前 30 个测试和四份 golden 不回归。

验收结果（2026-09-04）：30 个 unittest 全部通过；SQLite migration version
为 1；calculator/todo smoke 通过，todo 测试结果保持 `false → true`；删除
JSONL 后 replay 仍读取 SQLite，`export-trace` 可重建 projection。

### 6.2 M2.2 Checkpoint, Interrupt and Resume

范围：

- versioned `RuntimeSnapshot`；
- interrupt request 与安全状态边界；
- `INTERRUPTED`、`WAITING_APPROVAL`；
- model/tool intent-result journal；
- read/repeatable/reconcilable/non-idempotent 恢复分类；
- workspace/source identity 验证；
- crash window fault injection。

退出条件：在每个定义的持久边界中断后，恢复结果要么等价于不中断执行，要么进入明确等待/失败状态；不能盲目重复不确定写操作。

状态：已完成（2026-09-05）。`INTERRUPTED`、`WAITING_APPROVAL`、versioned
checkpoint、lease、model/tool journal 和 edit reconciliation 已接入；47 个
unittest 覆盖四个 tool crash window、已保存 model response 复用、未知写操作三种
resolution、lease takeover 与 source/workspace resume rejection。

### 6.3 M2.3 Real Model Adapters and Resilience

范围：

- OpenAI-compatible 与 Anthropic adapter；
- Provider response/tool-call/usage 归一化；
- retryable、rate limit、auth、invalid request、protocol error 分类；
- 有界 exponential backoff + jitter；
- 仅针对基础设施错误的显式 fallback；
- 脱敏日志和可选 live smoke。

退出条件：默认测试完全离线；同一 adapter contract fixture 验证两种 Provider；凭据不进入 DB/trace/error；retry/fallback 次数与原因可 replay。

状态：已完成（2026-09-05）。OpenAI-compatible 与 Anthropic adapter、统一 HTTP
错误分类、持久 retry/backoff、受控 fallback 和脱敏已通过离线 contract tests。
M2.3 完成时尚未配置 Provider 凭据；后续用户已使用 DeepSeek 完成一次 opt-in live smoke，
多次真实 baseline 不属于 M2.3 的离线 contract 验收。

## 7. M3：Context Engineering and Evaluation（已完成）

详细接口、数据结构、指标公式和 checklist 见 [`m3-implementation-plan.md`](./m3-implementation-plan.md)。

M3 已按三个可独立验收的子阶段完成；实现保留在单文件模块中，避免无必要的
package 搬迁。M3 固定证据仍使用离线 ScriptedBackend；后续真实 Provider smoke 不改变该
离线验收边界。

### 7.1 M3.1 Context Budget Baseline（已完成）

- 统一 token accounting 接口；
- system/task/repo/summary/recent section budget；
- 高水位触发；
- deterministic context manifest；
- passthrough 与 budgeted builder A/B 基线。

证据：`context.py`、`domain.py`、`workspace.py` 和 `tests/test_m3_context.py`；全量
回归保持通过。

### 7.2 M3.2 Compression with Lineage（已完成）

- 结构化摘要 schema；
- source event range/hash 和 workspace revision；
- stale fact invalidation；
- required-fact retention 测试；
- 摘要失败时可观察 fallback。

证据：`compression.py`、SQLite `summaries` migration、compression/stale/replay tests。
摘要是可废弃派生缓存，原始 events/messages 仍是 authority；未虚构 Token 节省比例。

### 7.3 M3.3 Evaluation Harness（已完成）

- versioned task manifest；
- source fixture setup；
- test/file/diff 等程序化 oracle；
- batch runner；
- 指标：task success、runtime completion、tool/model calls、tokens、latency、failure reason、recovery rate；
- 失败 run 保留和按版本对比报告。

证据：`evaluation.py`、`evaluate` CLI、`examples/eval_suite.json` 和
`tests/test_m3_evaluation.py`。固定 suite 覆盖 success、task-fail、runtime-fail、
recovery，并将基础设施失败与 Agent failure 分开；报告比较投影去除随机执行标识。

退出条件已满足：固定任务集可重复运行，Runtime completion 与 task success 独立统计，
并可用 paired diff 比较 passthrough/budgeted/compressed 变体；小样本结果只作描述性
指标，不外推生产成功率。

## 8. M4：OS-level Isolation

Threat model、SandboxExecutor contract 和攻击测试矩阵见 [`m4-implementation-plan.md`](./m4-implementation-plan.md)。

范围：

- source 不进入 sandbox、只挂载可写 task workspace；
- 默认无网络；
- CPU、内存、磁盘、进程数和 wall-clock 限制；
- 最小环境与 secret 隔离；
- 子进程树清理；
- escape、symlink race、network、fork bomb/资源耗尽测试；
- 若增加通用执行能力，使用结构化 argv、命令/工作目录策略和同一 sandbox executor。

### 8.1 M4.1 OS Isolation Foundation（已完成，2026-09-05）

已将 `restricted_test` 接入 `SandboxPolicy` 和默认 Linux rootless namespace backend：
workspace 是唯一任务 mount，系统 rootfs 只读，默认无网络，环境变量 allowlist，
no-new-privileges/drop capabilities，wall/CPU/memory/PID/storage/output 有界，执行进程
组和 namespace 可清理。Linux capability probe 失败时 fail closed，不回退宿主执行。

`tests/test_m4_sandbox.py` 的 7 个测试覆盖成功、资源/边界失败、输出上限、子进程清理、
并行 session、trajectory metadata 和 SQLite tool recovery；M4.1 阶段全量回归为 68/68，四份
semantic golden 保持通过。native backend 只记录关键运行时样本 fingerprint，不是完整
rootfs digest，也不宣称 OCI image/container lifecycle 已实现。

M4.1 不提供任意时刻抢占；中断仍使用 M2 的安全状态边界，sandbox timeout/崩溃清理已
验证。native backend 的 runtime sample fingerprint 仍不是 OCI image 生命周期承诺。

### 8.2 M4.2 Structured Execution Extension（已完成，2026-09-05）

新增 `run_command`，模型只能提交可信 profile、结构化 `argv` 和受限相对 `cwd`；
executable allowlist、argv/参数/cwd 上限、`EXECUTE_COMMAND` permission 和
`NON_IDEMPOTENT` recovery 已接入同一 ToolHarness/SQLite/M4.1 sandbox。非零退出是
observation，timeout/resource/sandbox failure 复用统一结果；network 或扩容 profile
未获一次性 approval 时 fail closed。不得引入接收 shell 字符串的通用 Shell。

`tests/test_m4_execution.py` 的 6 个测试覆盖成功、失败、approval、direct argv 和
crash recovery；M4.1 的 7 个安全测试与四份 semantic golden 保持通过。固定 eval suite
现有 7 个 case、覆盖 calculator 与 todo-cli 两个 fixture；结构化 command case 和 todo
recovery case task success，infrastructure failure 为 0。

## 9. M5：Eval-driven Capability Expansion（条件阶段）

只有 M3 指标显示覆盖率瓶颈时才考虑：search、git diff/status、patch/edit 增强、依赖准备或更广执行能力。每个工具必须回答：

1. 哪类评测任务需要它？
2. 为什么现有 read/edit/test 不足？
3. 新增了什么权限和 prompt/schema 成本？
4. 对 task success、tool count 和失败率的影响是什么？

不以工具数量、多 Agent 数量或 UI 完成度作为项目成熟度指标。

### 9.1 首次能力扩展门禁（2026-09-05）

已扩展当前固定 suite 为 13 个 case、6 个 fixture，并运行 `budgeted` variant：13 个 run
全部为 valid run，`infrastructure_failure=0`，`source_invariant_rate=1.0`，
`runtime_completion_rate=0.9231`，`task_success_rate=0.6923`，`recovery_rate=1.0`。
新增 case 覆盖跨文件定位探针、多文件 bug 后测试失败继续定位、指定文件范围和长历史
compression；compressed variant 中 long-history case 成功记录压缩 Token。失败仍是预先
设计的 `invalid_script_response`、`oracle_failure` 和错误路径 observation；budgeted variant
另记录了 summarizer 未配置时的有界 compression fallback。没有真实模型 failure coverage。
这是小型 scripted/offline 证据，不代表真实 Coding 任务覆盖率，也不能证明不需要 search
或 Git。

Passthrough/budgeted 单变量 A/B 各 13 个 paired runs，task success、Runtime completion、
source invariant、工具调用和 Token 没有差异；单次 latency 波动不作结论。DeepSeek 真实
Provider smoke 已通过，并已完成修复后的 3 次 13-case 探索性 live Eval：39/39 valid、基础设施
失败=0、task success=0.8205、Runtime completion=0.8462、source invariant=1.0、permission
violations=0。其聚合结果混入 scripted 负控制，不能作为最终真实模型成功率；budgeted 长历史
case 仍需用 `compressed` variant 验证。tool-call 组裁剪问题已修复，详细记录见
[`m5-eval-expansion.md`](./m5-eval-expansion.md)。

后续 2026-09-06 增加独立 `search_lab`：no-search DeepSeek 在固定 8-call 预算下端到端
0/10，最小只读 `search_files` 候选为 6/10，oracle 8/10；直接 read 75→49、invalid calls
5→0，但输入 Token 73,073→147,027。该证据批准 M5.1，具体权限/边界和限制见
[`decisions/m5-1-search-files.md`](./decisions/m5-1-search-files.md)。Git、patch/edit、依赖准备
仍保持条件阶段。

同日继续把 search benchmark 扩大到 3 个仓库并运行 15+15 提示 A/B：search-first 提示将
端到端 12/15→13/15、oracle 12/15→14/15、first-search 中位位置 4→1、输入 Token
234,638→207,658（-11.5%），且不提高 8-call 工具预算。剩余两次预算耗尽继续作为行为证据。

## 10. Release/Evidence Hardening（已完成，2026-09-05）

- 修正 `AGENTS.md` 与 `HANDOFF.md` 的旧里程碑描述，明确 M4.2 当前基线和 M5 条件门禁；
- `evaluation.py` 将 `RESUME_STARTED` 纳入 `recovery_events`，并由测试断言恢复指标一致；
- 初始化 Git 仓库并保留工作副本现状，创建并推送初始提交；新增 GitHub Actions CI，覆盖
  Ruff、23/33 源码文件 mypy、93 个默认测试、native capability report、70% coverage、
  compile、calculator/todo scripted smoke 和 offline eval（native capability 可用时）；能力
  受限 runner 的 native-only case 显式 skip，且跳过 sandbox-dependent eval，不视为 native
  security suite 通过；
- 新增显式、凭据门控的 `tests/live_provider_smoke.py` 与手动 workflow；DeepSeek 首次 live
  请求因过小的 smoke 输出预算得到空 `content`，现已增加预算并支持 `thinking: disabled`，
  用户已重新运行确认成功；另提供 provider override 运行真实 Eval baseline；上下文 tool-call
  组裁剪问题已修复，修复后已完成 3 次 `budgeted` 探索性 Eval；compressed 定向 3 次不再出现
  context/schema failure，端到端 2/3；
- 固定 eval 扩展为 14 case/7 fixture，覆盖跨文件、多文件恢复、范围约束、长历史 compression
  和 search-specific repository；仍明确标注为小型 scripted/offline 数据集，不能外推真实 Coding 任务；
- Eval case 显式区分正常任务和负控制，报告分别给出正常任务成功率与负控制 observed failure
  rate；保留全体 run 聚合字段只为历史兼容；
- Provider usage 与 model journal 异常提交语义、普通 Tool handler 的可终止进程边界已完成回归；
- mypy 门禁扩大到 23/33 个源码文件，coverage 合并 multiprocessing/subprocess 数据，并加入
  `uv.lock`；namespace runner 仍因隔离环境不注入宿主 coverage；
- 记录 `runtime.py`、`persistence.py`、`evaluation.py` 的集中度风险，暂不进行无验收收益的拆分。

## 11. 持续风险登记

| 风险 | 当前状态 | 缓解路径 |
|---|---|---|
| 兼容 JsonlEventStore append 读取旧 trace，写入趋近 O(n²) | 已知；默认主写路径不使用它 | SQLite sequence；JSONL 只做导出 projection |
| Session 已持久化但中断不可恢复 | 已缓解；仍受 crash window 边界约束 | M2.2 interrupt/resume 与调用 journal |
| restricted test 在宿主执行仓库代码 | M4.1 已缓解；仍受内核/运行时 TCB 限制 | capability probe、namespace attack tests；无 fallback |
| fingerprint 只能检测 source 变化 | 已缓解；仍作为不变量证据保留 | read-only source boundary + workspace mount |
| Runtime/backend 同步，长调用占线程 | 有意选择 | 完成持久语义后再用测量决定 async |
| 自定义 JSON schema 只支持有限子集 | 有意选择 | 新工具需求驱动扩展并加 contract tests |
| `COMPLETED` 被误读为 task success | 文档/指标风险 | M3 oracle 独立统计，现阶段明确表述 |
| Python/POSIX timeout 行为跨平台不一致 | 已知 | 当前声明 Linux 基线，M4 executor 统一 |

## 12. 进度更新规则

每完成一个子阶段：

1. 在本页更新状态，但只有全部 exit criteria 满足才写“已完成”；
2. 勾选对应实施 checklist；
3. 更新 `current-state.md` 的实现、限制和测试数；
4. 更新 `repository-structure.md` 的真实目录；
5. 在 `HANDOFF.md` 写清新的唯一下一项；
6. 记录测试、golden、smoke 和可选 live 验证结果；
7. 对改变外部 event/schema 的内容说明版本迁移。

## 13. 当前下一步

M5.1 的 3-repository follow-up 已修复模型上下文中始终显示初始预算的问题，并增加停止无关
读取、预留测试调用的紧凑行为指引；15 次 live 端到端/Runtime 为 14/15，固定 8-call 预算未
提高。随后四类非 search capability holdout 的 12 次 live 全部端到端成功，没有形成 Git、
patch/edit、依赖安装或其他 M5.2 failure coverage。因此冻结当前五工具集合，下一步转向版本
整理和已托管 CI 证据；只有未来新的独立失败覆盖才能重开能力扩展。保持 M4.1
namespace/policy/harness 边界，不加入 shell 字符串、默认网络、多 Agent。
