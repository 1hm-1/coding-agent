# M3：Context Engineering 与 Evaluation 实施计划

> 状态：已完成（2026-09-05）  
> 前置门禁：M2.1—M2.3 全部完成，恢复与 Provider contract tests 通过  
> 实施顺序：M3.1 Context Budget → M3.2 Compression → M3.3 Eval Harness  
> 真实 Provider live smoke：未执行（环境无凭据）；M3 证据使用离线 ScriptedBackend。

## 1. 目标与非目标

M3 要回答两个可测量问题：长任务如何在有限 Token 内保留决策所需事实，以及 Runtime/模型/上下文策略的效果如何独立评估。

M3 实现：

- provider-aware token accounting 与稳定 context manifest；
- recent/task/repository/summary 分区预算；
- 带 event lineage 和 workspace revision 的结构化压缩；
- versioned eval case、程序化 oracle、batch runner 和报告；
- task success、调用量、Token、延迟、失败原因和 recovery rate；
- passthrough/budget/compression 的可重复 A/B。

M3 不实现：跨用户长期偏好记忆、RAG 平台、Skill routing、多 Agent、在线流量实验或 OS sandbox。测试代码强隔离仍属于 M4。

## 2. M3.1：Context Budget Baseline

### 2.1 接口演进

当前 `ContextBuilder.build(task, messages)` 只适合短历史。M3 在 Domain 中新增可序列化输入/输出：

```python
@dataclass(frozen=True)
class ContextBuildInput:
    session_id: str
    task: str
    messages: Sequence[Message]
    runtime_state: RuntimeState
    policy: RunPolicy
    repository_snapshot: RepositorySnapshot
    latest_summary: SummaryRecord | None
    provider: str
    model: str

@dataclass(frozen=True)
class ContextSection:
    name: str
    messages: tuple[Message, ...]
    estimated_tokens: int
    source_refs: tuple[str, ...]
    truncated: bool = False

@dataclass(frozen=True)
class BuiltContext:
    messages: tuple[Message, ...]
    sections: tuple[ContextSection, ...]
    total_input_tokens: int
    budget_tokens: int
    manifest_version: int

class ContextBuilder(Protocol):
    def build(self, request: ContextBuildInput) -> BuiltContext: ...

class TokenCounter(Protocol):
    def count_messages(self, provider: str, model: str, messages: Sequence[Message]) -> int: ...
```

Runtime 只消费 `BuiltContext.messages`，并将 manifest 摘要写入 event；它不判断应该删除哪些消息。M2 Provider adapter 可以提供精确 counter；未知模型使用显式命名的保守 estimator，不能把字符数估算伪装成精确 Token。

### 2.2 固定装配顺序

```text
system policy + tool protocol
task / runtime state / remaining budgets
repository snapshot + current diff + latest test
validated structured summary（若有）
recent raw messages and tool observations
```

硬保留：system policy、原始 task、当前状态、剩余预算、pending/active call、最近一次 tool result、最近一次 test result。软保留：较旧 conversation、重复文件读取、过期 repository listing。

### 2.3 预算规则

```text
input_budget = model_context_limit
             - reserved_output_tokens
             - provider_protocol_margin
```

- budget 必须来自 model capability registry，不散落 magic number；
- reserved output 使用 `RunPolicy.max_output_tokens`；
- protocol margin 必须可配置并记录；
- 各 section 有 minimum/target/maximum；
- 超预算时先裁软保留 recent history，再触发 M3.2 compression；
- 连硬保留内容都超预算时分类失败 `context_required_content_exceeds_budget`，不能静默丢 task 或最后测试。

### 2.4 Repository snapshot

Snapshot 只能从 isolated workspace 构建，至少包含：

- workspace revision；
- 有上限的文件列表；
- 当前 Git diff summary/允许范围；
- 已读文件及其 content hash；
- 最近测试 profile、pass/fail 和 relevant stderr 摘要。

不要每轮把所有源码塞进 prompt。文件内容来自工具 observation；Context Engine 不建立第二条绕过 Tool Harness 的任意读取路径。若需要内部 repository inspector，必须是可信只读服务且使用同一个 WorkspaceGuard/审计边界。

### 2.5 Events

扩展 `context_built` 或新增版本化事件，稳定 payload 至少包括：

```json
{
  "manifest_version": 1,
  "provider": "...",
  "model": "...",
  "budget_tokens": 10000,
  "total_input_tokens": 7200,
  "sections": [
    {"name": "recent", "tokens": 3100, "truncated": false}
  ],
  "summary_id": null,
  "workspace_revision": "sha256:...",
  "counter": "provider_exact|named_estimator"
}
```

不记录 secret；源码内容继续由受控 observation 保存策略决定，manifest 只记录引用和计数。

### 2.6 M3.1 checklist

- [x] exact/fallback TokenCounter contract tests；
- [x] model capability registry 和未知模型失败/降级测试；
- [x] 固定 section 顺序与 hard-retention tests；
- [x] boundary tests：刚好等于、超 1 Token、required content 超预算；
- [x] deterministic manifest：同一事实输入得到同一 section 投影；
- [x] workspace revision 和 last test 进入 manifest；
- [x] Passthrough builder 仍可作为 eval baseline；
- [x] M1/M1.5 golden 通过，未发生 projection migration。

