# 新窗口开发交接

> 交接基线：2026-09-06，M2.1—M2.3、M3.1—M3.3、M4.1、M4.2、M5.1 已完成。下一项仍须由 Eval failure coverage 决定，不是重写 M1/M1.5。
> 固定版本：`v0.1.0`；安装、测试、离线 Eval、Demo 和支持边界见 `docs/releases/v0.1.0.md`。

## 1. 开始前必须做

1. 将工作目录切换到 `/home/hmli/code/coding-agent`。
2. 完整阅读 `AGENTS.md`、`docs/README.md`、`docs/current-state.md`。
3. 阅读 `docs/m4-implementation-plan.md` 的 M4.2 范围和 `docs/m5-eval-expansion.md`；M2/M3/M4.1 的实施记录分别在 `docs/m2-implementation-plan.md`、`docs/m3-implementation-plan.md` 和该文档中。
4. 运行：

```bash
PYTHONPATH=src python3 -m unittest discover -v
```

当前基线应为 93 个默认测试通过。`tests/live_provider_smoke.py` 是凭据门控的显式 smoke，不属于默认 discovery。若不是，先定位环境或已有变化，不要直接扩展 M5。

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

## 4. M2/M3/M4/M5.1 完成事实与下一步推荐入口

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
  recovery event；默认 93 个测试、70% statement coverage、23/33 源码文件 mypy、GitHub
  Actions 离线 CI 和手动凭据门控 Provider smoke 已建立。
- 当前固定 eval 有 14 个 case、覆盖 7 个 fixture；Eval manifest 显式区分 10 个正常任务与
  4 个负控制。能力主指标是正常任务的 `oracle_success` 和
  `end_to_end_success = oracle_success && runtime_completed`，负控制不混入能力分母。
- Provider usage 严格校验、model journal 异常 closure、ToolHarness worker 强制超时、`uv.lock`
  和 subprocess/multiprocessing coverage 合并已经加入；native runner 仍由安全集成测试提供证据。
- OpenAI-compatible adapter 现支持显式 `thinking: disabled`；DeepSeek smoke 首次请求曾因 16
  token 输出上限得到空 `content`，已增加预算并关闭 DeepSeek thinking；用户已重新运行并
  验证 smoke 成功。
- `evaluate` 现支持 provider override，可直接对固定 suite 运行真实 backend；使用
  `--provider openai-compatible --model ... --base-url https://api.deepseek.com --thinking disabled`
  和 `--repetitions 3`。修复后已完成 3 次 DeepSeek `budgeted` 探索性 Eval：39/39 valid、基础设施失败=0、
  task success=0.8205、Runtime completion=0.8462、source invariant=1.0、permission violations=0；
  其中 scripted 负控制和长历史预算问题需要单独解释，不能把 0.8205 当作最终 live baseline。
- 真实 DeepSeek Eval 暴露的上下文裁剪拆分 assistant tool-call 组问题已修复。长历史 compressed
  定向 3 次使用真实 Provider 摘要器后不再有 context/schema failure，端到端 2/3；剩余一次为
  `tool_budget_exhausted`。
- 路径 schema/系统提示已明确 workspace-relative 契约。pipeline 定向 10 次无 invalid path，
  原始 9/10 唯一 oracle failure 是等价实现，现已用 `contains_any` 修正过窄 oracle。
- 独立 `search_lab` no-search 基线端到端 0/10，最小只读 `search_files` 候选为 6/10、oracle
  8/10；因此 M5.1 已批准并实现。它只支持大小写敏感 UTF-8 字面量搜索，具有文件/字节/结果/
  timeout 上界，不支持 regex/glob/Shell/Git/网络。输入 Token 73,073→147,027、仍有 4 次预算
  失败，均作为限制保留。证据见 `docs/decisions/m5-1-search-files.md` 与 `docs/evidence/`。
- `examples/search_eval_suite.json` 把搜索覆盖扩大到 3 个不同结构仓库。当前提示与紧凑
  search-first 提示各做 15 次 DeepSeek：端到端 12/15→13/15、oracle 12/15→14/15、输入
  Token -11.5%、first-search 中位位置 4→1；提示已保留，8-call 预算未提高，仍有 2 次耗尽。
- 后续修复了 `remaining_budgets` 每轮重复初始上限的问题，并加入停止无关读取、预留测试调用
  的紧凑指引。同一 benchmark 新 15 次端到端/Runtime 为 14/15、oracle 14/15、预算耗尽
  1 次；工具尝试略降但输入 Token 增加 9.0%，因此保留真实预算契约，不外推效率结论。
- 四类未参与三仓库提示 A/B 的 capability holdout 已通过 scripted 4/4 和 DeepSeek 12/12；
  live 运行没有基础设施失败、无效调用、权限违规、预算耗尽，也没有暴露 Git、patch/edit 或
  依赖能力缺口。证据见 `docs/evidence/deepseek-m5-capability-holdout-2026-09-06.summary.json`。

当前不启动 M5.2，冻结五工具能力集合。实现提交 `0ccd434`、证据提交 `cf82f3c` 已推送；后者
触发的 GitHub Actions run `34035706601` 在 Python 3.10/3.11 两个 quality job 上成功。
`v0.1.0` 已固定为发布基线；只有未来新的、可重复的 failure coverage 才能批准 Git
inspection、patch/edit 增强或依赖准备。
项目展示优化已完成 README 中英双语重构、首屏验证数据和 Mermaid 架构图；后续可选工作是
录制 2—3 分钟终端 Demo/GIF，不因此改变 Runtime 能力边界。
保持 M4.1/M4.2 的 OS isolation foundation；不要加入 shell 字符串、默认网络或多 Agent。

## 5. 完成一次开发后的交接动作

- 更新 `docs/current-state.md` 中“已实现/未实现/技术债”；
- 勾选当前里程碑文档对应 checklist；M2、M3 和 M4.1/M4.2 的 checklist 已完成；
- 更新 `docs/roadmap.md` 的子阶段状态；
- 运行全量测试、Ruff、calculator 和 todo smoke；DeepSeek live smoke、compressed 定向、
  M5.1 search A/B、budget-aware follow-up 和 capability holdout 已有脱敏证据；不必在没有
  新假设时重复消耗 Provider 配额；
- 在最终说明中给出测试数量、golden 状态、迁移版本、覆盖率/类型检查/CI 状态和仍未实现项；当前 schema 为 v3，M4.1/M4.2 native backend 不等同于 OCI container；
- 不使用“生产可用”“完全安全”等超出证据的表述。

## 6. 新窗口建议首条指令

在 `/home/hmli/code/coding-agent` 作为工作目录打开新窗口，然后使用：

```text
请先完整阅读 AGENTS.md、docs/HANDOFF.md、docs/current-state.md、
docs/contracts.md、docs/requirements-traceability.md、docs/roadmap.md 和
docs/m4-implementation-plan.md、docs/m5-eval-expansion.md。运行 93 个默认测试的基线后，
当前 M2.1—M2.3、M3.1—M3.3、M4.1/M4.2/M5.1 已完成，固定 eval 为 14-case/7-fixture；只评估
后续 M5 能力是否被真实 failure coverage 证明需要，
保持现有 OS isolation、structured argv 和 ToolHarness 边界，不增加 shell 字符串、
默认网络或多 Agent。若确有能力扩展，先写 decision record 和验收测试，再同步
current-state、roadmap、repository-structure、HANDOFF 和测试证据。
```

若新 Agent 建议扩大范围，先要求它指出当前 roadmap 门禁或 acceptance criteria 需要该变化；无法对应时不采纳。
