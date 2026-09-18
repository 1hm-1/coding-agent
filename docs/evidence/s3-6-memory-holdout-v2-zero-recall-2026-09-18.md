# S3.6 Holdout v2 零召回根因调查

日期：2026-09-18

## 1. 调查边界与结论

本次只做离线诊断，没有修改 `src/coding_agent/memory/retrieval.py`、runner、Holdout、manifest 或
任何 Runtime/Application 默认路径，也没有重跑 Holdout v2。证据身份如下：

- algorithm：`f1d03cf`
- runner：`e7287d2`
- suite SHA-256：`d1d9c45d9d06aea211780fa1d6b3d9baf4154891f1a3344ce1ae1cb658907d6f`
- raw SHA-256：`49e979db9b97f0000cae1f1fdae4d13e1d9d15f67ff584211c522813b2351122`
- redacted summary SHA-256：`371dfe07ba9ece26d94b01b0b55816f0db9833ecf3da8327b5a3c473ae15bad4`

三选一结论：**Lexical ceiling**。

12 个 relevant case 的目标 record 全部通过 scope、status、expiry 和 repository revision 过滤；runner
也确实以完整 task 作为 query，未发生 query 丢失或错配。2 个目标 record 与 query 没有任何
informative lexical overlap，另外 10 个虽有稀疏交集，但原始 lexical score 只有
`0.05931280`–`0.30628391`，全部低于绝对阈值 `0.60`。因此没有候选进入 relative floor、Token budget
或 top-k 阶段。继续增加 alias 只能覆盖已知措辞，不能解决无词面交集的通用 paraphrase。

统一失败分类计数：

| 分类 | case 数 |
|---|---:|
| `no_lexical_overlap` | 2 |
| `below_absolute_threshold` | 10 |
| 其余九类 | 0 |

## 2. 方法与阈值口径

离线分析按 `f1d03cf` 的 `_TERM`、`_normalize_term`、`_COMMON_TERMS`、query term weight 和 score
公式重新计算，不调用 `retrieve()`，因此不产生 retrieval ID 或 audit 写入。runner 的 query 规则是：
显式 `query` 存在时使用它，否则使用完整 `task`；本 suite 的 20 个 case 都没有显式 `query`，所以
全部按预期走 task fallback。scope 和 revision 由 runner 的 query-scope 解析器传入，目标 record
逐条与实际 store candidate 资格复核。

`raw score = 0.85 * query_coverage + 0.15 * record_precision`。绝对阈值比较发生在 confidence 相乘
之前；表中的 adjusted score 是以 fixture confidence `0.9` 计算的反事实候选分，仅用于说明，即使
越过绝对阈值后其 manifest score 会是多少。无 informative overlap 时实现直接跳过，故 raw/adjusted
记为 `—`。所有 case 的 top-k 均为 5，Token budget 均为 512。

## 3. Relevant case 检索漏斗

`eligible` 按 `scope/status/expiry/revision` 顺序列出；`pass` 表示四项均通过。relative-floor 和
token/top-k 的 `not reached` 不是拒绝，而是候选已在更早阶段被淘汰。