实现落点为 `context.py`、`domain.py` 和 `workspace.py`；Runtime 在
`BUILDING_CONTEXT` 记录 `context_built` manifest。`tests/test_m3_context.py` 与全量
回归覆盖 exact/fallback、hard retention、边界失败、determinism 和 passthrough。

## 3. M3.2：带来源的压缩

### 3.1 触发条件

只在装配后的预计输入超过 high-water mark 时压缩。不得仅按“消息超过 N 条”触发。建议策略：

```text
high_watermark = 0.85 * input_budget
target_after_compression = 0.65 * input_budget
```

数值放配置并通过 eval 调整。一次压缩失败不得循环无限调用 summary model；受独立 compression call/token/retry budget 约束。

### 3.2 Summary schema

```python
@dataclass(frozen=True)
class SummaryRecord:
    summary_id: str
    schema_version: int
    session_id: str
    source_event_start: int
    source_event_end: int
    source_event_hash: str
    workspace_revision: str
    goals: tuple[str, ...]
    constraints: tuple[str, ...]
    decisions: tuple[str, ...]
    files_read: tuple[FileFact, ...]
    edits: tuple[EditFact, ...]
    tests: tuple[TestFact, ...]
    errors: tuple[ErrorFact, ...]
    unresolved: tuple[str, ...]
    created_at: str
```

Fact 至少有 `source_event_sequence` 和相关文件 `content_hash/revision`。摘要是派生缓存，不覆盖原始 event；数据库保留摘要与源范围，使其可验证和废弃。

### 3.3 生成与验证

```text
select eligible old event range
 → persist compression_started(range/hash/revision)
 → call summarizer through ModelBackend boundary
 → schema validation
 → required-fact verifier
 → persist SummaryRecord + compression_finished
 → next context manifest references summary_id
```

required-fact verifier 至少检查：task/constraint、已修改文件、最后测试结果、当前 unresolved、active failure。验证失败时保留 raw history，并记录 `compression_rejected`；不能使用格式不完整的 summary。

### 3.4 Stale 与纠错

- file fact 的 hash 与当前 revision 不符时标 `stale`；
- 后来的 tool observation 优先于摘要；
- 明确冲突时保留新事实和冲突引用，不把二者拼成一个未经证实事实；
- 错误摘要通过 `superseded_by` 链失效，原记录不可原地篡改；
- workspace rollback/resume 后重新验证 summary revision；
- summary model 输出永远不是权限依据、完成依据或 tool result。

### 3.5 压缩评测指标

- `token_reduction = 1 - compressed_input / passthrough_input`；
- required-fact precision/recall；
- downstream task success delta；
- extra model calls/tokens/latency；
- stale fact rate；
- compression rejection/fallback rate。

禁止只报告“节省约 X% Token”；必须说明固定数据集、baseline、样本数和 task success 是否退化。

### 3.6 M3.2 checklist

- [x] high-water/target 触发边界测试；
- [x] summary schema、event range hash 和 persistence round-trip；
- [x] required facts 缺失时拒绝 summary；
- [x] 文件变化后 stale invalidation；
- [x] bad summary、timeout、provider error 的 fallback；
- [x] 原始 events 仍完整，Replay 不依赖 summary 重写历史；
- [x] passthrough vs compressed 固定场景 A/B；
- [x] 不虚构 Token 改善数字。

实现落点为 `compression.py`、`persistence.py` 和 Runtime 的 compression seam。
摘要模型仍通过 `ModelBackend` 调用；`summaries` 是 SQLite 派生缓存，原始事件仍是
authority。压缩拒绝和 stale invalidation 都有结构化事件。A/B runner 支持
`passthrough`、`budgeted`、`compressed` 变体；没有在未执行固定实验时填写 Token
节省百分比。

## 4. M3.3：Evaluation Harness

### 4.1 EvalCase manifest

优先使用 JSON，避免首版引入 YAML 解析依赖：

```json
{
  "schema_version": 1,
  "case_id": "todo-empty-input",
  "fixture": "examples/todo_cli",
  "task": "Fix the empty input crash and run tests.",
  "backend": {"kind": "scripted", "fixture": "..."},
  "policy": {"max_steps": 32},
  "required_facts": ["empty input must not crash"],
  "oracles": [
    {"kind": "test_profile", "profile": "python_unittest"},
    {"kind": "changed_paths", "allow": ["todo_parser.py"]}
  ]
}
```

Manifest 路径相对 eval suite root 解析并做 containment。每个 repetition 创建新的 session/workspace，不复用前一次修改。

### 4.2 Oracle 优先级

1. 可信 test profile；
2. 文件存在/hash/包含/不包含断言；
3. changed-path allow/deny 和 diff constraints；
4. exit/result schema；
5. LLM judge 仅用于无法程序化的辅助维度，并单独报告模型/提示词/方差。

Oracle 自己失败（fixture 不存在、profile 配置错）属于 `eval_infrastructure_failure`，不能算 Agent task failure，也不能从报告静默删除。

