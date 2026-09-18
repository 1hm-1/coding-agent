# 新窗口开发交接

> 交接基线：2026-09-18，M2.1—M2.3、M3.1—M3.3、M4.1、M4.2、M5.1、Phase 2 P2-M1 Headless Runtime IPC、P2-M2 Layered Memory 及 S3 paired live harness 已完成；P2-M3 尚未激活。
> 固定版本：`v0.1.0`；安装、测试、离线 Eval、Demo 和支持边界见 `docs/releases/v0.1.0.md`。

## 1. 开始前必须做

1. 将工作目录切换到 `/home/hmli/code/coding-agent`。
2. 完整阅读 `AGENTS.md`、`docs/README.md`、`docs/current-state.md`。
3. 阅读已完成的 `docs/p2-implementation-plan.md`、`docs/p2-m2-implementation-plan.md`、
   `docs/p2-m2-retrieval-optimization.md` 与 Runtime IPC 权威规范；开始 P2-M3 前等待用户明确激活。
4. 运行：

```bash
PYTHONPATH=src python3 -m unittest discover -v
```

本轮收口为 143/143 个默认测试通过（`6c47a7d` 的 142 个无回退，新增 1 个通用边界标点
development test）。Ruff、34 文件 mypy、compileall 与 git diff --check 也通过。
`tests/live_provider_smoke.py` 是凭据门控的显式 smoke，不属于默认 discovery。若不是，先定位环境或已有变化。

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
- Phase 2 P2-M1 producer 与 P2-M2 episodic/semantic Memory 已实现；多 Agent、终端、Skill/MCP、
  procedural memory、RAG/vector 仍未激活，没有明确里程碑不得加入代码或依赖。
- `v0.1.0` 没有 `protocol-info`、`run-headless` 或 Runtime IPC v1；这些能力只属于当前 `0.2.0.dev0` 开发树。

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
- 简历 benchmark 已补正常任务专属 total/mean/P50/P95、端到端 wall latency、摘要 Token/调用
  归集、固定 case 选择和 pair 内交替 A/B。scripted 校准及 `deepseek-flash` live 25-run/
  10-pair 均已完成：稳定性端到端 24/25，压缩 A/B 为 9/10→10/10，但 compressed 总 Token
  增加 339,864，不能宣称节省 Token。计划的 `deepseek-v4-flash` 已从 Provider model list 消失，
  且 live worktree 非 clean；精确 snapshot/artifact hash 与限制见 `docs/resume-benchmark.md`。

当前不启动旧路线 M5.2，冻结五工具能力集合。实现提交 `0ccd434`、证据提交 `cf82f3c` 已推送；后者
触发的 GitHub Actions run `34035706601` 在 Python 3.10/3.11 两个 quality job 上成功。
`v0.1.0` 已固定为发布基线；只有未来新的、可重复的 failure coverage 才能批准 Git
inspection、patch/edit 增强或依赖准备。
Phase 2 P2-M1 已完成：`protocol-info`、严格 request validator、scripted/real backend headless
composition、提交后公共事件投影、唯一 terminal result、稳定退出码、cooperative SIGINT/deadline、
checkpoint-before-result 和子进程清理均有 producer contract tests。111 个默认测试、Ruff、26 个
配置范围源码文件 mypy、compileall、76.8% coverage、wheel/sdist、独立 wheel discovery 与两个
既有 smoke 均通过。Agent Platform consumer 不在本仓库实现。

Phase 2 P2-M2 已完成：SQLite schema v4、versioned episodic/semantic record、committed journal
provenance、scope/revision/content policy、proposal/explicit approval/stale/supersede/delete、原子审计、
bounded lexical retrieval、Context manifest 与 multi-task cold/warm calibration 均有测试。删除 tombstone
清空原文；unbounded Context preview 不重复写 retrieval audit。P2-M2.1 原收口门禁为 136/136；
加入 L3 holdout contract test 后当前默认测试为 137/137，Ruff、33 文件 mypy、compileall、78.5%
statement coverage、wheel/sdist、独立 wheel
Memory import、calculator/todo smoke 和多任务 Memory benchmark，均已通过。当前 benchmark 已冻结
12 个 case，覆盖相关/无匹配/词面 distractor/scope-revision 隔离/stale-deleted/诱导指令拒绝；trusted
oracle 为 cold 8/12 → warm 12/12，同进程 A/B 的 relevant recall 保持 1.0，precision 2/3→1.0、
irrelevant injection 1/3→0，retrieval Token 116→81，Memory context/额外模型 Token 803→130
（-83.8%），scripted model after total 2740→2870。scope/revision/stale-deleted 泄漏为 0，原始
3-case warm 3/3。

