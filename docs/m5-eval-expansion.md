# M5 Eval-driven Capability Expansion 评测证据记录

> 记录日期：2026-09-05
> 当前决策：M5 的评测扩展已完成；已完成 DeepSeek 3 次重复的探索性 live Eval，并修复一个上下文协议边界问题。search_files、Git inspection、patch/edit 增强和依赖准备等能力扩展暂不实现。

## 1. 这次工作要回答什么问题

M5 不是“工具越多越好”的阶段，而是要用任务失败证据回答：

1. 现有 read_file、edit_file、restricted_test、run_command 是否覆盖了更多真实 Coding 任务？
2. 如果任务失败，失败原因是模型决策、Runtime、工具边界、测试失败，还是评测基础设施？
3. 是否反复出现“无法定位文件”或“无法理解修改范围”等相同瓶颈？
4. 新工具带来的成功率提升，是否值得增加权限、schema、提示词和安全边界成本？

在这些问题有明确证据前，不应直接实现 search_files 或 Git 工具。

## 2. 当前评测扩展

扩展前固定 suite 有 7 个 case、2 个 fixture，主要覆盖 calculator 和 todo-cli。现在增加了 4 个独立的小仓库 fixture：

| Fixture | 文件总行数 | 主要目的 |
|---|---:|---|
| mini_repos/checkout_service | 69 | 跨文件定位和错误路径探针 |
| mini_repos/order_pipeline | 47 | 多文件 bug、第一次测试失败后继续修复 |
| mini_repos/settings_service | 55 | 指定文件范围和 changed-path oracle |
| mini_repos/long_history | 150 | 较长历史、context high-water 和 compression |

每个 fixture 的总行数都在 20～200 行范围内。它们都是独立的、可复制的小仓库；评测运行时仍由 WorkspaceManager 复制到隔离 workspace，不能修改 source fixture。

扩展后 suite 共 13 个 case、6 个 fixture：

manifest 使用 `case_type=task|negative_control` 明确区分 9 个正常任务和 4 个故意失败的
负控制。报告仍保留历史全体 run 聚合字段，但能力成功率应读取 `task_runs`，负控制只读取
`negative_control_runs.observed_failure_rate`。

| Case | 类型 | 观察目标 | Oracle |
|---|---|---|---|
| checkout-cross-file-location | 成功 | 先遇到错误路径，再跨模块定位 pricing.py 并修复 | test、file、changed_paths |
| checkout-location-failure | 预设失败 | 只尝试错误路径后无法完成任务 | test、file、changed_paths |
| pipeline-multi-file-recovery | 成功/恢复 | 第一处修改后测试失败，再找到第二个模块并修复 | test、两个 file、changed_paths |
| settings-scoped-edit | 成功 | 只修改允许的 settings.py | test、file、changed_paths |
| settings-scope-violation | 预设失败 | 测试通过但额外修改 README，触发范围 oracle | test、file、changed_paths |
| long-history-compression | 成功 | 重复读取长历史，触发并完成摘要压缩 | file、result_schema、changed_paths |

原有 calculator/todo case 继续保留。因此原有四份 semantic golden、M1/M1.5 垂直切片和 M4.1/M4.2 执行边界没有被替换。

## 3. 每类评测如何提供程序化证据

### 3.1 跨文件定位

checkout_service 的调用关系是：

    test_checkout.py
        ↓
    checkout.py
        ↓
    pricing.py
        ↓
    catalog.py

成功脚本故意先读取不存在的 discounts.py，然后读取 checkout.py、catalog.py 和 pricing.py，最后只修改 pricing.py。这样报告会保留一次 workspace_path 工具失败，同时可以验证任务最终是否成功。

这能说明当前 read_file 只接受已知路径；它不能证明真实模型已经需要 search_files。因为本 case 使用的是预先编写的 ScriptedBackend，路径尝试顺序是人为给定的。

checkout-location-failure 是对应的受控负例：脚本只读取错误路径后结束，test/file oracle 失败。它用于验证失败仍保留在分母和 failure taxonomy 中，不是生产模型成功率。

