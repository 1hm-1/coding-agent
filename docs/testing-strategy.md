# 测试与质量门禁

## 1. 测试目标

测试不是只证明 final answer 存在，而是分别证明控制流、工具边界、隔离、失败语义和轨迹可诊断。测试失败的 run 不能从评测分母或历史中删除。

## 2. 当前测试层次

| 文件 | 层次 | 证明内容 |
|---|---|---|
| `test_state_machine.py` | unit | 合法/非法状态迁移 |
| `test_workspace.py` | unit/integration | 副本独立、路径和 symlink 越界拒绝 |
| `test_tools.py` | contract | 注册、schema、权限、精确编辑、可信测试、timeout |
| `test_vertical_slice.py` | integration | read-edit-test-final、预算失败、backend failure |
| `test_hardening.py` | fault/golden | 权限 observation、测试恢复、handler fault、四类 golden |
| `test_m2_recovery.py` | fault/integration | checkpoint、interrupt/resume、调用 journal、lease、reconciliation、resolution |
| `test_models.py` | contract | 两个 adapter、HTTP 错误分类、retry/fallback、凭据脱敏 |
| `test_persistence.py` | contract/fault | migration、snapshot round-trip、atomic mutation、冲突、rollback、DB→JSONL rebuild |
| `test_m3_context.py` | contract/integration/fault | capability/counter、section budget/hard retention、summary schema/lineage、stale、compression fallback、raw-event preservation |
| `test_m3_evaluation.py` | eval/integration | strict manifest/containment、fresh repetition、trusted oracle、基础设施失败、task/runtime 分离、metrics、paired A/B |
| `test_m4_sandbox.py` | security/integration/fault | capability fail-closed、namespace escape/network/secret/symlink/proc/device、resource/output limits、process cleanup、parallel session、SQLite recovery |
| `test_m4_execution.py` | security/contract/integration/fault | structured argv/profile allowlist、cwd/schema、approval fail-closed、non-zero observation、sandbox cleanup 和 non-idempotent recovery |
| `live_provider_smoke.py` | opt-in live contract | 凭据门控的 OpenAI-compatible/Anthropic 真实 adapter 请求；不进入默认 discovery |

当前统一命令：

```bash
PYTHONPATH=src python3 -m unittest discover -v
```

当前验收基线为 82 个测试；在具备 native sandbox capability 的环境中全部通过，能力受限
环境中 native-only case 会显式 skip。live provider smoke 需显式凭据和手动触发。

静态检查（安装 Ruff 开发依赖后）：

```bash
ruff check src tests examples/todo_cli examples/mini_repos
.venv/bin/mypy
PYTHONPATH=src .venv/bin/coverage run -m unittest discover -v
.venv/bin/coverage combine
.venv/bin/coverage report
```

`pyproject.toml` 显式选择 `E4`、`E7`、`E9`、`F`，避免不同 Ruff 版本的默认规则改变项目门禁。扩大 lint 规则属于独立质量变更：先清理基线、解释有意异常边界（例如 Harness 的兜底捕获），再更新配置。

## 3. 必须保持的四个 golden

| Golden | 核心不变量 |
|---|---|
| `bugfix_success.json` | read → edit → test，最终测试通过 |
| `test_failure_recovery.json` | test outcomes 必须为 `false → true` |
| `permission_denied.json` | tool failure 被观察，Runtime 不 crash |
| `runtime_failure.json` | backend 协议错误进入分类 `FAILED` |

Golden 比较的是 `ReplayResult.semantic_projection()`，不比较原始 JSONL。

## 4. Golden 变更规则

可以更新：

- 已批准的状态协议变化；
- 新里程碑明确增加必要事件或工具步骤；
- schema version 升级并有迁移说明。

不能更新：

- 为掩盖意外多一次模型/工具调用；
- 为忽略 source 被修改；
- 为把失败测试改成成功；
- 因 UUID、路径或 duration 进入 projection；
- 仅因为测试红了但尚未定位原因。

推荐流程：打印 actual projection → 判断行为是否预期 → 修代码或写设计决策 → 最后更新 golden。