### 4.3 指标定义

| 指标 | 定义 |
|---|---|
| task success rate | 全部 oracle 通过的有效 run / 有效 run |
| runtime completion rate | `COMPLETED` run / 有效 run |
| tool/model calls | 从 committed events 汇总，报告 mean/p50/p95 |
| token usage | input/output/compression tokens 分开报告 |
| latency | run、model、tool、test、compression 分段 wall time |
| failure reasons | Runtime/tool/oracle/infrastructure 分类计数 |
| recovery rate | 遇到预定义可恢复 fault 且最终 oracle 通过 / 遇到该 fault 的有效 run |
| permission violations | denied attempt / run，并保留具体 tool kind |
| source invariant | source unchanged run / 有效 run，任何失败均为安全回归 |

`COMPLETED` 且 oracle 失败必须显示为“runtime completed / task failed”。超时、崩溃和预算耗尽留在分母。

这里“有效 run”只排除 manifest 无效、fixture 无法构建、oracle 自身配置错误等 eval infrastructure failure；Agent 的 Runtime failure、timeout、预算耗尽和 task oracle failure 都是有效 run，必须进入分母。

### 4.4 Runner 与输出

```text
load/version-validate suite
 → materialize fresh case workspace
 → run application use case
 → run trusted oracles
 → persist EvalRun
 → aggregate report
```

建议输出：

```text
<agent-home>/evals/<evaluation-id>/
├── manifest.snapshot.json
├── runs.jsonl                 # case/repetition/session/result references
├── report.json                # machine-readable exact metrics
└── report.md                  # human-readable summary and failures
```

底层 session/events 仍在 SQLite；报告只存引用和聚合，不能成为恢复 authority。

### 4.5 A/B 规则

- 一次只改变一个显式 variable（model、prompt、context policy、tool schema 等）；
- 使用同一 suite version、case order、repetition 和预算；
- ScriptedBackend 用于 Runtime 回归，真实 backend 至少多次重复并报告方差；
- paired case 结果保留，不能只比较总体平均；
- 报告新增成功、回归 case 和失败轨迹链接；
- 样本过小时写明描述性结果，不做夸大显著性结论。

### 4.6 M3.3 checklist

- [x] manifest version/containment/unknown-field validation；
- [x] fresh workspace per repetition；
- [x] test/file/diff oracle 正常与基础设施失败测试；
- [x] task success 与 runtime completion 分离；
- [x] tokens/latency/calls/failures/recovery 指标单测；
- [x] 失败 run 留在分母且 trace 可定位；
- [x] deterministic report 排除 timestamp/id 等随机比较字段；
- [x] A/B 同一变量与 paired diff 校验；
- [x] 至少一个包含 success、task-fail、runtime-fail、recovery 的固定 suite；
- [x] README 只引用实际生成的测量结果。

实现落点为 `evaluation.py`、`cli.py`、`examples/eval_suite.json` 和
`tests/test_m3_evaluation.py`。Eval report 的 comparison projection 不含随机
session ID/trace path；运行明细 `runs.jsonl` 仍保留定位所需引用。评测器不把
Runtime failure、timeout、预算耗尽或 oracle failure 从有效 run 分母删除。

## 5. M3 完成定义

1. 长轨迹在预算内构建 context，硬保留事实不会被静默删除。
2. Summary 可追溯到 event range/hash/revision，过期和错误事实可失效。
3. 压缩失败有界、可观察并能回退，不破坏主 session 历史。
4. EvalCase 与 Oracle 版本化，任务成功独立于 Runtime 终态。
5. 固定 suite 报告全部要求指标和 failure taxonomy。
6. 至少完成一次 passthrough/budget/compression 的配对 A/B。
7. M1/M1.5/M2 regression、golden 和 resume tests 全部通过。
8. `current-state.md`、roadmap、contracts、repository structure 和 HANDOFF 已更新。

## 6. M3 验收记录

- `PYTHONPATH=src python3 -m unittest discover -v`：61 个测试通过，包含四份既有
  semantic golden、M2 recovery/model contract 和 M3 context/eval tests；
- `.venv/bin/ruff check src tests examples/todo_cli`：通过；
- `PYTHONPATH=src python3 -m compileall -q src tests examples/todo_cli`：通过；
- `examples/eval_suite.json`：离线固定 suite 含 success、task-fail、runtime-fail、
  recovery 四类 case，可生成 `manifest.snapshot.json`、`runs.jsonl`、`report.json`
  和 `report.md`；
- 无 `OPENAI_API_KEY`/`ANTHROPIC_API_KEY`，因此不宣称 live Provider 或生产成功率。

## 7. 禁止项

- 不把 summary 当原始事实或 checkpoint；
- 不用摘要文本授予权限或证明测试通过；
- 不按固定消息条数无条件压缩；
- 不把字符估算写成精确 Provider Token；
- 不用 final answer 文本判断 task success；
- 不从评测分母删除 timeout/crash/budget failure；
- 不为展示指标临时挑选成功 case；
- 不在 M3 引入用户画像、RAG、多 Agent 或 UI。