### 3.2 多文件 bug 和测试失败后的继续定位

order_pipeline 初始同时有两个实现错误：

- normalization.py 没有去掉输入两侧空白；
- formatting.py 没有输出美元符号。

脚本先修复 normalization.py，再运行测试；第一次测试失败被作为 observation 记录，然后继续读取并修复 formatting.py，最后再次运行测试。

这个 case 验证：

- 测试失败不是 Runtime 崩溃；
- 模型可以收到失败 observation 后继续调用工具；
- changed_paths 可以确认两个实现文件发生变化；
- 测试文件和 README 没有被修改。

### 3.3 修改范围

settings-scoped-edit 验证只修改 settings.py 的成功路径。

settings-scope-violation 故意同时修改 settings.py 和 README.md。测试本身通过，但 changed_paths oracle 失败。这个结果很重要：当前系统能在评测结束时发现范围越界，但还没有一个通用的、由任务 manifest 直接注入 Harness 的“每个任务允许修改哪些文件”的写权限策略。

因此目前不能宣称已经实现了通用的 pre-write allowed-path policy。它只是一个可审计的 post-run oracle。是否需要把范围约束提前到 Harness，应该由真实任务失败证据决定，而不是在没有需求时扩大写权限策略。

### 3.4 长历史和 context compression

long_history fixture 由 150 行的确定性 audit history、测试和 README 组成。脚本重复读取 history.py，使用 compressed variant 运行时触发 context high-water。

这次实际结果：

- compression_input_tokens：220；
- compression_output_tokens：70；
- compression_rejections：空；
- case task_success：true；
- source invariant：true。

该 case 没有把 test profile 放在压缩路径之后，避免 unittest 生成 __pycache__ 改变 workspace revision，干扰摘要 stale lineage。fixture 的 unittest 仍然单独运行并通过；Eval case 使用 file/result/changed-path oracle 验证只读任务。

## 4. Scripted Eval 结果

### 4.1 Budgeted variant

运行命令：

    PYTHONPATH=src python3 -m coding_agent.cli \
      --agent-home /tmp/coding-agent-m5-budgeted \
      evaluate \
      --suite examples/eval_suite.json \
      --variant budgeted \
      --repetitions 1

结果：

| 指标 | 结果 |
|---|---:|
| case_count | 13 |
| requested_run_count | 13 |
| valid_run_count | 13 |
| infrastructure_failure_count | 0 |
| task_success_rate | 0.6923（9/13） |
| runtime_completion_rate | 0.9231（12/13） |
| source_invariant_rate | 1.0 |
| recovery_rate | 1.0 |

分类后的指标：正常任务 9/9 成功、Runtime completion 9/9；4 个负控制 observed failure
rate 为 4/4，Runtime completion 为 3/4。原始 `task_success_rate=0.6923` 仅为兼容历史报告，
不再作为正常任务成功率。

失败由预设场景组成：

- 1 个 invalid_script_response；
- 3 个 oracle_failure，包括 task-fail、定位失败和范围越界；
- 2 个 workspace_path，来自定位探针的错误路径读取；
- 3 个 `compression:summarizer_unconfigured`，表示 budgeted variant 发现需要压缩但该
  variant 没有配置 summarizer；它们是可观察的有界 fallback，不是基础设施失败，也没有
  让对应任务失败；
- 没有 infrastructure_failure。

这里的 0.6923 只是这个小型 scripted suite 的描述性结果，不能当作真实模型 Coding 成功率。

### 4.2 Compressed variant

同一个 13-case suite 使用 compressed variant：

| 指标 | 结果 |
|---|---:|
| case_count | 13 |
| valid_run_count | 13 |
| infrastructure_failure_count | 0 |
| task_success_rate | 0.6923 |
| runtime_completion_rate | 0.9231 |
| source_invariant_rate | 1.0 |
| recovery_rate | 1.0 |

分类后的指标同样为正常任务 9/9 成功、负控制 4/4 被 oracle/Runtime 识别。