## 5. Failure injection 规则

Fault 应在最窄边界注入：

- backend fault 用 `ScriptedBackend` 非法项或 error response；
- permission fault 用 `RunPolicy.allowed_permissions`；
- tool handler fault 用测试专属 Registry/handler；
- 普通 handler timeout 使用阻塞 worker 并验证 deadline 前返回且延迟副作用未发生；sandbox
  timeout 使用测试专属可信 TestProfile；
- M2 crash fault 在事务边界注入，不用随机 `sleep` 模拟。

故障用例必须断言三件事：最终 Runtime 状态、observation/event 证据、workspace/source 副作用。

## 6. M2 新增测试矩阵

| 能力 | 正常路径 | 必须故障路径 |
|---|---|---|
| SQLite migration | 空库升级到最新 | 重复执行 migration、未知未来版本 |
| Session store | create/load/transition | 乐观锁冲突、非法状态、写事务回滚 |
| Tool journal | prepared→running→finished | 每个边界 crash、重复 call id |
| Resume | 每个非终态恢复 | workspace 缺失、checkpoint/event 不一致 |
| Edit reconciliation | pre hash 执行、post hash补记 | hash 既非 pre 也非 post → uncertain |
| Interrupt | step 边界安全停止 | tool/model 返回后立刻中断 |
| OpenAI adapter | text/tool/usage mapping | 401、429、5xx、timeout、malformed JSON、非法 usage 与 journal closure |
| Anthropic adapter | content block mapping | 同上及未知 block、非法 usage |
| Retry/fallback | retryable infrastructure error | auth/invalid request 不重试，质量差不 fallback |
| JSONL export | DB event 正确导出 | sink 失败后可从 DB 重建 |

M2.1 的 storage contract 由 `test_persistence.py` 覆盖；M2.2 的调用 journal、resume、
crash matrix 和 M2.3 的 adapter/retry contract 分别由 `test_m2_recovery.py`、
`test_models.py` 覆盖。默认测试完全离线；真实 Provider live smoke 仅在有凭据时人工触发，
本次因无凭据未执行。

## 7. M3 Context、Compression 与 Evaluation 矩阵

| 能力 | 正常路径 | 必须故障/恢复路径 |
|---|---|---|
| Token accounting | provider exact、named fallback、确定性 manifest | unknown capability、required content 超预算 |
| Context sections | system/task-runtime/repository/summary/recent 固定顺序 | soft recent trim、hard fact retention、high-water boundary |
| Summary lineage | schema + event range/hash + workspace revision round-trip | schema/bad output、timeout/provider error、required fact 缺失 |
| Summary freshness | 新 workspace revision/file hash 被引用 | stale invalidation、supersede 后仍保留原始 events |
| Eval suite | fresh workspace、test/file/diff/result oracle、report | fixture/oracle infrastructure failure 不进入 Agent 分母 |
| Eval outcomes | task success 与 Runtime completion 分开 | runtime failure、oracle failure、预定义 fault recovery |
| A/B | 相同 case/repetition paired diff | key 不匹配显式失败，不只比较总体平均 |

M3 的固定离线 suite 位于 `examples/eval_suite.json`，由 `evaluate` CLI 生成
`manifest.snapshot.json`、`runs.jsonl`、`report.json` 和 `report.md`。报告比较投影排除
session ID/trace path 等随机执行标识；latency 作为实际测量值保留，不被伪装成确定常量。

## 8. M4.1 OS isolation 矩阵

| 能力 | 正常路径 | 必须故障/恢复路径 |
|---|---|---|
| Capability/policy | probe 成功后 profile 通过 | 缺能力、未 pin identity、shell/非 allowlist 环境/网络拒绝，均 fail closed |
| Mount boundary | workspace 可写、system rootfs 只读 | source/agent-home/外部 canary、外部 symlink、proc/device 探测不可越界 |
| Network | loopback namespace 可见 | DNS 和直接公网 IP 连接失败 |
| Limits | wall/CPU/memory/PID/storage/output 有界 | resource result 结构化，宿主继续运行 |
| Cleanup | 单个 execution 完成并记录 metadata | timeout、子进程忽略 TERM、crash recovery 不留执行进程组 |
| Parallelism | 两个 session 同时执行 | mount、PID、stdout 和 workspace 不串线 |
| Observability | execution id、identity、limits、cleanup 进入 ToolResult/event | secret、完整 host path、environment value 不进入 public projection |