| case / target memory | eligible | lexical overlap（informative） | raw / adjusted | absolute threshold | relative floor | token / top-k | final | 分类 |
|---|---|---|---:|---|---|---|---|---|
| `cinder-01-empty-lines` / `cinder-empty-record-policy` | pass/pass/pass/pass | `a,is,the`（无） | — / — | pre-score reject | not reached | `33`, not reached | `[]` | `no_lexical_overlap` |
| `cinder-02-duplicate-order` / `cinder-duplicate-marker-order` | pass/pass/pass/pass | `a,arrival,observation`（`arrival,observation`） | `0.10724588` / `0.09652129` | reject `<0.60` | not reached | `37`, not reached | `[]` | `below_absolute_threshold` |
| `cinder-04-malformed-conflict` / `cinder-malformed-reject` | pass/pass/pass/pass | `and,contract,malformedrow,partial,public,the`（除 `and,the`） | `0.20919431` / `0.18827488` | reject `<0.60` | not reached | `40`, not reached | `[]` | `below_absolute_threshold` |
| `harbor-01-breaker-stop` / `harbor-breaker-stop` | pass/pass/pass/pass | `an,call,client,not,open,the`（`call,client,not,open`） | `0.20204904` / `0.18184414` | reject `<0.60` | not reached | `33`, not reached | `[]` | `below_absolute_threshold` |
| `harbor-02-headerless-fallback` / `harbor-headerless-fallback` | pass/pass/pass/pass | `request,the`（`request`） | `0.07395666` / `0.06656100` | reject `<0.60` | not reached | `25`, not reached | `[]` | `below_absolute_threshold` |
| `harbor-04-tenant-zone-scope` / `harbor-tenant-a-zone` | pass/pass/pass/pass | `a,audit,event,local,rendered,tenant,when`（`audit,event,local,rendered,tenant`） | `0.28023399` / `0.25221059` | reject `<0.60` | not reached | `30`, not reached | `[]` | `below_absolute_threshold` |
| `harbor-05-route-revision-conflict` / `harbor-route-rev-b` | pass/pass/pass/pass | `current,quarantine,the,traffic,unrecognized`（除 `the`） | `0.29418239` / `0.26476415` | reject `<0.60` | not reached | `26`, not reached | `[]` | `below_absolute_threshold` |
| `meadow-01-undated-order` / `meadow-undated-last` | pass/pass/pass/pass | `a`（无） | — / — | pre-score reject | not reached | `25`, not reached | `[]` | `no_lexical_overlap` |
| `meadow-05-private-archive-scope` / `meadow-private-archive-user-a` | pass/pass/pass/pass | `a,archive,private,s,writer`（除 `a`） | `0.30628391` / `0.27565552` | reject `<0.60` | not reached | `23`, not reached | `[]` | `below_absolute_threshold` |
| `kiln-01-display-precision` / `kiln-display-rounding` | pass/pass/pass/pass | `boundary,the`（`boundary`） | `0.05931280` / `0.05338152` | reject `<0.60` | not reached | `30`, not reached | `[]` | `below_absolute_threshold` |
| `kiln-02-negative-duration` / `kiln-negative-duration` | pass/pass/pass/pass | `at,the,zero`（`at,zero`） | `0.13797468` / `0.12417722` | reject `<0.60` | not reached | `31`, not reached | `[]` | `below_absolute_threshold` |
| `kiln-04-schema-revision` / `kiln-schema-rev-b` | pass/pass/pass/pass | `b,elapsed_m,revision,schema,the`（除 `the`） | `0.25842105` / `0.23257895` | reject `<0.60` | not reached | `24`, not reached | `[]` | `below_absolute_threshold` |

### 3.1 规范化 term 证据

以下是每个 relevant case 实际参与集合运算的去重、排序 term，不保存原始 prose：

- `cinder-01-empty-lines`
  - query: `a, add, archive, blank, focused, harden, ignored, input, is, keep, line, or, order, ordinary, physical, reader, record, regression, so, test, the, their, two-field, update, while`
  - record: `a, an, arrival, between, conveyor, empty, gap, importer, is, item, mint, not, should, specimen, the, two`
- `cinder-02-duplicate-order`
  - query: `a, are, arrival, as, behavior, carrying, catalog, change, entry, implementation, in, marker, observation, order, retained, same, shelf, so, test, the, two, verify, with`
  - record: `a, arrival, by, collapsing, from, instead, is, key, location, observation, of, one, preserve, second, separate, sequence, still`
- `cinder-04-malformed-conflict`
  - query: `an, and, column, contract, define, ensure, escape, existing, few, for, input, it, malformedrow, no, partial, path, preserve, public, record, reject, row, the, through, too, valid-row, with`
  - record: `a, and, as, before, contract, data, emit, failure, hard, intake, malformedrow, partial, public, returning, specimen, the, treat, truncated`