long-history-compression 成功产生压缩输入/输出 Token 记录，且没有 compression rejection。其余 case 没有因为压缩配置错误而被算作基础设施失败。

## 5. Passthrough / Budgeted A/B

使用同一 suite、同一 repetition，进行单变量 context policy A/B：

    Baseline：passthrough
    Variant：budgeted

两边各 13 个 run，case/repetition key 完全匹配：

| 指标 | Passthrough | Budgeted |
|---|---:|---:|
| task success rate | 0.6923 | 0.6923 |
| Runtime completion rate | 0.9231 | 0.9231 |
| source invariant rate | 1.0 | 1.0 |
| mean tool calls | 2.8462 | 2.8462 |
| mean model calls | 3.8462 | 3.8462 |
| mean input tokens | 385.77 | 385.77 |
| mean output tokens | 56.77 | 56.77 |

单次 repetition 的 latency 有轻微浮动，但没有 task-success 或工具调用改善。由于这是 scripted backend、样本量只有 13 且只重复一次，不能据此声称某个 context policy 在生产环境更好。

## 6. 真实 Provider 基线状态

项目已有：

- OpenAI-compatible adapter；
- Anthropic adapter；
- retry/backoff 和错误分类；
- 凭据门控的 tests/live_provider_smoke.py；
- 手动 GitHub Actions live-provider-smoke workflow。

本次显式运行：

    PYTHONPATH=src python3 -m unittest tests.live_provider_smoke -v

结果：

    首次配置 DeepSeek 后，测试实际发起了 Provider 请求，但断言失败：response.text 为空。
    这不是凭据门控 skip。

初始 smoke 使用 `max_output_tokens=16`。DeepSeek 当前 thinking 模式默认开启，思考内容和
最终 `content` 分开返回；在很小的输出预算下，可能只得到 `reasoning_content`，而最终
`content` 为空。官方协议说明了该字段分离和 `thinking: disabled` 开关。

因此本次修复做了三件事：

- smoke 的输出预算提高到 256；
- OpenAI-compatible adapter 支持显式 `thinking: disabled`；
- CLI、Eval backend 和 DeepSeek smoke 都可以传递该设置，且 DeepSeek smoke 对官方 base URL
  默认关闭 thinking。

当前仍未把 `reasoning_content` 写入消息、SQLite 或 trace，也没有声称支持 DeepSeek thinking
模式下的多轮工具回传。用户随后以 `thinking: disabled` 重新执行，单次 DeepSeek live adapter
smoke 已成功。随后完成了一次 13-case 的探索性 live Eval；多次 live baseline 仍待执行，且
smoke 的首次失败不作为工具缺口证据。

单次 smoke 已通过；下一步是在固定 suite 上做至少多次重复，并记录：

- task success；
- Runtime completion；
- tool call 顺序和数量；
- input/output/compression Token；
- model/tool/test/run latency；
- failure taxonomy；
- recovery；
- source invariant；
- provider/model/base URL 及 suite 版本。

为避免复制或改写 13-case scripted manifest，CLI 支持 provider override。DeepSeek 基线命令为：

    PYTHONPATH=src .venv/bin/python -m coding_agent.cli \
      --agent-home /tmp/coding-agent-deepseek-eval \
      evaluate --suite examples/eval_suite.json \
      --provider openai-compatible --model deepseek-v4-flash \
      --base-url https://api.deepseek.com --thinking disabled \
      --repetitions 3 --variant budgeted \
      --output /tmp/coding-agent-deepseek-eval/report

override 只替换每个 case 的 model backend；fixture、程序化 oracle、fault/resume 字段和
报告输出仍来自固定 suite。输出的 `manifest.snapshot.json` 会记录实际 provider 配置，
不会记录 API key。

### 6.1 单次 DeepSeek live Eval（探索性结果）

报告已写入：

    /tmp/coding-agent-deepseek-eval/report/report.json

本次使用 `budgeted` variant、13 个 case、每个 case 1 次运行。只从报告提取以下汇总：

