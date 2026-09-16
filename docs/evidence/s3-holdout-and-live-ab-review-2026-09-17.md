# S3 Holdout 审查与 Live Provider A/B Harness

日期：2026-09-17
审查对象：L3 提交 `d7801e2`，实现基线 `8ebc800`

## Holdout 审查结论

L3 首轮结果保持冻结，不修改 case、retrieval scoring、alias、threshold 或 Context renderer。
其 recall `10/15`（`0.6666666666666666`）和 irrelevant injection `2/12`
（`0.16666666666666666`）均未达门槛。

| 检查项 | 结论 | 证据与限制 |
| --- | --- | --- |
| 非同源文本 | 部分满足，不足以证明强独立 | 未复制三个原始 task，且代码阻止 `invoice`、`deploy color`、`alias` 三个片段；但多数 task/memory 仍是同一关键词集合的近同构改写，禁词检查不能排除开发集句式同构。 |
| memory pool | 通过 | 18 case 使用三个共享池，规模 6/7/5；不是一例一池。原始三例仅在独立 compatibility arm。 |
| oracle 泄露 | prompt 未直接泄露；oracle 仍是 synthetic | expected answer 不进入 Runtime task/context。`HoldoutBackend` 自身持有 expected fact/answer，并按 Memory 是否出现决定 final answer，因此只能证明 retrieval→synthetic task 的连通性，不能证明 Provider 能完成任务。 |
| 是否只测检索 | 不是纯 retrieval test，但任务 oracle 较弱 | 每个 arm 都运行完整 Runtime，并用 final answer 判定 task success；不过 backend 是确定性 simulator，任务结果与检索命中近乎同义。 |

失败只分类，不据此调参：五个 relevant miss 为 `archive-interval`、`orbital-uplink`、
`lighthouse-account`、`observatory-window`、`basalt-catalog`；两条 irrelevant selection 来自
`garden-pump` 与 `quarry-alarm` 的近同词 hard negative。前者属于 lexical coverage/词形与门槛组合，
后者属于共享主题词无法区分 answer-bearing fact 与文档性描述。该分类不是参数建议。

后续检索工作的 development set 明确指定为 L1/L2 已冻结的 12-case
`examples/memory_cold_warm_benchmark.py`（源提交 `80dba4c`），而不是 L3 的 18 case。它已经是已知、
可调参的数据，不能再被称为 holdout；其同源与小样本限制继续保留。本次没有改算法或参数。若未来
基于该 development set 修改检索算法，当前 L3 不得重新充当验收 holdout，必须在查看结果前冻结
文本、pool 与 oracle 均独立的 Holdout v2。

## Paired live harness

新增 `evaluate-memory-live`，只接受 OpenAI-compatible 或 Anthropic adapter。每个 case/repetition：

1. 用一次已完成的先前 Runtime 结果生成 Memory；Memory 内容必须与该 Runtime final result 完全一致，
   provenance 必须指向 journal 中已提交的 `run_finished` event，再显式 activate。
2. cold/warm 使用同一 Provider 工厂、model、task、`RunPolicy`、fixture fingerprint 和 trusted oracle；
   warm 唯一增加显式 `BudgetedContextBuilder` Memory composition。
3. pair 内按 case/repetition 奇偶交替 off/on 顺序，避免固定先后顺序偏差。
4. 使用既有 file、trusted test profile、changed-path 或 result-schema oracle；报告同时给出 oracle、
   Runtime completion 和二者合取的 end-to-end success。

报告包含 input/output/total Token、Token per successful task、retrieval Token、wall latency P50/P95、
tool attempts/executions、repeated failure batches、invalid calls、首次相关 action 前时间/工具次数、
source invariant、permission violation 与 scope leakage。warm run 保留完整 Context Memory manifest：
memory/retrieval ID、schema/record version、score、provenance、retrieval cost 与实际 Context Token 成本。

报告不保存 final answer、provider raw response、reasoning content、trace/workspace/source 绝对路径、base URL、
API key 环境变量名或 Secret；写盘前再次扫描绝对路径与 Secret 值。默认 `AgentApplication`、headless 和
Runtime IPC 没有接入 Memory，harness 仍是显式 eval composition。

示例（会真实消耗 Provider 配额）：

```bash
PYTHONPATH=src python3 -m coding_agent.cli \
  --agent-home /tmp/coding-agent-memory-live \
  evaluate-memory-live \
  --suite examples/memory_live_ab_suite.json \
  --provider openai-compatible \
  --model '<provider-model>' \
  --output /tmp/coding-agent-memory-live-report \
  --repetitions 3
```

本提交只校准 harness 的离线 test double，不冒充真实 Provider 结果。必须在冻结真实任务集并配置凭据后
单独运行上述命令，才能形成 Provider 净收益证据。

## S3 判定

当前为 **有条件通过**：允许使用 paired harness 补真实 Provider A/B；Memory 继续显式 opt-in，
P2-M3 不因 S3 自动激活。L3 不是足够强的独立质量通过证据，也不得以本次 harness 实现掩盖其失败。
