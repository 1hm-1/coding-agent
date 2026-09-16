# Runtime IPC Compatibility Policy

> 状态：P2-M1 producer contract 已验证；Agent Platform consumer suite 尚未执行。

## 1. Authority 与消费关系

`coding-agent/docs/protocol/runtime-ipc-v1.md` 和 `coding-agent/protocol/v1/*.schema.json` 是生产者
权威。Agent Platform 记录其支持的 protocol version、Runtime release 和各 Schema digest，但不复制修改
Schema。

只有两个生产/消费仓库时不建立第三个 protocol repository。若未来出现多个独立 Runtime producer 或
多个外部 consumer，再评估提取只含 Schema、golden 和 compatibility tooling 的独立 artifact。

## 2. 兼容性规则

### v1 内允许

- 新增 capability identifier；
- 新增 backend kind 或 sandbox mode identifier；
- 新增 event `type`，且事件仅是 informational；
- 在明确标记为开放的 `payload`、`metadata` 或 `extensions` 中新增字段；
- 增加新的 failure `code`，但保留既有 category/status 语义；
- 提高实现上限且不改变已有请求结果。

### v1 内禁止

- 删除或重命名必需字段；
- 改变字段类型、枚举值或既有语义；
- 把可选字段改为必需；
- 允许 sequence 缺口、多个 result 或 result 后继续输出；
- 改变 exit/result 冲突规则；
- 把 informational event 变为 Platform 正确性所必需的命令；
- 放宽 Secret、绝对路径或输出大小边界；
- 让同一 request 在相同 capability snapshot 下具有不兼容解释。

以上变化需要 Runtime IPC v2，并保留 v1 至少一个明确的迁移/停用窗口。

## 3. 软件版本与协议版本

Runtime 软件版本和协议版本独立：

```text
coding-agent 0.1.0  → Runtime IPC 未实现
coding-agent 0.2.0.dev0 → Runtime IPC v1 producer（开发树，尚非发布 tag）
coding-agent future → 仍可使用 v1，并通过 capabilities 表达 memory/skills/MCP/multi-agent
```

内部从单 Agent 升级为多 Agent不必升级 IPC，只要 request/envelope/result 的 v1 语义保持不变。

## 4. 当前兼容矩阵

| Runtime release | Protocol | Producer suite | Agent Platform consumer suite | 状态 |
|---|---:|---|---|---|
| `v0.1.0` | 无 | 不适用 | 不适用 | 已发布 Runtime；不得宣称 IPC v1 |
| `0.2.0.dev0`（P2-M1 开发树） | `1` | 111 tests + schema/golden/vector + build/smoke 通过 | 待 Platform 验证 | producer 完成，尚非发布 tag |
| Phase 2 product release | `1` 或后续版本 | 待实现 | 待验证 | 设计 |

不得预填“通过”。实现发布时记录：

```text
runtime git tag/commit
runtime_version
protocol_version
SHA-256 of every schema
producer CI run
consumer CI run
supported/required capabilities
known limitations
```

## 5. Consumer 策略

Agent Platform 必须显式配置：

```text
supported_runtime_protocol_versions
preferred_runtime_protocol_version
minimum_required_capabilities
pinned_runtime_release
pinned_schema_digests
```

协商过程：

```text
run protocol-info
  → parse and validate
  → choose highest mutually supported version
  → verify required capabilities/backend/sandbox
  → verify and snapshot runtime version/capabilities/schema digests
  → construct execution request
```

没有共同版本或缺少必需能力时，在启动任务前 fail closed。不得尝试猜测旧 CLI 输出。

## 6. Golden 与 Schema 发布

每个协议版本目录不可原地进行 breaking rewrite。Schema 和 semantic golden 随 Coding Agent release
发布；Agent Platform 在测试环境固定对应 release/digest。

Consumer 测试可以保存一份标注来源 commit 和 SHA-256 的不可变 test vector snapshot，但该 snapshot
只用于验证，不成为第二个规范来源。发现差异时以生产者 release 中的 Schema 为准，并通过显式升级修改
Platform。

## 7. Deprecation

停止支持某协议版本前必须：

1. 新版本 producer/consumer contract tests 已通过；
2. Platform 已不再创建旧版本 request；
3. 文档和 capability discovery 已标记旧版本 deprecated；
4. 保留旧版本失败时的稳定 `unsupported protocol` 结果；
5. release notes 记录迁移命令、截止版本和回滚方法。

## 8. 当前开发决策

`v0.1.0` 不发生行为变化。Coding Agent P2-M1 producer contract 已在 `0.2.0.dev0` 开发树通过，
但该开发版本不等同于 release tag；发布仍需记录 commit/CI 与 consumer 兼容结果。Agent Platform
consumer 仍不在本仓库实现。