| 指标 | 结果 |
|---|---:|
| valid runs | 13/13 |
| infrastructure failures | 0 |
| task success rate | 0.6923（9/13） |
| Runtime completion rate | 0.8462（11/13） |
| source invariant rate | 1.0 |
| permission violations | 0 |
| mean model calls / tool calls | 3.6154 / 4.6923 |
| mean latency | 6031 ms |

这个 9/13 不能作为最终真实模型成功率。provider override 会移除 scripted backend 的
`responses`，因此 `calculator-runtime-fail`、`calculator-task-fail` 等预设负控制不再保持
原本的 scripted fault 语义；它们仍可帮助检查报告分类，但不应直接用于工具能力排名。

本次 live trace 暴露了一个协议风险：`long-history-compression` 的上下文裁剪保留了
assistant 的 3 个 tool call，却只保留了其中 2 个 tool result，DeepSeek 因缺少对应
`tool_call_id` 返回 `invalid_request`。已在 `BudgetedContextBuilder` 中修复：assistant
tool-call turn 与全部结果现在作为不可拆分组参与裁剪；完整组无法放入预算时分类为
`context_required_content_exceeds_budget`，不再发送非法 Provider 请求；不完整历史则以
`context_invalid_tool_history` fail closed。新增了两个 M3.1 回归测试。

另一个值得继续观察的信号是 `checkout-location-failure` 中出现了 4 次
`workspace_path`（模型重复尝试绝对路径）。这说明真实模型可能需要更好的路径定位提示或能力，
但单次运行且该 case 本身是定位失败控制，尚不足以批准 `search_files`。

### 6.2 三次重复 DeepSeek live Eval

修复后使用相同的 `budgeted` 命令运行 3 次，报告仍保存在：

    /tmp/coding-agent-deepseek-eval-postfix/report/report.json

仓库同时保存脱敏聚合、逐 case 计数、原始 artifact SHA-256 与 provenance 限制：
[`evidence/deepseek-budgeted-2026-09-05.summary.json`](./evidence/deepseek-budgeted-2026-09-05.summary.json)。
它不包含凭据、session ID、trace path 或模型原文；原始 `/tmp` artifact 未入库，因此该摘要提升
了审计性，但不能替代具有稳定 artifact 存储与精确代码 revision 的重跑证据。

| 指标 | 结果 |
|---|---:|
| requested / valid runs | 39 / 39 |
| infrastructure failures | 0 |
| task success rate | 0.8205（32/39） |
| Runtime completion rate | 0.8462（33/39） |
| source invariant rate | 1.0 |
| permission violations | 0 |
| 每次 task success | 11/13、10/13、11/13 |
| 每次 Runtime completion | 11/13、11/13、11/13 |
| recovery case | 3/3 成功，recovery events=3 |

失败分类不是互斥计数：

- `context_required_content_exceeds_budget`：3 次，全部来自 `long-history-compression`。
  该 case 的 `max_output_tokens=12000` 配合 fallback context limit=16384，只剩 4128 个输入
  Token；实际硬保留内容约 4221～4300，且 `budgeted` 没有配置 summarizer，因此现在安全失败，
  不再向 Provider 发出非法请求。需要验证 compression variant 后，才能判断这是预算配置问题还是
  长历史能力缺口。
- `step_budget_exhausted`：3 次，全部来自 `checkout-location-failure` 控制 case。模型反复尝试
  读取/修改，但没有在 16 步内完成；这属于重复的行为信号，但该 case 是预设失败控制，不能单独
  证明需要 `search_files`。
- `oracle_failure`：6 次，来自两个原本用于 scripted fault/不可能任务的负控制；provider override
  移除了它们的 scripted response，所以不纳入真实模型能力评分。
- `command_executable_denied`：4 次，是非致命 observation；模型尝试使用不在 profile allowlist
  中的 `python`，随后仍完成相关任务。这证明 M4.2 边界在工作，不是新增工具证据。
- `compression:summarizer_unconfigured`：3 次，是 `budgeted` 的有界 fallback 记录，不是基础设施失败。

