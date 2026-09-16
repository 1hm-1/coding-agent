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
- 完成证据：134/134 默认 unittest；四份既有 semantic golden 不变；Ruff；33 个配置范围源码
  文件 mypy；compileall；78.3% statement coverage；wheel/sdist 与独立 wheel Memory import；
  calculator/todo scripted smoke 与 12-case Memory cold/warm benchmark。
- 12 个冻结 case 的 trusted task oracle 为 cold 8/12 → warm 12/12，relevant recall=1.0、
  precision=2/3、irrelevant injection=1/3，无关行为变化率=0；retrieval cost 为 116 Token，
  Memory context Token 为 803，scripted model total Token 为 2740 → 3543（+803）。Token per
  successful task 为 cold 342.5、warm 295.25（仅 model），计入 retrieval 后 warm 为 304.92；
  最近一次本地运行 wall latency mean 为 cold 44.53ms、warm 47.71ms，warm-cold mean +3.18ms。
  延迟受本机调度影响；该 baseline 不代表真实 Provider 或生产收益。
  脱敏摘要见 [`docs/evidence/memory-cold-warm-2026-09-16.summary.json`](./evidence/memory-cold-warm-2026-09-16.summary.json)。
  它证明 A/B 和指标链路，不代表真实 Provider 或生产收益。
- SQLite schema 已迁移到 v4。P2-M3 仍未激活。

冻结上述 baseline 后，已按 [`p2-m2-retrieval-optimization.md`](./p2-m2-retrieval-optimization.md)
完成同一 12-case 集合的 before/after：recall 1.0→1.0、precision 2/3→1.0、irrelevant
injection 1/3→0，Memory 额外模型 Token 803→130（-83.8%），隔离泄漏保持 0；当前默认测试
为 136/136。原始完成证据保留在上文，避免用优化后数字重写冻结基线。
