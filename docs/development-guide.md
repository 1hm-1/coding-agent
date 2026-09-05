# 开发指南

本文给第一次接触项目的开发者提供可复制的操作路径。开始前先读 [`current-state.md`](./current-state.md)，避免把目标架构当成已实现代码。

## 1. 环境要求

- Python 3.10 或更高版本；当前验证版本为 Python 3.10.12。
- Git CLI；WorkspaceManager 用它在隔离副本内创建 baseline。
- Linux/POSIX 环境；M4.1 native security tests 需要 user/mount/PID/network namespace 和
  mount capability，普通非 Linux 环境会对 sandbox execution fail closed。
- M2 运行时依赖 `httpx`；Provider transport 仍可用注入 fake 完全离线测试。
- Ruff 是开发依赖；`pyproject.toml` 显式固定了稳定的必检规则。

项目目录：

```text
/home/hmli/code/coding-agent
```

相邻的 `/home/hmli/code/hermes-agent` 是参考源码仓库，不是本项目代码，且存在用户未提交改动。除非用户明确要求，不要修改它。

## 2. 首次检查

```bash
cd /home/hmli/code/coding-agent
python3 --version
git --version
PYTHONPATH=src python3 -m unittest discover -v
```

预期：当前应发现 75 个默认测试；在 native sandbox capability 可用时全部通过，能力受限环境中 7 个 native-only case 会显式 skip。`tests/live_provider_smoke.py` 不以 `test_` 命名，不属于默认 discovery；它只在显式配置凭据后运行。若输出 `Ran 0 tests`，检查 `tests/__init__.py` 是否存在，以及命令是否从项目根执行。

可以使用 `uv` 创建虚拟环境并安装项目及开发工具：

```bash
uv venv .venv
uv pip install --python .venv/bin/python -e ".[dev]"
PYTHONPATH=src .venv/bin/python -m unittest discover -v
.venv/bin/ruff check src tests examples/todo_cli examples/mini_repos
.venv/bin/mypy
PYTHONPATH=src .venv/bin/coverage run -m unittest discover -q
.venv/bin/coverage report
```

当前 M5 条件评估 suite 位于 `examples/eval_suite.json`，包含 13 个 case 和 6 个 fixture；
它仍是 scripted/offline 证据，不能代替真实 Provider 基线，也不能直接证明需要新增 search
或 Git 工具。要运行预算化评测、压缩评测或单变量 A/B：

```bash
PYTHONPATH=src python3 -m coding_agent.cli \
  --agent-home /tmp/coding-agent-m5 \
  evaluate --suite examples/eval_suite.json --variant budgeted --repetitions 1

PYTHONPATH=src python3 -m coding_agent.cli \
  --agent-home /tmp/coding-agent-m5-compressed \
  evaluate --suite examples/eval_suite.json --variant compressed --repetitions 1

PYTHONPATH=src python3 -m coding_agent.cli \
  --agent-home /tmp/coding-agent-m5-ab \
  evaluate --suite examples/eval_suite.json --ab --repetitions 1
```

评测的 fixture、程序化 oracle、结果和 M5 工具门禁解释见 [`m5-eval-expansion.md`](./m5-eval-expansion.md)。

native sandbox backend 使用 `/usr/bin/python3` 作为固定 runtime；因此无论 Ruff/项目工具
是否由 `.venv` 提供，M4.1 的实际 namespace security test 都应使用上面的 system
`python3` 命令运行。`.venv/bin/python` 可用于普通 unit tests 和工具安装，但不应被当作
sandbox image 内的可执行文件。

也可使用已有的 `ruff`/`uvx ruff`。项目显式选择 `E4`、`E7`、`E9`、`F` 作为当前强制基线；不要因本机 Ruff 版本默认规则变化而顺手做全仓风格重写。更严格规则应单独提案、修复基线后再加入门禁。

不要为了运行当前测试而安装 LangChain、LangGraph 或其他 Agent 框架。

## 3. 跑通两个示例

### 最小 calculator 示例

```bash
PYTHONPATH=src python3 -m coding_agent.cli \
  --agent-home /tmp/coding-agent-calculator \
  run-scripted \
  --source examples/fixture \
  --task "修复 add 函数并运行测试" \
  --script examples/scripted_run.json
```

### 测试失败后恢复的 todo 示例

```bash
PYTHONPATH=src python3 -m coding_agent.cli \
  --agent-home /tmp/coding-agent-todo \
  run-scripted \
  --source examples/todo_cli \
  --task "Fix the empty input crash and run tests." \
  --script examples/todo_cli_scripted_run.json
```