本轮实际 wall latency 为 before cold/warm mean
`45.834164333731074ms`/`47.033260334198225ms`（`+1.1990960004671507ms`），after
`45.834164333731074ms`/`45.95990916732262ms`（`+0.12574483359154698ms`）；检索 mean
`0.056614917411934584ms`→`0.06059541647118749ms`（`+0.003980499059252907ms`）。延迟受
本机调度影响，但回退原样保留，完整摘要见
[`docs/evidence/memory-cold-warm-2026-09-16.summary.json`](./evidence/memory-cold-warm-2026-09-16.summary.json)。
本次未运行真实 Provider，因此不宣称真实模型净收益。Memory 的交付形态明确为显式 Python
composition：默认 `AgentApplication` 与 `run-headless` 不创建、查询或注入 Memory，也不把它加入
Runtime IPC capability；只有未来扩大真实任务/Provider A/B 并证明净收益后，才重新评估默认入口或
IPC 集成。S2 复核结论为有条件通过，并修复了 before 自定义 renderer 下 record 级 Context Token
归因与实际注入不一致；冻结 A/B 总 Token 和 task 指标不变。仍需真实 Provider A/B，P2-M3
仍未激活。

L3 非同源 holdout 已在 `8ebc800` 基线上完成并冻结：18 个 case 使用三个共享 pool（6/7/5），
manifest SHA-256 为 `3d4bffb06a19ee219534d4649d93e983e7ecd14cebfd90b84ae64feb1f7e2ed1`，并加入
`5298ba0` 三任务共享池兼容 arm。首轮 warm task success 为 `13/18`，relevant recall
`0.6666666666666666`，irrelevant injection `0.16666666666666666`，所以 recall/injection 门槛
未通过；scope/revision/stale-deleted 泄漏为 0、无相关 Memory 行为变化为 0、manifest Token 一致、
兼容 arm warm 3/3。该结果已原样保存，未根据结果修改 case；后续重复运行只补采 latency，不重新
宣称首轮。S3 进一步确认其不是一例一池且会跑 Runtime task result，但 task/memory 仍多为关键词
近同构改写，deterministic oracle 直接持有答案，因此不能作为强非同源 Provider 证据。失败只分类，
没有据此调参；后续 development set 是另一组 L1/L2 冻结 12-case 数据，算法若再修改必须在查看
结果前冻结全新的 Holdout v2。

S3 已新增 `evaluate-memory-live` paired harness：同一 Provider/model/task/budget/source revision/oracle，
pair 内交替 off/on；Memory 必须来自先前 committed Runtime completion event。脱敏报告包含 Token、
retrieval、latency、工具/失败、first relevant action 与安全不变量，并保留完整 Memory manifest，拒绝
Secret、绝对路径和 reasoning content。当前仅完成离线 contract test，真实 Provider A/B 仍待凭据
门控运行；Memory 继续显式 opt-in，P2-M3 未激活。

L3.5 Holdout v2 已在 S3.5 算法冻结后完成唯一一次有效执行：正文仍由用户在仓库外保管，共享仓库
保留 metadata 和 S3.6 脱敏 term-level 诊断，不保存原始 prose；manifest 位于
docs/evidence/memory-retrieval-holdout-v2.manifest.json，suite SHA-256 为
d1d9c45d9d06aea211780fa1d6b3d9baf4154891f1a3344ce1ae1cb658907d6f。主 suite 有 20 个 case、4 个
独立任务领域、4 个共享 pool；oracle 只检查代码、测试、文件或工具行为，原始 5298ba0 三任务 arm
单独保留且不计入主 Holdout。algorithm 为 `f1d03cf`、runner 为 `e7287d2`，20/20 case 有效；
manifest 已记录 executed=true、results_generated=true、first_run_reserved=false 与原始结果 hash。

该 runner 为 observation-only，没有 Provider 或 Agent 修改：主 relevant recall `0.0`、precision
`1.0`、irrelevant injection `0.0`、scope/revision/stale-deleted leakage `0`、无关行为变化 `0`、
prompt-injection bypass `0`、Token mismatch `0`，兼容 arm warm `3/3`。task success `0/20` 和
renderer estimator Token `5518` 不是真实模型收益证据；`tokens_per_successful_task` 为 `null`，
原始 cold/warm latency mean 为 `1.7627652014198247ms`/`1.456806949863676ms`。完整脱敏记录见
[`memory-retrieval-holdout-v2-2026-09-18.md`](./evidence/memory-retrieval-holdout-v2-2026-09-18.md)，
原始结果仍在用户保管目录。三次无有效结果的基础设施失败均保留 failure record，未覆盖结果。

