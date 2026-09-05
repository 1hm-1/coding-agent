# Production-oriented Coding Agent

这是一个以 Agent Systems Engineering 为目标的 Coding Agent 项目。当前已完成 M1、M1.5、
M2（SQLite persistence、恢复和模型边界）与 M3（Context Engineering、Compression、
Evaluation）；用最小能力证明完整 runtime
闭环及其失败语义，而不是用工具数量包装一个聊天接口。

当前已完成 **M4.2 Structured Execution Extension**，并完成 Release/Evidence Hardening。项目已支持离线测试的真实模型 adapter、
预算化 context、带 lineage 的摘要、评估报告和 Linux restricted-test OS isolation。新接手开发请从
[文档索引](docs/README.md)和[新窗口交接](docs/HANDOFF.md)开始，不要仅根据目标架构直接写代码。
本轮 M5 评测扩展和工具门禁记录见 [M5 评测证据](docs/m5-eval-expansion.md)。

## M1 已实现

- 显式有限状态机：每个状态有独立 handler 和合法迁移表；
- `ScriptedBackend`：可复现模型决策和工具调用；
- 每个 session 独立复制 workspace，文件路径拒绝越界；
- 统一 Tool Harness：schema、permission、timeout、结构化错误、输出限制和审计边界；
- M1 基线工具：`read_file`、`edit_file`、`restricted_test`；M4.2 增加结构化 `run_command`；
- 测试工具只接受可信 profile 名称，模型不能提交 command/argv；
- SQLite schema-versioned journal、原子 snapshot/event mutation；
- 从 SQLite 生成的 schema-versioned JSONL trajectory 和无副作用 replay；
- read → edit → test → final 的确定性端到端测试。

## M1.5 已实现

- 权限拒绝、测试失败恢复、handler 异常、测试超时和非法 backend response 注入；
- Replay 汇总 Token、工具状态、测试结果序列、失败类型和 source invariant；
- 四份不含随机字段的 semantic golden trajectory；
- 一个 68 行的 `todo-cli` fixture：首次修复不正确、测试失败、二次修复后通过。

通用 Shell 不属于 M1。测试命令由应用可信配置固定；M4.1 已将 `restricted_test` 放入
Linux rootless namespace、只读 rootfs、默认禁网和资源配额边界，但这不是完整容器平台，
也不包含通用 Shell 或其他平台等价实现。

## M2 已实现

- `<agent-home>/state.db` 使用 migration v3 保存 sessions、messages、events、checkpoints、调用 journal 和 summaries；
- state、checkpoint、event 与可选 message 通过短事务原子提交，并校验 expected state/version；
- JSONL 是可删除的导出投影，`export-trace` 可从 SQLite 重建；
- `INTERRUPTED`、`WAITING_APPROVAL`、lease、model/tool recovery 和 edit reconciliation 已实现；
- OpenAI-compatible、Anthropic adapter、retry/backoff 和显式 fallback 已实现；
- M2 完成时的 47 个回归测试、四份 semantic golden、calculator/todo smoke 全部通过；加入 M3/M4 和评测证据门禁后，默认 discovery 共 75 个测试。

## M3 已实现

- `BudgetedContextBuilder` 按 system/task-runtime/repository/summary/recent 分区构建上下文，使用 model capability registry 和 exact/命名 fallback token counter；
- hard-retention、section budget、high-watermark 和 deterministic `context_built` manifest；`PassthroughContextBuilder` 保留为 A/B baseline；
- `SummaryRecord` 以 event range/hash、workspace revision、file/test/error facts 和 `superseded_by` 持久化到 SQLite；压缩失败或 stale 时保留原始历史；
- `evaluate` CLI 支持严格版本化 JSON suite、隔离 repetition、可信 test/file/diff/result oracle、task/runtime 分离指标和 paired A/B report；
- `examples/eval_suite.json` 提供 13 个 case、覆盖 6 个 fixture；新增跨文件定位、多文件失败恢复、指定文件范围和长历史 compression 样例，但仍是小型 scripted 数据集，不能代表真实 Coding 任务覆盖率。

## M4.1 已实现

- `restricted_test` 只从可信 profile 生成固定 `argv`、环境、工作目录、网络模式和资源限制；模型不能提交任意命令；
- Linux 默认使用 rootless user/mount/PID/network namespace、最小 chroot、只读系统 runtime、受控 tmpfs 和唯一可写任务 workspace；
- capability probe 失败时 fail closed，绝不回退到宿主进程；默认无网络，secret 环境不进入执行；
- wall/CPU/memory/PID/storage/stdout/stderr 限制、子进程组清理、symlink/proc/device 回归、并行 session 和 recovery 有自动测试；
- trajectory 保存 execution id、profile、rootfs content identity、capability snapshot、limits、truncation 和 cleanup metadata。

## M4.2 已实现

- `run_command` 只接受可信 command profile、结构化 `argv` 和受限相对 `cwd`；profile 固定
  executable allowlist、环境、network、limits 和 rootfs identity；
- 不接受 shell 字符串、shell interpreter 或 profile 外 executable；非零退出作为 observation，
  timeout/resource/sandbox failure 使用统一结构化结果；
