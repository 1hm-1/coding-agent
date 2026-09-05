# 仓库结构

> 当前结构与未来结构必须分开阅读。本文件中未标记为“计划”的路径应能在当前仓库找到。

## 1. 当前 M4.2 + Release/Evidence Hardening 结构

```text
coding-agent/
├── AGENTS.md                         # 后续 Coding Agent 的项目级约束
├── README.md                         # 项目概览和演示入口
├── pyproject.toml                    # package/build metadata 和稳定 lint 基线
├── .gitignore                        # 运行产物、虚拟环境和 coverage 缓存
├── .github/
│   └── workflows/
│       ├── ci.yml                    # Ruff/mypy/coverage/tests/eval 门禁
│       └── live-provider-smoke.yml   # 手动、凭据门控的真实 adapter smoke
├── docs/
│   ├── README.md                     # 文档导航和权威性说明
│   ├── HANDOFF.md                    # 新窗口开发交接
│   ├── architecture.md               # 目标架构、当前边界和不变量
│   ├── contracts.md                  # 当前 M4.2 精确契约
│   ├── current-state.md              # 已实现、未实现、已知限制
│   ├── development-guide.md          # 从零上手和变更流程
│   ├── module-design.md              # 模块职责、依赖和扩展边界
│   ├── requirements-traceability.md  # 原始需求和面试能力证据映射
│   ├── repository-structure.md       # 本文件
│   ├── roadmap.md                    # 里程碑、门禁和完成定义
│   ├── testing-strategy.md           # 测试层次、golden 和 CI 门禁
│   ├── m1-vertical-slice.md          # M1 已完成实施记录
│   ├── m1.5-engineering-hardening.md # M1.5 已完成实施记录
│   ├── m2-implementation-plan.md      # M2.1—M2.3 实施与验收记录
│   ├── m3-implementation-plan.md      # M3.1—M3.3 实施与验收记录
│   └── m4-implementation-plan.md      # M4.1/M4.2 验收与 M5 门禁
├── examples/
│   ├── fixture/                      # calculator 最小源仓库
│   ├── scripted_run.json             # calculator 确定性模型脚本
│   ├── eval_suite.json               # M3 固定离线评测 suite
│   ├── todo_cli/                     # 真实小仓库 fixture
│   └── todo_cli_scripted_run.json    # 失败后修复的确定性脚本
├── src/coding_agent/
│   ├── __init__.py
│   ├── application.py                # composition root / start use case
│   ├── cli.py                        # run/resume/interrupt/inspect/replay/export/evaluate CLI
│   ├── context.py                    # token counter、capability、section/budget builders
│   ├── compression.py                # summary schema、lineage、stale/fact verification
│   ├── domain.py                     # 核心值对象、enum 和错误
│   ├── export.py                     # SQLite committed events → JSONL projection
│   ├── migrations.py                 # ordered SQLite schema migrations
│   ├── persistence.py                # SQLite journal、snapshot、summary 和事务
│   ├── runtime.py                    # 显式 FSM 与执行循环
│   ├── test_profiles.py              # 可信测试命令配置
│   ├── command_profiles.py            # 结构化命令 profile/allowlist
│   ├── trajectory.py                 # 兼容 JSONL、replay、semantic projection
│   ├── workspace.py                  # workspace 生命周期、路径守卫和 repo snapshot
│   ├── evaluation.py                 # versioned suite、oracle、runner、metrics、A/B
│   ├── sandbox/
│   │   ├── __init__.py                # SandboxExecutor public exports
│   │   ├── base.py                    # ExecutionSpec/Result/limits/capabilities
│   │   ├── policy.py                  # fail-closed admission policy
│   │   ├── local_container.py         # Linux namespace executor, no host fallback
│   │   └── runner.py                  # namespace-private rootfs/limits runner
│   ├── models/
│   │   ├── __init__.py
│   │   ├── base.py                   # ModelBackend protocol
│   │   ├── scripted.py                # ScriptedBackend
│   │   ├── errors.py                  # Provider error classification/redaction
│   │   ├── retry.py                   # bounded backoff calculation
│   │   ├── fallback.py                # ordered provider fallback
│   │   ├── http.py                    # injectable HTTP transport boundary
│   │   ├── openai_compatible.py       # OpenAI chat-completions adapter
│   │   └── anthropic.py               # Anthropic Messages adapter
│   └── tools/
│       ├── __init__.py
│       ├── base.py                   # ToolDefinition/Registry/Outcome
│       ├── builtin.py                # read/edit/restricted_test/run_command
│       └── harness.py                # 统一执行管线
└── tests/
    ├── __init__.py                   # 保证 stdlib discovery 进入 tests
    ├── golden/
    │   ├── bugfix_success.json
    │   ├── permission_denied.json
    │   ├── runtime_failure.json
    │   └── test_failure_recovery.json
    ├── test_hardening.py
    ├── test_m2_recovery.py
    ├── test_models.py
    ├── test_m3_context.py
    ├── test_m3_evaluation.py
    ├── test_m4_sandbox.py              # OS isolation attack/resource/recovery tests
    ├── test_m4_execution.py             # structured argv/profile/approval/recovery tests
    ├── live_provider_smoke.py           # opt-in provider adapter smoke, not default discovery
    ├── test_persistence.py
    ├── test_state_machine.py
    ├── test_tools.py
    ├── test_vertical_slice.py
    └── test_workspace.py
```