S3.6 已离线复核全部 12 个 relevant target：scope/status/expiry/revision 均 eligible；2 个没有
informative lexical overlap，10 个在 `0.60` 绝对阈值前失败，没有 case 到达 relative floor、Token
或 top-k。未发现 runner query mismatch、label error、metric/harness error 或失败运行污染。结论为
**Lexical ceiling**，不再堆 alias；v2 仅保留诊断证据，另建 development set，任何候选算法须先冻结新 v3。
本轮没有修改或重跑 retrieval/runner/Holdout，也没有启用默认 Memory。证据见
[`s3-6-memory-holdout-v2-zero-recall-2026-09-18.md`](./evidence/s3-6-memory-holdout-v2-zero-recall-2026-09-18.md)。

L3.7 已建立 `examples/memory_retrieval_backend_development.json`：40 个明确标记为
`development_data` 的 case，覆盖 8 个代码仓库/任务领域和 8 个共享 Memory pool；分类为
10 zero-overlap、10 low-overlap、8 同主题 hard negative、4 冲突/过时、4 无相关 Memory 和 4 个
边界 case。每个 query 标注 relevant Memory IDs，并带有可由 fixture 验证的结构化 metadata ground
truth。v2 20-case 以 source manifest/hash 和 case metadata 的 reference-only 子集保留，正文不复制回
共享仓库；本项不执行 suite、不改检索算法、不创建 Holdout v3。

S3.5 已只用 L1/L2 development set 与自建通用标点 case 修复 lexical term 边界标点问题；内部
identifier 标点保留，没有新增 alias/literal/末尾词特判。L1/L2 recall、误注入、retrieval/model
Token、scope leakage、三任务兼容 arm 与 manifest attribution 均不回退。历史 L3 测试现在只校验
冻结 evidence/hash，不再用候选算法重跑已知 Holdout；Holdout v2 已按 manifest 记录一次首轮结果，不能
修改 case、算法或重跑以替换该结果。

下一候选是 P2-M3 Profiles/Skill Runtime，但尚未激活。继续保持 M4.1/M4.2 OS isolation，不加入
shell 字符串、默认网络、Skill、MCP、多 Agent、UI、RAG 或 vector backend。

## 5. 完成一次开发后的交接动作

- 更新 `docs/current-state.md` 中“已实现/未实现/技术债”；
- 勾选当前里程碑文档对应 checklist；M2、M3 和 M4.1/M4.2 的 checklist 已完成；
- 更新 `docs/roadmap.md` 的子阶段状态；
- 运行全量测试、Ruff、calculator、todo smoke 和 P2-M2 Memory cold/warm benchmark；DeepSeek live smoke、compressed 定向、
  M5.1 search A/B、budget-aware follow-up 和 capability holdout 已有脱敏证据；不必在没有
  新假设时重复消耗 Provider 配额；
- L3 holdout 的 18-case 首轮结果已冻结并保存脱敏摘要；它在 `8ebc800` 基线上未达到 recall/injection
  门槛，不能用改题或真实 Provider 未运行来填补该证据缺口。L3.5 v2 首轮也未达到 recall 门槛，且
  observation-only runner 不是 Provider A/B；真实 Provider A/B 仍需独立完成；
- 在最终说明中给出测试数量、golden 状态、迁移版本、覆盖率/类型检查/CI 状态和仍未实现项；当前 schema 为 v4，M4.1/M4.2 native backend 不等同于 OCI container；
- 不使用“生产可用”“完全安全”等超出证据的表述。

## 6. 新窗口建议首条指令

在 `/home/hmli/code/coding-agent` 作为工作目录打开新窗口，然后使用：

```text
请先完整阅读 AGENTS.md、docs/HANDOFF.md、docs/current-state.md、已完成的
docs/p2-implementation-plan.md、docs/p2-m2-implementation-plan.md 和 Runtime IPC v1/compatibility
权威规范。P2-M1/P2-M2 已完成，先运行 143 个默认测试确认基线；P2-M3 尚未激活，不得提前加入
Skill、MCP、多 Agent、UI、RAG/vector、shell 字符串或默认网络。
```

若新 Agent 建议扩大范围，先要求它指出当前 roadmap 门禁或 acceptance criteria 需要该变化；无法对应时不采纳。
