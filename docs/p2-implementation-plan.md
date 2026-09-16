# Phase 2 Implementation Plan

> 状态：P2-M1 已完成；P2-M2 尚未激活。
> 激活日期：2026-09-15
> 当前开发版本：`0.2.0.dev0`；`v0.1.0` 仍是固定发布基线。
> 唯一活跃实现范围：P2-M1 Headless Runtime IPC。

## 1. 产品目标与实施顺序

目标是把现有单 Agent Runtime 逐步完善为本地优先、可恢复、可审计的完整 Agent 产品。核心能力
按以下顺序交付：

```text
P2-M1 Runtime IPC 稳定边界
  → P2-M2 分层 Memory
  → P2-M3 Agent Profiles 与 Skill Runtime
  → P2-M4 MCP Capability Gateway
  → P2-M5 可恢复多 Agent
  → P2-M6 Terminal 产品与发布证据
```

Memory 是首个产品能力里程碑。P2-M1 先固定 Runtime 的公共进程边界，使后续 Memory、Skill、
MCP 和多 Agent 的内部演进都能由同一 producer contract 回归保护。不得跳过 P2-M1 直接让产品层
读取私有 SQLite、trajectory 或 Runtime Python 类型。

## 2. P2-M1 范围

只实现 [`protocol/runtime-ipc-v1.md`](./protocol/runtime-ipc-v1.md) 已定义的单 Agent producer：

- `protocol-info` capability discovery；
- `run-headless` 私有 request file 输入；
- request、event、terminal result 的严格校验和大小限制；
- 内部事件到脱敏公共事件的显式投影；
- stdout JSONL 的连续 sequence、唯一 terminal result 和稳定退出码；
- SIGINT cooperative cancellation、checkpoint-before-result 和子进程清理；
- producer semantic golden 与供 consumer 复用的不可变 contract vectors。

本阶段不实现 Memory、Skill、MCP、多 Agent、TUI、通用 Shell、默认网络或 Platform consumer。

## 3. 不变量

1. `AgentRuntime` 继续是不知道 IPC 的单任务内核；IPC 只通过 application use case 组合它。
2. Runtime 的副作用仍全部经过 ToolHarness，source repository 仍不作为写入目标。
3. SQLite 仍是恢复 authority；stdout event 只是脱敏投影。
4. Provider-specific request 格式只留在 adapter。
5. `v0.1.0` 不得被描述为实现 IPC；开发实现使用 `0.2.0.dev0`。
6. capability discovery 只公布已经可调用并通过测试的能力。

## 4. P2-M1 验收矩阵

### Protocol discovery

- [x] 变更前 93 个默认测试与四份 semantic golden 通过。
- [x] `protocol-info --protocol-version 1 --output json` 只输出一条 JSON record。
- [x] 输出包含四份 producer-authority Schema 的实时 SHA-256。
- [x] 不支持的协议版本不写 stdout，并以退出码 65 失败。
- [x] wheel/sdist 包含权威 Schema，独立 wheel 安装后的 discovery smoke 通过。
- [x] capability document 通过权威 Schema 的自动验证。

### Headless success/failure

- [x] request file 大小、JSON、Schema、backend、权限与目录 containment 验证完成。
- [x] scripted success/Runtime failure 分别产生连续 event stream 和唯一 result。
- [x] `COMPLETED` 映射 `succeeded`，但文档不把它等同于外部 task oracle success。
- [x] invalid request=64、unsupported protocol=65、未处理异常=70、private I/O=74。
- [x] stdout writer/serialization/oversize failure 有确定性测试。

### Projection/redaction

- [x] 公共 projector 使用事件 allowlist/minimal payload，不透传内部 event dict。
- [x] Secret、绝对 source/workspace/agent-home 路径和完整模型/工具输出不会进入 stdout。
- [x] projection success、unknown event、redaction 和 record oversize 测试通过。

### Cancellation/recovery

- [x] start 前、model/tool 安全边界中、重复 SIGINT 均有测试。
- [x] `INTERRUPTED` checkpoint 在 `cancelled` result 之前提交。
- [x] 活动子进程被清理，terminal result 最多写一次。
- [x] crash-after-events-before-result 可由 consumer contract 稳定识别。

### 退出门禁

- [x] producer semantic golden 和 v0.1-kernel/v0.2-bridge contract vectors 通过。
- [x] 全量 unittest、Ruff、mypy、coverage、compile、build 和既有 smoke 通过。
- [x] `current-state.md`、roadmap、structure、HANDOFF、README 与 compatibility matrix 同步。
- [x] 只有以上项目全部完成后，P2-M1 才能标记“已完成”；P2-M2 Memory 作为下一候选里程碑，仍需用户明确激活。

## 5. 完成证据

`src/coding_agent/protocol/` 已实现 discovery、严格 request validator、headless composition、公共
event projector、terminal result、连续 JSONL writer、稳定退出码和 cooperative cancellation。
SQLite committed event 仍是事实来源；public stream 只在提交后观察并做 allowlist/minimal-payload
投影，Runtime 内核没有引入 IPC wire 类型。

2026-09-16 验收：111/111 默认 unittest、四份既有 semantic golden、三份 Runtime IPC producer
golden、v0.1-kernel/v0.2-bridge vectors、Ruff、26 个配置范围源码文件 mypy、compileall、76.8%
statement coverage、wheel/sdist build、独立 wheel 安装后的 discovery smoke，以及 calculator/todo
既有 smoke 均通过。取消覆盖 start 前、model/tool 安全边界、重复 SIGINT、deadline、checkpoint
顺序和活动 sandbox 子进程清理；crash-after-events-before-result、stdout I/O/oversize/重复 result
也有确定性负例。

P2-M1 到此冻结。Memory、Skill、MCP、多 Agent、TUI、RAG 和 Platform consumer 均未在本里程碑实现。