- network 或超出默认 limits 的 profile 在没有一次性 approval 时 fail closed；默认 profile
  不开放网络或额外资源；
- `run_command` 使用与 `restricted_test` 相同的 ToolHarness、SQLite journal 和 M4.1 sandbox，
  非幂等执行 crash 后不会自动重复。

当前 native identity 是运行时内容哈希，不宣称 OCI image、Docker/Podman backend 或生产级
漏洞扫描/多租户隔离已经实现。

## Release/Evidence Hardening 已实现

- Git 工作区已初始化并推送到 `https://github.com/1hm-1/coding-agent`，同时提供 GitHub Actions 的离线 CI：Ruff、release-surface mypy、75 个默认测试、native capability report、70% statement coverage、compile 检查、calculator/todo scripted smoke 和固定离线 eval（native capability 可用时）；能力受限 runner 会显式跳过 native-only case 和 sandbox-dependent eval，不将其当作 native security suite 通过。
- `tests/live_provider_smoke.py` 和手动 workflow 提供显式凭据门控的真实 Provider smoke；无凭据时不发起网络请求。
- recovery 指标将 `RESUME_STARTED` 计为 recovery event；扩展后的 13-case/6-fixture eval 基础设施失败为 0，但 scripted 结果仍不足以证明真实模型不需要 search 或 Git 能力；详见 [M5 评测证据](docs/m5-eval-expansion.md)。

## 快速运行

项目的唯一运行时依赖是 `httpx`；开发工具使用 `uv` 安装。在仓库目录执行：

```bash
PYTHONPATH=src python3 -m coding_agent.cli \
  --agent-home /tmp/coding-agent-demo \
  run-scripted \
  --source examples/fixture \
  --task "修复 add 函数并运行测试" \
  --script examples/scripted_run.json
```

真实模型 CLI（API key 从对应环境变量读取，不会写入 DB/trace）：

```bash
PYTHONPATH=src python3 -m coding_agent.cli \
  --agent-home /tmp/coding-agent-provider-demo \
  run --provider openai-compatible --model <model> \
  --base-url https://api.deepseek.com --thinking disabled \
  --source examples/fixture --task "修复 add 函数并运行测试"
```

DeepSeek 使用 OpenAI-compatible 接口时，将 key 放在 `OPENAI_API_KEY` 环境变量中；当前
Runtime 不回传 thinking 模式的 `reasoning_content`，因此使用 DeepSeek 工具循环时必须传入
`--thinking disabled`。真实 smoke 会对 `https://api.deepseek.com` 自动采用该设置。

命令返回 `session_id`、隔离 workspace 和 trace 路径。使用返回的 session id 回放：

```bash
PYTHONPATH=src python3 -m coding_agent.cli \
  --agent-home /tmp/coding-agent-demo \
  replay --session-id <session-id>
```

若 JSONL 导出被删除，可从 SQLite 重建：

```bash
PYTHONPATH=src python3 -m coding_agent.cli \
  --agent-home /tmp/coding-agent-demo \
  export-trace --session-id <session-id>
```

运行测试：

```bash
PYTHONPATH=src python3 -m unittest discover -v
```

## M1.5 展示任务

运行真实小仓库示例：

```bash
PYTHONPATH=src python3 -m coding_agent.cli \
  --agent-home /tmp/coding-agent-todo-demo \
  run-scripted \
  --source examples/todo_cli \
  --task "Fix the empty input crash and run tests." \
  --script examples/todo_cli_scripted_run.json
```

2026-09-05 验证结果：

| 项目 | 结果 |
|---|---|
| Task | Fix empty input crash |
| Runtime | `COMPLETED` |
| Model / tool calls | 6 / 5 |
| Test outcomes | `false → true` |
| Original fixture | fingerprint unchanged |
| Isolated workspace | `todo_parser.py` modified |
| Final tests | passed |
| Replay | 72 continuous events |

## 明确延后的能力

- approved network 的显式 approval UI/授权通道、OCI/container backend；
- 多 Agent、UI、消息平台、Skill 或 RAG。

这些能力的接口位置与演进顺序见
[当前实现](docs/current-state.md)、[架构设计](docs/architecture.md)、
[模块设计](docs/module-design.md)、[路线图](docs/roadmap.md)和
[M2 实施计划](docs/m2-implementation-plan.md)、[M3 实施计划](docs/m3-implementation-plan.md)和[M4 实施计划](docs/m4-implementation-plan.md)。M1/M1.5 的完成记录分别见
[M1 实施说明](docs/m1-vertical-slice.md)与
[M1.5 Engineering Hardening](docs/m1.5-engineering-hardening.md)。
原始工程目标与面试高频问题的“已实现/仅设计”边界见
[需求与面试能力追踪](docs/requirements-traceability.md)。

## 开发者入口

```text
docs/HANDOFF.md
  → docs/current-state.md
  → docs/development-guide.md
  → docs/contracts.md
  → docs/m2-implementation-plan.md
```

提交任何里程碑状态变化时，同时更新 `current-state.md`、`roadmap.md`、对应
checklist 和本 README。完整规则见 [开发指南](docs/development-guide.md)与
[测试策略](docs/testing-strategy.md)。
