# 新窗口开发交接

> 交接基线：2026-09-05，M2.1—M2.3、M3.1—M3.3、M4.1、M4.2 已完成。下一项是 M5 条件评估，不是重写 M1/M1.5。

## 1. 开始前必须做

1. 将工作目录切换到 `/home/hmli/code/coding-agent`。
2. 完整阅读 `AGENTS.md`、`docs/README.md`、`docs/current-state.md`。
3. 阅读 `docs/m4-implementation-plan.md` 的 M4.2 范围和 `docs/m5-eval-expansion.md`；M2/M3/M4.1 的实施记录分别在 `docs/m2-implementation-plan.md`、`docs/m3-implementation-plan.md` 和该文档中。
4. 运行：

```bash
PYTHONPATH=src python3 -m unittest discover -v
```

当前基线应为 75 个默认测试通过。`tests/live_provider_smoke.py` 是凭据门控的显式 smoke，不属于默认 discovery。若不是，先定位环境或已有变化，不要直接开始 M5。

## 2. 工作区事实

- `/home/hmli/code/coding-agent` 已初始化独立 Git 工作区，初始提交 `69a16c6` 已推送到
  `origin/main`；后续改动可以使用 Git 历史恢复。
- `/home/hmli/code` 不是有效 Git 仓库。
- `/home/hmli/code/hermes-agent` 是独立参考仓库，已有用户改动：
  - `agent/tool_dispatch_helpers.py`
  - `tests/run_agent/test_tool_batch_segmentation.py`
- 不要修改、格式化、清理或提交 Hermes 的这些文件。
- 当前项目运行时依赖 `httpx`，开发工具使用 `uv` 安装；Python 基线是 3.10.12。

## 3. 不得破坏的 M1/M1.5 契约

- 源仓库不能作为 Agent 写入目标；任务修改只发生在隔离 workspace。
- Tool 调用必须经过 Harness，不能从 Runtime 直接读写文件或启动进程。
- `restricted_test` 仍只接受可信 profile；M4.1 不增加通用 Shell，M4.2 也必须保持结构化 argv 边界。
- `COMPLETED` 只代表 Runtime 正常结束，不等于 task success。
- 四份 semantic golden 必须持续通过。
- JSONL 只能从当前 SQLite committed events 重建；不能反过来把 JSONL 当恢复事实来源。
- Provider 分支只能存在于 adapter，不进入 Runtime。
- 不引入多 Agent、UI、RAG、Skill 或 Agent framework。

## 4. M2/M3/M4 完成事实与下一步唯一推荐入口：M5 条件评估

M2.1 已完成 SQLite persistence foundation，M2.2/M2.3 也已严格完成：

- migration v1 runner，重复运行幂等并拒绝未知更高版本；
- sessions/messages/events/checkpoints 最小表；
- 原子 journal mutation（state/checkpoint/event/message 同事务）；
- SQLite event sequence 与 expected state/version 冲突保护；
- 从 SQLite 导出当前 JSONL envelope；
- storage contract tests 和 crash-before-commit rollback test；
- migration v2 的 model/tool call journal、versioned snapshot、interrupt/resume、lease 和 edit reconciliation；
- OpenAI-compatible/Anthropic adapter、统一错误分类、持久 retry/backoff、显式 fallback 和 secret redaction。
- M3.1 的 model capability/token counter、固定 context sections、hard retention、high-water 和 deterministic manifest；
- M3.2 的 SummaryRecord、SQLite `summaries`、event range/hash lineage、stale/supersede、required-fact verifier 和有界 compression fallback；
- M3.3 的严格 JSON eval suite、fresh repetition、trusted oracles、failure denominator、metrics、recovery 和 paired A/B report。
- M4.1 的 `restricted_test` OS isolation foundation：Linux rootless namespace、只读
  rootfs、默认禁网、环境/资源限制、进程清理、capability probe/fail-closed 和攻击测试。
- M4.2 的 `run_command` 只接受固定 command profile、精确 executable allowlist、结构化
  argv 和 workspace-relative cwd；非零退出是 observation，网络/扩展资源/approval profile
  在没有授权通道时 fail closed；非幂等 crash 进入 `WAITING_APPROVAL`。
- Release/Evidence Hardening 已完成：recovery metrics 已将 `RESUME_STARTED` 纳入
  recovery event；默认 75 个测试、70% statement coverage、release-surface mypy、GitHub
  Actions 离线 CI 和手动凭据门控 Provider smoke 已建立。
- 当前固定 eval 有 13 个 case、覆盖 6 个 fixture，valid 13、基础设施失败为 0；新增跨文件定位、多文件恢复、范围约束和长历史 compression，但失败仍是预设 scripted/oracle 场景，因此它只能作为小型 scripted/offline 证据，不能证明真实 Coding 任务不需要 search 或 Git。详细证据见 `docs/m5-eval-expansion.md`。

下一窗口先使用 `docs/m5-eval-expansion.md` 的 13-case/6-fixture 证据和真实 Provider
baseline 门禁；只有出现新的、可重复的真实模型 failure coverage 后才进入 M5 的
eval-driven capability expansion 设计，当前不新增工具。保持 M4.1/M4.2 的 OS isolation
foundation；不要加入 shell 字符串、默认网络或多 Agent。

## 5. 完成一次开发后的交接动作

- 更新 `docs/current-state.md` 中“已实现/未实现/技术债”；
- 勾选当前里程碑文档对应 checklist；M2、M3 和 M4.1/M4.2 的 checklist 已完成；
- 更新 `docs/roadmap.md` 的子阶段状态；
- 运行全量测试、Ruff、calculator 和 todo smoke；真实 live smoke 因无 Provider 凭据未执行；
- 在最终说明中给出测试数量、golden 状态、迁移版本、覆盖率/类型检查/CI 状态和仍未实现项；当前 schema 为 v3，M4.1/M4.2 native backend 不等同于 OCI container；
- 不使用“生产可用”“完全安全”等超出证据的表述。

## 6. 新窗口建议首条指令

在 `/home/hmli/code/coding-agent` 作为工作目录打开新窗口，然后使用：

```text
请先完整阅读 AGENTS.md、docs/HANDOFF.md、docs/current-state.md、
docs/contracts.md、docs/requirements-traceability.md、docs/roadmap.md 和
docs/m4-implementation-plan.md、docs/m5-eval-expansion.md。运行 75 个默认测试的基线后，
当前 M2.1—M2.3、M3.1—M3.3、M4.1/M4.2 已完成，固定 eval 为 13-case/6-fixture；只评估
M5 的新能力是否被真实 failure coverage 证明需要，
保持现有 OS isolation、structured argv 和 ToolHarness 边界，不增加 shell 字符串、
默认网络或多 Agent。若确有能力扩展，先写 decision record 和验收测试，再同步
current-state、roadmap、repository-structure、HANDOFF 和测试证据。
```

若新 Agent 建议扩大范围，先要求它指出当前 roadmap 门禁或 acceptance criteria 需要该变化；无法对应时不采纳。
