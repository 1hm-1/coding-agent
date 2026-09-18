# P2-M2 Layered Memory 实施计划

> 状态：**已完成**
> 激活日期：2026-09-16
> 前置基线（激活前）：P2-M1 已完成；111/111 默认测试通过；SQLite schema v3。P2-M2 完成后迁移至 schema v4。

## 1. 本阶段目标

在不改变单 Agent Runtime、ToolHarness 安全边界和 Runtime IPC v1 公共协议的前提下，加入可审计、
可隔离、可遗忘的 episodic/semantic 长期记忆，并让选中的记忆以有界、可追溯的方式进入 Context
Builder。Working memory 继续由现有 session/messages/context 提供；Procedural memory 留到 P2-M3 Skill。

## 2. 明确边界

- 本阶段只实现 `session`、`repository`、`user` scope 的 episodic/semantic memory。
- 不实现向量数据库、embedding、RAG 框架、Skill、MCP、多 Agent、UI 或新的公共 IPC 字段。
- 模型输出不能直接写入 active memory；所有长期记忆先进入 `proposed`，通过 schema、provenance、
  scope/permission、敏感内容、去重/冲突检查后，才可由显式 approval 激活。
- repository memory 必须绑定 repository identity 与 revision；user memory 不得由 session 自动提升。
- 不保存 Secret、API key、完整工具输出或宿主绝对路径。删除保留 tombstone 和审计事件。
- SQLite 是 memory record、生命周期和 retrieval audit 的唯一 authority。

## 3. 分片与验收清单

### A. Domain 与 schema

- [x] 定义版本化 `MemoryRecord`、scope/kind/status、provenance、supersedes 和 content hash。
- [x] SQLite migration v4 提供 records、lifecycle audit 和 retrieval audit，重复迁移幂等。
- [x] domain/SQLite round-trip、未知枚举/版本、迁移 rollback 和 future-schema 测试通过。

### B. 写入生命周期与策略

- [x] 实现 propose、approve/activate、reject、mark stale、delete tombstone。
- [x] provenance、scope ownership、repository revision、Secret/绝对路径、内容上限 fail closed。
- [x] 同 scope/kind/hash 去重；supersedes/conflict 关系显式且原子更新。
- [x] 每个状态变化具备成功、失败和 transaction rollback 测试。

### C. 有界检索与 Context

- [x] 先实现确定性 lexical + metadata retrieval，不引入 vector dependency。
- [x] top-k、Token budget、scope containment、revision/expiry/stale/deleted filtering 有测试。
- [x] Context Builder 只注入 validated active records；manifest 记录 memory id/schema version、
  score、token cost、retrieval id 和总成本。
- [x] 无 memory 配置时保持既有 context 顺序、语义 golden 和 Runtime 行为不变。
- [x] 默认 `AgentApplication` 与 `run-headless` 保持 Memory-disabled；Memory 只由调用方显式
  组装 `MemoryStore`/retriever/query factory 到 `BudgetedContextBuilder`。

### D. Eval 与质量门禁

- [x] 固定 12-case cold/warm paired baseline，报告 task success、relevant recall、precision、
  irrelevant injection、无关行为变化、retrieval/Memory-context Token、Token per successful task
  和 latency；不把 retrieval recall 当作 task success。
- [x] 跨 user、repository、session leakage、prompt injection、过期/删除/旧 revision 负例通过。
- [x] 记录 warm-run 收益或无收益及成本，证据不足时不宣称 memory 改善能力。

### E. 回归、文档与退出门禁

- [x] `PYTHONPATH=src python3 -m unittest discover -v` 全部通过，四份 semantic golden 不变。
- [x] Ruff、配置范围 mypy、compileall、coverage 与既有 calculator/todo smoke 通过。
- [x] 更新 `current-state.md`、roadmap、module/repository structure、HANDOFF 和版本/限制说明。
- [x] 只有 A—E 全部通过后才把 P2-M2 标记完成；P2-M3 在此之前保持未激活。

