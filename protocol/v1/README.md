# Runtime IPC v1 Schema Bundle

> 状态：P2-M1 producer 已完成；Coding Agent `v0.1.0` 不实现本协议，`0.2.0.dev0` 开发树实现 discovery 与 headless execution。

本目录是 Runtime IPC v1 的机器可读 producer authority：

| 文件 | 用途 |
|---|---|
| `capabilities.schema.json` | `protocol-info` 的 release、protocol、backend、sandbox、capability、limits 和 Schema digests |
| `execution-request.schema.json` | Platform Adapter 写入私有 request file 的输入 |
| `event-envelope.schema.json` | Runtime stdout 的非终态 JSONL record |
| `terminal-result.schema.json` | Runtime stdout 最后且唯一的终态 record |
| `execution-request.vector.json` | producer/consumer 可复用的合法 request contract vector |
| `v0.1-kernel.vector.json` | 固定单 Agent kernel terminal semantics |
| `v0.2-bridge.vector.json` | Runtime IPC bridge terminal semantics |

完整语义、进程退出和取消规则见
[`../../docs/protocol/runtime-ipc-v1.md`](../../docs/protocol/runtime-ipc-v1.md)，兼容规则见
[`../../docs/protocol/compatibility.md`](../../docs/protocol/compatibility.md)。只读取 Schema 不能替代这两份
语义规范。

## 修改规则

1. 所有文件必须保持 JSON Schema Draft 2020-12 合法；
2. 修改前先按 compatibility policy 判断 additive 或 breaking；
3. 同步文档示例、producer golden/negative tests 和 Platform consumer vectors；
4. 计算并记录每个文件的 SHA-256，不以目录 mtime 或 Git branch 名代替；
5. producer release 后禁止原地进行 breaking rewrite，必须新增 protocol version 目录；
6. `additionalProperties: false` 的闭合对象只能通过已有 `payload`、`metadata`、`extensions` 或新协议扩展；
7. Schema 允许的输入仍须经过 containment、Secret、权限和部署 policy 校验。

计算当前文件摘要：

```bash
sha256sum protocol/v1/*.schema.json
```

P2-M1 自动化检查已覆盖 Schema bundle validation、文档正例、关键负例、semantic golden、
contract vectors，以及 `protocol-info.schema_digests` 与发布文件内容的一致性。Platform consumer
仍须固定具体 producer release/digest 并运行自己的 contract suite。
