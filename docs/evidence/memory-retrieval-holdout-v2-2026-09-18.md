# L3.5 Holdout v2 first-run record

日期：2026-09-18

本记录只保存脱敏执行证据；Holdout 正文仍由用户在仓库外保管，共享仓库不保存正文或 Memory
内容。正文 SHA-256 为
`d1d9c45d9d06aea211780fa1d6b3d9baf4154891f1a3344ce1ae1cb658907d6f`。

## 冻结与执行身份

- algorithm commit：`f1d03cf`；执行前后 `src/coding_agent/memory/retrieval.py` 与该提交完全一致。
- runner commit：`e7287d2`。
- 主 suite：20 个 case、4 个 repository、4 个共享 pool；compatibility arm 不计入主集合。
- 执行时间：`2026-09-18T02:33:10.163659+00:00`。
- 有效/无效 case：`20/0`。
- 原始结果 SHA-256：`49e979db9b97f0000cae1f1fdae4d13e1d9d15f67ff584211c522813b2351122`。
- 脱敏 summary SHA-256：`371dfe07ba9ece26d94b01b0b55816f0db9833ecf3da8327b5a3c473ae15bad4`。
- 结果位置：用户保管目录 `l3.5-memory-retrieval-holdout-v2/first-run-v2-2026-09-18-final-retry/`，含
  `raw-result.json`、`redacted-summary.json` 和 `execution-metadata.json`。

首次三次基础设施失败均发生在产生 case 评价结果之前，未覆盖或删除；失败记录 SHA-256 依次为：

- `ad6139b10563e40fdb62afaabf8a2b3941a23e2d9eda2097898998c8a1c83b20`
- `a3aa5b0f3f283858b89c2c732643d372f0870e9712e5817e2798eadf0864cba4`
- `60091ed34377f23946bd5aac57152268b808c22086f3e306290536b0de931fb3`

## 实测结果

runner 是 observation-only executor：实际测量了 frozen fixture 上的检索、Context renderer 和行为
oracle，但没有 Provider、没有 Agent 修改、没有真实模型 usage。因此 `task_success=0/20` 与
`model_total_tokens=5518` 不能解释为真实模型成功率或 Provider Token；后者是 renderer 的
`named_estimator` 输入 Token 估计。

| 指标 | 实测值 | 门槛/说明 |
|---|---:|---|
| task success | 0/20 (`0.0`) | observation-only；不是 Provider 任务成功率 |
| relevant recall | `0.0` | 门槛 `>=0.85`，未通过 |
| precision | `1.0` | 无 Memory 被注入，因此为描述性值 |
| irrelevant injection | `0.0` | 门槛 `<=0.15`，通过 |
| 无关 Memory 行为变化 | `0` | 两个 no-memory case，on/off oracle 结果相同 |
| scope/revision/stale/deleted leakage | `0` | 通过 |
| prompt-injection policy bypass | `0` | 通过 |
| retrieval Token | `0` total | 20 个主 case 均未选中 record |
| model total Token | `5518` | renderer estimate；无 Provider usage |
| cold latency | `1.7627652014198247 ms` mean | observation-only Context build |
| warm latency | `1.456806949863676 ms` mean | observation-only Context build |
| retrieval latency | `0.24218380058300681 ms` mean | actual retriever measurement |
| Token per successful task | `null` | 没有成功 task，原样保留 |
| manifest Token mismatch | `0` | 与实际 renderer 一致 |
| compatibility warm | `3/3` | 独立保留，不计入主 Holdout |

结论：该首轮证明了固定 suite、scope/status 过滤、renderer Token 归因和兼容 arm 的可执行性，
但 lexical baseline 在非同构 paraphrase 上 relevant recall 为 `0.0`，且 observation-only runner
不能提供真实 task-success 或 Provider 净收益证据。不得据此把 Memory 接入默认 Application、
headless 或 IPC，也不得激活 P2-M3；仍需另行配置真实 Provider 和真实任务 A/B 后再决定默认入口。