三次重复没有出现 `workspace_path` 失败、权限违规、基础设施失败或新的 Provider 协议错误。
因此当前证据仍不足以批准 search/Git；下一步优先运行同一 suite 的 `compressed` variant，单独
验证长历史上下文路径，再决定是否需要 context 配置/压缩改进。

## 7. M5 工具扩展决策

当前证据的结论是：

1. 新增 fixture 后，现有四个工具仍能完成跨文件、多文件、范围受限和长历史示例。
2. 预设的定位失败和范围越界能被 oracle 捕获，但它们不是由真实模型反复产生的 failure coverage。
3. ScriptedBackend 能验证 Runtime、Harness、oracle 和报告语义，不能证明真实模型需要某个新工具。
4. A/B 没有显示新增工具或当前 context policy 带来可归因的成功率提升。
5. 已完成 3 次探索性 live Provider Eval，但由于负控制混入、budgeted 长历史预算不足，尚不能作为最终真实模型能力分数。

因此本轮不实现：

- search_files；
- git_status；
- git_diff；
- 通用 Shell；
- 默认网络；
- 依赖安装工具；
- patch/edit 增强。

M5 继续保持“条件阶段”。只有真实模型在多次重复中明确出现以下模式，才进入下一轮设计：

| 失败证据 | 候选能力 |
|---|---|
| 因无法定位文件失败，且重复盲目 read_file | search_files |
| 因无法理解修改范围失败，且 Git 状态信息是必要事实 | git_status/git_diff |
| 精确替换反复失败且目标内容可安全定位 | patch/edit 增强 |
| 依赖缺失是任务失败主因，且存在可信 profile 需求 | 可信 dependency profile |

进入设计前仍必须先写 decision record、权限增量、schema、成功/失败/恢复验收测试，并做单变量 A/B。若真实 baseline 仍没有稳定工具缺口，当前版本可作为 v0.1.0 工程基线，转向 CI、发布文档和面试演示材料。

## 8. 当前进度

以路线图计算：

- M0～M4.2：已完成；
- Release/Evidence Hardening：已完成；
- M5 评测门禁扩展：本轮完成；
- M5 新工具能力：0%，尚未满足启动条件；
- 真实 Provider 多次 baseline：DeepSeek 已完成 3 次探索性重复；需补充 compressed variant 并单独解释负控制后再形成工具决策；
- 当前总体工程进度：约 90% 的既定路线图已实现，剩余部分取决于真实模型证据，不按工具数量估算。

下一步不是自动新增工具，而是使用同一真实 Provider 执行 `compressed` variant，验证长历史 case 的压缩路径；在此之前继续保持现有安全边界和四工具集合。

## 9. 门禁清单

- [x] 保持 82 个默认 unittest、四份 semantic golden 和 M4.1/M4.2 边界测试通过。
- [x] 将新 fixture 纳入 Ruff、compile 和 CI 的 calculator/todo scripted smoke 边界。
- [x] 扩展到 13 个 case、6 个 fixture，并为每个 case 配置程序化 oracle。
- [x] 覆盖跨文件定位、多文件测试失败恢复、changed-path 范围和长历史 compression。
- [x] 运行 budgeted/compressed Eval，并确认 infrastructure failure 为 0。
- [x] 运行 passthrough/budgeted 单变量 A/B，并确认 paired keys 与指标可比较。
- [x] 修复并回归 `RESUME_STARTED` recovery event 计数（现有代码已覆盖）。
- [x] 配置真实 Provider 后完成单次 live adapter smoke。
- [x] 配置真实 Provider 后对新版 suite 完成 3 次 live baseline（结果仍标记为探索性，待 compressed variant 补充）。
- [x] Eval manifest/report 分离正常任务与负控制统计。
- [x] 保存脱敏 live-eval 汇总、逐 case 计数、原始 artifact hash 与 provenance 限制。
- [x] 修复 Provider usage 协议校验与异常 model journal closure。
- [x] 为普通 Tool handler 增加可终止的 Linux worker 进程边界，保留 sandbox 自有超时边界。
- [ ] 只有 live failure coverage 明确指向工具缺口时，才新增工具 decision record 和实现。