- `harbor-01-breaker-stop`
  - query: `adjust, again, an, attempt, call, check, circuit, client, contacted, count, during, fake, in, include, is, loop, not, open, repository, retry, so, test, that, the, to, upstream`
  - record: `afterward, an, breaker, call, client, current, delivery, end, grow, ledger, must, not, open, pass, s, the`
- `harbor-02-headerless-fallback`
  - query: `a, channel, console, cover, default, fallback, focused, header, make, no, path, request, routing, s, select, test, that, the, then, with`
  - record: `absent, catch-all, in, is, lane, metadata, place, request, the, when`
- `harbor-04-tenant-zone-scope`
  - query: `a, an, and, audit, b, belonging, borrow, code, configured, do, event, for, formatting, honor, local, not, offset, rendered, s, setting, tenant, the, through, to, verify, when, zone`
  - record: `a, are, audit, clock, europe, event, helsinki, in, is, its, local, rendered, requested, tenant, when`
- `harbor-05-route-revision-conflict`
  - query: `current, destination, fallback, for, must, not, older, quarantine, r2, revision, route-table, rule, s, send, the, to, traffic, unrecognized, win`
  - record: `belong, current, in, quarantine, route, table, the, traffic, unrecognized`
- `meadow-01-undated-order`
  - query: `a, after, and, both, by, card, date, dated, due, equal, for, keep, lacking, path, placed, reminder, sort, stable, test, the, their, with`
  - record: `a, behind, belong, calendar, carry, entry, slip, stamp, that, unscheduled`
- `meadow-05-private-archive-scope`
  - query: `a, archive, b, exercise, exposing, for, helper, in, location, private, return, root, s, scope-specific, test, the, without, writer`
  - record: `a, archive, is, private, root, s, under, writer, writer-a`
- `kiln-01-display-precision`
  - query: `a, add, and, at, available, boundary, caller, change, display, early, full, include, keep, meter, of, only, precision, reading, regression, round, test, that, the, to, value, when`
  - record: `boundary, decimal, formatter, human-facing, instrument, leave, raw, s, the, until, untouched`
- `kiln-02-negative-duration`
  - query: `accepted, and, at, below, boundary, close, for, gap, leave, measurement, non-negative, public, reject, sample, the, them, validation, zero`
  - record: `at, before, cannot, discard, impossible, ingress, observation, report, sensor, that, the, time, zero`
- `kiln-04-schema-revision`
  - query: `a, available, b, caller, elapsed_m, field, for, historical, is, its, keeping, lookup, name, old, public, revision, s, schema, so, the, to, update, while`
  - record: `as, b, elapsed, elapsed_m, expose, measurement, revision, s, schema, the`

`elapsed_ms` 被当前复数后缀规则规范化为 `elapsed_m`，所有格会留下 `s`。前者在 query 和 record
两侧一致，后者也没有决定任何 case 的通过/失败，所以本轮没有把 case 归类为
`normalization_gap`；它们是后续 tokenizer 设计需要保留的已知风险。

## 4. 六项重点核查

1. **不是 runner/query 组装错误。** 20 个 case 均无显式 query，runner 按设计使用完整 task；离线
   term 与实际 task fallback 一致。scope/revision 参数也正确进入 `MemoryQuery`。
2. **目标 Memory 未在 scoring 前被错误过滤。** 12/12 目标 record 的 scope、ACTIVE status、expiry
   和 revision 都 eligible；2 个在 informative-overlap guard 被跳过，10 个在 raw lexical score 的
   `0.60` 绝对阈值被拒绝。
3. **纯 lexical 已触及 paraphrase ceiling。** 两例完全没有 informative overlap，阈值、relative
   floor、top-k 或 Token 调整都无法召回；其余十例的完整 task 造成 query-coverage 稀释。BM25 可改善
   稀疏词面匹配，但仍不能单独覆盖零交集语义改写。