## 4. 实施顺序

1. migration v4 + memory domain/store；
2. policy/service 生命周期；
3. deterministic retrieval + Context manifest；
4. cold/warm eval 与攻击/泄漏负例；
5. 全量质量门禁和文档收口。

## 5. 当前证据

- 激活前基线：2026-09-16，111/111 默认 unittest 通过。
- 冻结的未优化完成基线：134/134 默认 unittest；四份既有 semantic golden 不变；Ruff；33 个配置范围
  源码文件 mypy；compileall；78.3% statement coverage；wheel/sdist 与独立 wheel Memory import；
  calculator/todo scripted smoke 与 12-case Memory cold/warm benchmark。
- 12 个冻结 case 的 trusted task oracle 为 cold 8/12 → warm 12/12，relevant recall=1.0、
  precision=2/3、irrelevant injection=1/3，无关行为变化率=0；retrieval cost 为 116 Token，
  Memory context Token 为 803，scripted model total Token 为 2740 → 3543（+803）。Token per
  successful task 为 cold 342.5、warm 295.25（仅 model），计入 retrieval 后 warm 为 304.92；
  该未优化 baseline 运行的 wall latency mean 为 cold 44.53ms、warm 47.71ms，warm-cold mean
  +3.18ms；这是历史冻结数据，不是本轮优化后的 latency。
  延迟受本机调度影响；该 baseline 不代表真实 Provider 或生产收益。
  脱敏摘要见 [`docs/evidence/memory-cold-warm-2026-09-16.summary.json`](./evidence/memory-cold-warm-2026-09-16.summary.json)。
  它证明 A/B 和指标链路，不代表真实 Provider 或生产收益。
- SQLite schema 已迁移到 v4。P2-M3 仍未激活。

冻结上述 baseline 后，已按 [`p2-m2-retrieval-optimization.md`](./p2-m2-retrieval-optimization.md)
完成同一 12-case 集合的 before/after：recall 1.0→1.0、precision 2/3→1.0、irrelevant
injection 1/3→0，Memory 额外模型 Token 803→130（-83.8%），隔离泄漏保持 0；当前默认测试
为 136/136。原始完成证据保留在上文，避免用优化后数字重写冻结基线。

本轮 P2-M2.1 evaluation evidence（2026-09-16）已重新执行全量门禁：136/136 unittest、Ruff、
33 个源码文件 mypy、compileall、78.5% statement coverage、wheel/sdist、独立 wheel import、
calculator/todo smoke 和冻结 Memory A/B 均通过。A/B 的 cold/warm wall latency 实测为：before
`45.834164333731074ms`→`47.033260334198225ms`（`+1.1990960004671507ms`），after
`45.834164333731074ms`→`45.95990916732262ms`（`+0.12574483359154698ms`）；检索 mean
`0.056614917411934584ms`→`0.06059541647118749ms`（`+0.003980499059252907ms`）。这些延迟值
受本机调度影响，但没有从证据中删除。完整脱敏摘要见
[`docs/evidence/memory-cold-warm-2026-09-16.summary.json`](./evidence/memory-cold-warm-2026-09-16.summary.json)。
本轮未运行真实 Provider，因此不宣称真实模型净收益；Memory 仍只提供显式 Python composition，
默认 Application/headless 与 Runtime IPC 入口保持关闭，P2-M3 仍未激活。

同日对 12-case development suite 的补采结果也已保存为用户保管的脱敏 summary，路径标签为
`p2-m2-memory-benchmark-2026-09-18/redacted-summary.json`，SHA-256 为
`991ff28b84f708651ccb3bb4e85549f017f74cb31feffe039fcbb7f328b84889`。after 实测 task success
为 cold `8/12`、warm `12/12`，relevant recall `1.0`，precision `1.0`，irrelevant injection
`0`，无关行为变化率 `0`；retrieval/context/model Token 分别为 `81/130/2740→2870`，warm
Token per successful task 为 `239.16666666666666`（计入 retrieval 为 `245.91666666666666`）。
保存运行的 wall latency mean 为 cold/warm `48.789092167377625ms`/`48.00096650069463ms`，
retrieval latency mean 为 `0.06890908480272628ms`。这是 deterministic scripted/renderer
观测，不是 Provider 或真实模型收益证据。