M4.1 native security tests 需要 Linux namespace/mount capability；测试会通过统一 capability
gate 在能力不足时显式 skip native-only case。普通语义测试仍然运行，且默认 executor 仍然
fail closed，不会回退到宿主 `subprocess` 或把 fake 结果当作 native 安全证据。具有能力的
Linux 环境必须完整运行 native security suite；能力受限的 hosted runner 只能证明 portable
语义和 fail-closed 路径。

## 9. M4.2 Structured execution 矩阵

| 能力 | 正常路径 | 必须故障/恢复路径 |
|---|---|---|
| Command profile | 固定 profile 注入 image/env/network/limits | 未知 profile、shell executable、非 allowlist executable 拒绝 |
| Structured argv | 直接 argv 进入 sandbox，cwd 受 workspace containment 约束 | 缺字段、未知字段、argv/参数/cwd 超限或越界拒绝 |
| Approval boundary | 默认 `none` network 和基线资源 profile 执行 | network、扩展资源或显式 approval profile 在无授权通道时 fail closed |
| Result semantics | exit 0 返回 `command_succeeded=true` | 非零退出仍是可观察成功执行；timeout/resource/sandbox 映射为结构化失败 |
| Recovery | execution id、limits、identity、revision 进入 journal/result | 外部调用 crash 进入 `WAITING_APPROVAL`，不得自动重放 |

M4.2 新增的 6 个测试与 M4.1 的 7 个 native sandbox 测试共同覆盖上述边界。固定
`examples/eval_suite.json` 当前为 13 个 case、覆盖 6 个 fixture；新增跨文件定位、多文件
恢复、changed-path 范围约束和长历史 compression，`infrastructure_failure=0`，预定义失败
case 仍保留在分母中。M5 评测与工具门禁的具体证据见 `docs/m5-eval-expansion.md`。

## 10. Smoke 验收

提交里程碑前至少运行：

1. 全量 unit/contract/integration/fault tests；
2. calculator scripted demo；
3. todo recovery demo；
4. replay 两个新 trace；
5. 删除一个 JSONL 后从 SQLite `export-trace` 重建并 replay；
6. 检查原 fixture fingerprint 不变；
7. 搜索是否意外注册 `run_shell`；
8. 核对 `current-state.md` 没有提前宣称未实现能力。
9. 运行至少一个固定 eval suite；若做 A/B，确认 case/repetition keys 成对且失败 run 未从分母移除。

## 11. Release/Evidence CI

`.github/workflows/ci.yml` 在 Python 3.10/3.11 上执行：

```text
lint
  └─ ruff check
typed-release-surface
  └─ mypy (23/33 source files: models/tools/context/domain/workspace/command_profiles/evaluation/sandbox)
unit-contract
  └─ capability report + coverage run unittest discover + combine + report (fail-under=70)
golden-smoke
  ├─ compileall + calculator/todo scripted smoke (native capability available时)
  └─ offline eval suite (native capability available时)
```

GitHub-hosted runner 如果 capability report 显示 native namespace 不可用，7 个 native-only
测试会显式 skip；这不是 native security suite 的通过证明。要取得 M4.1/M4.2 native 证据，
应在具备所需 Linux namespace/mount capability 的 self-hosted runner 或本地环境运行。

`.github/workflows/live-provider-smoke.yml` 只允许手动触发，先验证所选 Provider 的 secret
存在，再运行 `tests/live_provider_smoke.py`；它不会把真实凭据带入普通 PR。用户已在本地
以 DeepSeek 完成一次 smoke 和修复后的 3 次探索性 live Eval；这些运行仍不属于默认 CI，且
`compressed` variant 需要单独验证。