从输出复制 `session_id`：

```bash
PYTHONPATH=src python3 -m coding_agent.cli \
  --agent-home /tmp/coding-agent-todo \
  replay --session-id <session-id>
```

预期 todo replay 的关键结果：`final_state=completed`、`test_outcomes=[false,true]`、`source_unchanged=true`。

当前默认把事实写入 `<agent-home>/state.db`（schema v3，含 summaries 派生缓存）；JSONL 是导出投影。删除 trace 后可运行：

```bash
PYTHONPATH=src python3 -m coding_agent.cli \
  --agent-home /tmp/coding-agent-todo \
  export-trace --session-id <session-id>
```

注意：同一个 agent home 可以容纳多个随机 session；不要手工把固定 session id 指向已有 workspace。

## 4. 建议的代码阅读顺序

1. `domain.py`：先理解状态、消息、工具和事件结构；
2. `runtime.py`：从 `initialize()`、`step()`、handler map 阅读完整循环；
3. `models/base.py`、`models/scripted.py`、`models/errors.py` 和两个真实 adapter：理解 provider 边界；
4. `tools/base.py`、`tools/harness.py`、`tools/builtin.py`：理解工具契约和信任边界；
5. `workspace.py`：理解源目录与任务副本的隔离；
6. `trajectory.py`：理解 JSONL、replay 和 golden projection；
7. `persistence.py`、`migrations.py`、`export.py`：理解 SQLite authority 和 DB→JSONL projection；
8. `compression.py`、`evaluation.py`：理解 summary lineage、oracle、metrics 和 A/B；
9. `command_profiles.py`、`sandbox/base.py`、`sandbox/policy.py`、`sandbox/local_container.py`、`sandbox/runner.py`：理解 M4.2 structured argv/profile 与 M4.1 OS boundary、capability probe 和 fail-closed cleanup；
10. `application.py`、`cli.py`：最后看组装和入口；
11. `tests/test_vertical_slice.py`、`tests/test_hardening.py`、`tests/test_persistence.py`、`tests/test_m2_recovery.py`、`tests/test_models.py`、`tests/test_m3_context.py`、`tests/test_m3_evaluation.py`、`tests/test_m4_sandbox.py` 与 `tests/test_m4_execution.py`：用测试验证理解。

## 5. 标准开发流程

每个变更按以下顺序进行：

1. 在 `roadmap.md` 找到当前里程碑和明确退出条件。
2. 阅读对应实施文档；M3 使用 `m3-implementation-plan.md`，M4.1/M4.2 使用 `m4-implementation-plan.md`；下一阶段是 M5 条件评估。
3. 运行全量基线并保存结果，确认不是在已有红灯上开发。
4. 先写失败用例或 fault scenario，再修改生产代码。
5. 只改当前里程碑需要的最小模块，不顺手扩展工具/UI/multi-agent。
6. 运行目标测试、全量测试、静态检查和两个 smoke demo。
7. 检查四份 M1.5 golden；不得通过删除 golden 获得绿灯。
8. M4 变更还必须运行 native capability/security tests，确认没有 host subprocess fallback；M4.2 变更还要覆盖 profile/argv/approval/recovery contract。
9. 更新 `current-state.md`、实施 checklist 和 README；未完成能力保持“未实现”。

当前强制静态检查命令：

```bash
.venv/bin/ruff check src tests examples/todo_cli
.venv/bin/mypy
```

覆盖率门禁为 70% statement coverage：

```bash
PYTHONPATH=src .venv/bin/coverage run -m unittest discover -q
.venv/bin/coverage report
```

真实 Provider smoke 不是默认测试；只有明确设置 `CODING_AGENT_LIVE_PROVIDER`、
`CODING_AGENT_LIVE_MODEL` 和对应 API key 后才手动运行：

```bash
PYTHONPATH=src .venv/bin/python -m unittest tests.live_provider_smoke -v
```

M3 离线评测：

```bash
PYTHONPATH=src python3 -m coding_agent.cli \
  --agent-home /tmp/coding-agent-m3-evals \
  evaluate --suite examples/eval_suite.json --variant budgeted
```

评测器每个 case/repetition 使用新的 SQLite、session 和 workspace；报告中的
`report.json` 是去除随机执行标识的比较投影，`runs.jsonl` 保留 trace 定位信息。

## 6. 代码与数据约定