`__pycache__/`、`.pytest_cache/`、`.ruff_cache/`、虚拟环境和运行时 agent home 都是生成物，不属于设计结构，不应提交或依赖。

## 2. 运行时目录（当前）

CLI 的 `--agent-home` 必须在 source 外。例如：

```text
/tmp/coding-agent-demo/
├── state.db                          # SQLite 权威事实来源（schema v3）
├── traces/
│   └── <session-id>.jsonl
└── workspaces/
    └── <session-id>/
        ├── .git/              # 副本自己的 baseline，不是源仓库 metadata
        └── <copied source>
```

当前没有独立 checkpoint 文件或 Provider cache；checkpoint 存在 `state.db` 的
`checkpoints` 表中。JSONL trace 是可删除、可从 DB 重建的导出投影。

Workspace 创建规则：

1. source 与 agent home 不得互相包含；
2. 每个 session 使用唯一目录；
3. source `.git` 不复制；
4. 指向副本外部的 symlink 被移除；
5. 文件工具只能解析 workspace 内相对路径；
6. 结束时重新计算 source fingerprint 并记录不变量结果。

## 3. M2.2/M2.3/M3 已实现结构

```text
src/coding_agent/
└── models/
    ├── errors.py               # 已实现：统一 backend 错误
    ├── retry.py                # 已实现：有界重试策略
    ├── fallback.py             # 已实现：显式 fallback chain
    ├── http.py                 # 已实现：可注入 HTTP transport
    ├── openai_compatible.py    # 已实现
    └── anthropic.py            # 已实现

tests/
├── test_m2_recovery.py         # M2.2 crash/resume/lease/reconciliation
└── test_models.py              # M2.3 offline adapter/retry contract tests

<agent-home>/
├── state.db                    # 已实现：schema v3 权威事实来源，含 summaries
├── traces/                     # 已实现：从 DB 导出的可重建 projection
└── workspaces/
```

不要预先创建空目录或无消费者 protocol。实施者可以在满足 [`m2-implementation-plan.md`](./m2-implementation-plan.md) 契约的前提下调整文件拆分，并同步更新本文。

## 4. M3/M4 已实现、M5 条件结构

M3 当前保留 `context.py`、`compression.py` 和 `evaluation.py` 单文件模块；M4.1 已加入
`sandbox/`，M4.2 增加了结构化命令 profile。只有当单文件已经承担多个独立职责且有实际
消费者时才继续拆分。当前结构：

```text
src/coding_agent/
└── sandbox/
    ├── base.py                 # 窄执行协议和值对象
    ├── policy.py               # profile/能力/路径/环境准入
    ├── local_container.py      # Linux rootless namespace backend
    └── runner.py               # 私有 chroot、mount、limits、cleanup
```

`command_profiles.py` 管理固定 executable allowlist、argv/cwd 限制、环境和资源策略；
`run_command` 通过同一 ToolHarness、SQLite journal 和 M4 sandbox 执行，不接受 shell 字符串。
M4.1/M4.2 使用 native rootfs content identity，不宣称 Docker/Podman 或 OCI image backend。
批准网络和扩展资源 profile 仍 fail closed，等待未来明确授权通道。模块职责以
[`module-design.md`](./module-design.md) 为准，里程碑顺序以 [`roadmap.md`](./roadmap.md) 为准。

## 5. 文件放置规则

- 产品代码只放 `src/coding_agent/`；测试不要 import `examples/` 中的实现。
- 测试模块按行为边界命名，不按内部私有函数一一镜像。
- 可变的运行产物放 `--agent-home`，不得写入 source fixture。
- `examples/` 的 source 是“用户原仓库”样本，脚本是 ScriptedBackend 输入；两者语义不同。
- `tests/golden/` 只保存 semantic projection，不保存含 UUID/time/path 的原始 JSONL。
- 架构决策更新现有文档；只有存在长期独立主题时才新增文档。
- 凭据、真实用户仓库、Provider 原始敏感请求/响应不得进入仓库。

## 6. 相邻目录边界

`/home/hmli/code/hermes-agent` 是参考项目，不属于本仓库，也不是自动同步上游。它当前有用户改动；开发本项目时禁止顺手修改、格式化或清理该目录。

`/home/hmli/code` 自身不是有效 Git 仓库；不要在该目录执行会假设统一版本历史的恢复操作。开始开发前始终切换到 `/home/hmli/code/coding-agent`。

## 7. 结构变化检查

新增、移动或删除模块时：

1. 更新本文件的当前树；
2. 更新 [`module-design.md`](./module-design.md) 的职责和依赖；
3. 检查 `README.md`、`AGENTS.md`、测试 import 和 CLI 示例；
4. 确认没有把生成物或 agent home 写进仓库；
5. 运行文档链接检查、全量测试和 Ruff。