### L3 非同源冻结 Holdout

为审查同源开发集外的行为，新增 `examples/memory_retrieval_holdout.py` 与对应 contract test，
并把 case manifest 固定在 `8ebc800` 的实现基线。18 个 case 使用三个共享 Memory pool（6/7/5），
包含 6/4/2/2/1/1/1/1 的 semantic relevant、hard negative、shared-pool competition、scope
isolation、repository revision、stale/deleted、无相关 Memory、prompt-injection 负例；另有最初
`5298ba0` 三任务共享池兼容 arm。首轮 hash 为
`3d4bffb06a19ee219534d4649d93e983e7ecd14cebfd90b84ae64feb1f7e2ed1`，不再修改 case 或根据结果
重跑宣称首轮。

首轮实际结果：warm task success `13/18`，relevant recall `0.6666666666666666`，precision
`0.8333333333333334`，irrelevant injection `0.16666666666666666`；scope/revision/stale-deleted
泄漏为 `0`，无相关 Memory 行为变化为 `0`，Memory context Token 总计 `381`、retrieval Token
总计 `274`、scripted model Token cold/warm 为 `4274/4655`，manifest Token 与 renderer 一致，
兼容 arm warm `3/3`。因此 L3 的 recall/injection 门槛在该固定基线下未通过；这是一项阻断性证据，
不是失败后修改题目的理由。补采 latency 的重复运行 cold/warm mean 为
`41.255672772725426ms`/`42.08423031700982ms`，retrieval mean 为 `0.18361411154425392ms`。
脱敏摘要见 [`memory-retrieval-holdout-2026-09-16.summary.json`](./evidence/memory-retrieval-holdout-2026-09-16.summary.json)。

该 holdout 仍是 deterministic trusted oracle 与 synthetic usage，不能证明真实 Provider 或真实
任务净收益；因此默认 Application/headless/IPC Memory 入口保持关闭，真实 Provider A/B 仍是后续
独立门禁，P2-M3 仍未激活。

### L3.5 Holdout v2 首轮记录（2026-09-18）

S3.5 算法冻结后，按 suite SHA-256
`d1d9c45d9d06aea211780fa1d6b3d9baf4154891f1a3344ce1ae1cb658907d6f` 完成唯一一次有效执行。算法
提交为 `f1d03cf`，runner 提交为 `e7287d2`；主集合 20/20 case 有效，原始结果与脱敏 summary
保存在用户保管目录，hash 和路径标签记录在
[`memory-retrieval-holdout-v2.manifest.json`](./evidence/memory-retrieval-holdout-v2.manifest.json)。
三次 seed/加载基础设施失败均在产生 case 结果前发生，failure record 保留，未覆盖或删除。

首轮 runner 为 observation-only：relevant recall `0.0`、precision `1.0`、irrelevant injection
`0.0`、scope/revision/stale-deleted leakage `0`、无关行为变化 `0`、prompt-injection bypass `0`、
manifest Token mismatch `0`、compatibility arm warm `3/3`；task success `0/20`，renderer
estimator model Token `5518`，cold/warm latency mean 为
`1.7627652014198247ms`/`1.456806949863676ms`，Token per successful task 为 `null`。这些是
实际 observation/renderer 测量，不是 Provider usage 或真实模型收益；relevant recall 未达到
`0.85`，所以 Memory 继续只通过显式 Python composition 提供，默认 Application/headless/IPC 不接入，
P2-M3 不激活。完整脱敏说明见
[`memory-retrieval-holdout-v2-2026-09-18.md`](./evidence/memory-retrieval-holdout-v2-2026-09-18.md)。