4. **relevant label 有内容支持。** 逐条比对 oracle 所需行为与目标 record：空行/重复顺序/畸形行、
   breaker/缺省路由/租户时区/路由 revision、无日期排序/私有 archive、显示精度/负时长/schema
   revision 均由对应 Memory 内容直接支持；没有 `relevance_label_error`。
5. **三次基础设施失败没有污染最终 store、audit 或随机 ID。** 三次失败分别是 repository revision
   validation、被接受的 policy-negative fixture、runner import path；其 failure record 都明确
   `evaluation_results_generated=false`。每次 invocation 使用独立输出目录和临时 SQLite store，最终
   有效运行重新 seed；UUID 只作为 audit identity，不参与 score 或排序。
6. **raw 与 summary 完全一致；manifest 在其声明粒度内一致。** raw 的 20 个 cold/warm selection
   都是空数组；summary 的每案 selected count/relevant hit 都为 0，aggregate recall/retrieval Token
   也分别为 0/0。manifest 的 raw/summary hash、20/20 valid、recall、precision、injection、leakage
   和 Token mismatch 均匹配。manifest 不保存逐案 selected ID 数组，因此不能声称存在逐 ID 的
   三方字段对等；raw 是该字段的权威证据，未发现 `metric_or_harness_error`。

## 5. 三次基础设施失败隔离证据

| 次序 | failure | record SHA-256 | 是否产生 evaluation |
|---|---|---|---|
| 1 | repository scope/revision validation | `ad6139b10563e40fdb62afaabf8a2b3941a23e2d9eda2097898998c8a1c83b20` | 否 |
| 2 | policy-negative fixture 被接受 | `a3aa5b0f3f283858b89c2c732643d372f0870e9712e5817e2798eadf0864cba4` | 否 |
| 3 | compatibility runner import path | `60091ed34377f23946bd5aac57152268b808c22086f3e306290536b0de931fb3` | 否 |

失败目录、最终目录和 hash 都不同；没有覆盖已有输出。最终有效结果只对应 runner `e7287d2`。

## 6. 后续候选与成本评测方案

停止继续堆 alias。Holdout v2 只保留为已消费的诊断证据，不用它直接调参；另建明确标记的
development set，且任何算法变化前先冻结全新 Holdout v3，v2 不重跑。候选按从低到高复杂度分三臂：

1. **结构化 metadata retrieval**：写入时保存领域无关的 artifact/action/constraint/schema/revision
   等 facet，先沿用现有 scope/status/revision hard filter，再按 facet 匹配。评估人工标注成本、自动
   提取 Token、metadata bytes 和错误 facet 的误召回。
2. **BM25 lexical arm**：用长度归一化和 IDF 代替当前完整-query coverage，验证能否改善十个稀疏
   overlap case；保持两例零 overlap 为明确 negative capability boundary。评估 index build/query
   latency、index bytes、retrieval/context Token 和是否可在不增加第三方 runtime 依赖下实现。
3. **embedding hybrid arm**：semantic candidate generation 后仍执行现有 metadata hard filters，最后
   用确定性 reranker、阈值和 Token budget。评估 embedding 生成/API 或本地模型成本、缓存命中、
   index bytes、隐私边界、P50/P95 latency，以及零交集 paraphrase 的增益。

新 development set 的 A/B 同时报告 recall、irrelevant injection、scope/revision/status leakage、retrieval Token、
实际 Context/model Token、任务成功率、Token per successful task、index build/query latency 和存储成本；
negative control、原始三任务兼容 arm 与 manifest actual-token attribution 保持必测。选定算法并冻结
后，只允许在新 v3 上做一次首轮验证。

## 7. 冻结状态

- retrieval algorithm changed: `false`
- runner changed: `false`
- Holdout v2 rerun: `false`
- Holdout manifest/hash changed: `false`
- default Memory path enabled: `false`
- P2-M3 activated: `false`