- 兼容 Python 3.10；不要无意使用更高版本语法或 stdlib API。
- 跨层数据优先用显式 dataclass/enum/Protocol，持久边界必须有版本化 JSON 序列化；禁止 pickle Runtime 对象。
- 时间统一存 ISO-8601 UTC；测试中的 clock、UUID、backoff 和 fault point 应可注入，禁止用随机 sleep 验证并发或恢复。
- SQL 一律参数化，事务保持短小；外部 model/tool/process 调用不在事务中执行。
- Path 在进入 workspace 边界时 resolve/containment；不要用字符串前缀判断路径归属。
- 可预期失败使用 domain error/structured result；只有 Tool Harness 等明确隔离边界允许兜底捕获，并必须记录错误类型。
- Event payload 是持久协议，字段保持 JSON 类型；普通 debug log 不替代 Event。
- 测试默认离线、无真实凭据、无真实 Provider；live smoke 必须显式 opt-in。
- 不在测试中修改 `examples/` source fixture；每次通过 Application 创建新 workspace。
- 不在无关功能 PR 中进行全仓格式化、模块搬迁或依赖升级。

## 7. 修改状态机

新增或修改状态时：

1. 修改 `RuntimeState`；
2. 修改 `ALLOWED_TRANSITIONS`；
3. 在 `AgentRuntime._handlers` 注册 handler；
4. 确保 handler 恰好完成一个状态动作；
5. 为合法和非法迁移加测试；
6. 更新 `contracts.md`；
7. 运行 golden，判断 transition path 变化是否是预期协议变化；
8. 若是协议变化，升级 semantic projection schema 或写清迁移原因。

不要在 handler 内直接给 `session.state` 赋值；必须经过 `StateMachine.transition()`。

## 8. 新增或修改工具

M2 默认不新增模型工具。确有当前里程碑需求时：

1. 在 `ToolDefinition` 声明唯一名称、描述、闭合 schema、权限和 timeout；
2. handler 只接收验证后的参数与 `ToolContext`；
3. 文件路径必须走 `WorkspaceGuard`；
4. 不把源仓库路径、API key 或宿主环境交给 handler；
5. 返回 `ToolOutcome`，不要把错误伪装成普通字符串；
6. 写正常、未知字段、无权限、越界、超时、输出过大和 handler 异常测试；
7. 更新模型可见 schema 和 `contracts.md`；
8. 评估 golden tool order 是否应该改变。

禁止直接增加接收命令字符串的 Shell 工具。`restricted_test` 和 `run_command` 都通过
可信 profile、结构化 argv 与同一 SandboxExecutor/policy/harness 边界；profile 的
network 或扩展资源要求在没有明确授权通道时必须 fail closed。

## 9. 修改 Event 或 Replay

- Event 是外部可消费协议，不是随意日志。
- 新事件必须说明触发时机、payload、是否包含敏感数据和 replay 语义。
- 破坏性字段变化必须提升 `schema_version`。
- Replay 只能重建状态和指标，不能执行工具副作用。
- Semantic projection 不得包含 UUID、timestamp、绝对路径、duration 等随机值。
- `run_finished` 中的计数必须与 replay 计算结果交叉校验。

## 10. Golden 更新流程

Golden 失败时先回答：

1. Runtime 行为是否意外改变？如果是，修代码。
2. 是否只是随机字段进入 projection？如果是，修 projection。
3. 是否是经过批准的状态/工具协议变化？只有此时才更新 golden。

更新 golden 时，在对应实施文档记录变化原因，并确保四个场景仍分别表达：成功、测试恢复、权限拒绝和 Runtime failure。

## 11. 常见问题

### `Ran 0 tests`

从项目根执行，设置 `PYTHONPATH=src`，保留 `tests/__init__.py`。

### `agent_home must not be inside the source repository`

这是保护性检查。把 agent home 指向源仓库之外，例如 `/tmp/coding-agent-dev`。

### `workspace already exists`

不要复用相同 session id。正常 CLI 会生成 UUID；测试显式 session id 时使用新的临时目录。

### Golden 不一致

打印 `ReplayResult.semantic_projection()`，对比语义字段。不要对原始 JSONL 做全文 snapshot。

### 测试 profile 找不到依赖

M1 默认 profile 清空 `PYTHONPATH` 并使用最小环境。Fixture 应可从自身 workspace 运行；M2 如需依赖安装，必须设计可信环境准备阶段，不能让模型提交安装命令。
