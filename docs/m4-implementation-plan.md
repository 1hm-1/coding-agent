# M4：OS 级 Workspace Isolation 实施计划

> 状态：M4.1、M4.2 已完成（2026-09-05）  
> 前置门禁：M3 完成，Eval Harness 能稳定运行安全回归 suite  
> 核心原则：先写 threat model 和攻击测试，再选择/实现 executor

## 1. 为什么 M4 独立成阶段

M1 的 `WorkspaceGuard` 控制 Agent 自带文件工具，`restricted_test` 控制模型不能提交任意命令；但被测试的仓库代码仍作为宿主进程运行，可以读宿主文件、访问网络或消耗资源。source fingerprint 也只是事后检测。

M4.1 将 restricted test 的执行边界升级为 Linux OS namespace 强制边界；M4.2 在这条
边界上增加结构化 argv 执行入口。项目不把 M4 阶段描述为完整容器平台或通用 Shell
sandbox。

## 2. Threat model

### 2.1 不可信输入

- 用户提供的 repository 内容和测试代码；
- 模型生成的 tool arguments、文件内容和未来命令参数；
- 仓库内 symlink、可执行文件、配置和依赖脚本；
- stdout/stderr 中试图污染日志或泄露数据的内容。

### 2.2 可信组件

- Runtime、Tool Harness、Persistence 和 Sandbox policy；
- 由部署方选择的 executor backend；
- 可信 test/command profile；
- 宿主内核与容器/隔离运行时在本项目范围内视为 trusted computing base。

### 2.3 必须保护

- 用户 source repository；
- agent state DB、其他 session workspace 和 traces；
- API keys、宿主环境变量和凭据文件；
- 宿主网络、进程、CPU、内存和磁盘；
- Event/Log 的完整性和可用性。

### 2.4 不在首版承诺

- 抵御内核/容器运行时 0-day；
- 跨主机多租户调度；
- Windows/macOS 完整等价；
- 在无任何 OS isolation primitive 的机器上安全执行恶意代码。

## 3. Sandbox 窄接口

```python
@dataclass(frozen=True)
class ResourceLimits:
    wall_seconds: float
    cpu_seconds: float
    memory_bytes: int
    writable_bytes: int
    pids: int
    stdout_bytes: int
    stderr_bytes: int

@dataclass(frozen=True)
class ExecutionSpec:
    argv: tuple[str, ...]
    workspace: Path
    working_directory: str
    environment: Mapping[str, str]
    network: Literal["none", "approved"]
    limits: ResourceLimits
    profile_name: str

@dataclass(frozen=True)
class ExecutionResult:
    status: Literal["exited", "timeout", "resource_exhausted", "sandbox_error"]
    exit_code: int | None
    stdout: str
    stderr: str
    duration_ms: float
    limit_hit: str | None
    backend_metadata: JsonObject

class SandboxExecutor(Protocol):
    def capabilities(self) -> SandboxCapabilities: ...
    def execute(self, spec: ExecutionSpec) -> ExecutionResult: ...
```

Tool handler 只构造经过策略校验的 `ExecutionSpec`，不能调用 Docker/Podman/subprocess 细节。Backend metadata 只保存运行时版本、image digest、limit 状态等非敏感字段。

## 4. Sandbox policy

首个支持的 Linux backend 必须做到：

- workspace 是唯一任务可写 mount；
- source repository、agent home、state DB、其他 workspace 不进入 mount namespace；
- root filesystem 只读，临时目录使用有大小限制的 tmpfs；
- 默认无网络；
- rootless/unprivileged user namespace identity，drop capabilities，`no-new-privileges`；
- 固定、可审计的 base identity（image backend 使用 digest）；
- 环境变量使用 allowlist，不继承宿主 secret；
- wall time、CPU、memory、pids、writable storage、stdout/stderr 有上限；
- timeout/interrupt 后回收整个容器/进程树；
- workdir 经 containment，argv 直接传递，不经过 shell parsing。

M4.1 当前使用 rootless Linux user/mount/PID/network namespace 和最小 chroot
rootfs；它不依赖 Docker/Podman，也不把宿主 `subprocess` 作为 fallback。backend 先做
capability probe；依赖不存在或缺少关键能力时 fail closed，不能静默回退到宿主执行。
当前 native rootfs 以运行时内容哈希作为可审计 identity，不冒充 OCI image digest。

## 5. M4.1：将 restricted test 迁入 sandbox

### 5.1 数据流

```text
model selects trusted profile
 → Tool Harness schema/permission
 → TestProfile resolves fixed argv/image/limits
 → SandboxPolicy validates ExecutionSpec
 → persist tool attempt intent
 → SandboxExecutor.execute
 → normalized ExecutionResult
 → ToolResult + events/journal
 → observation
```

TestProfile 扩展：

```python
@dataclass(frozen=True)
class TestProfile:
    name: str
    argv: tuple[str, ...]
    image: str
    working_directory: str = "."
    network: str = "none"
    limits: ResourceLimits = ...
```

模型仍只能提交 profile 名称。image、argv、network 和 limits 均来自可信配置。

### 5.2 Workspace 生命周期

- sandbox 开始前记录 workspace revision；
- 执行时只挂载该 session workspace；
- 完成后记录新 revision 和 changed path summary；
- source fingerprint 必须仍不变；
- timeout/崩溃后验证 sandbox 实例已销毁；
- orphan cleanup 根据 session/execution id，只清理本系统带标签的对象。

### 5.3 依赖策略

首版优先使用预构建、digest-pinned image。禁止测试阶段默认联网 `pip install`。项目需要额外依赖时采用经批准的 image/profile 版本，记录 image digest，以保证 eval 可复现。

### 5.4 M4.1 checklist

- [x] capability probe 和 fail-closed behavior；
- [x] workspace 可写，rootfs 只读，source/agent-home 不可见；
- [x] host secret/environment 不可见；
- [x] 默认禁网，DNS/IP 直连都失败；
- [x] wall/CPU/memory/pids/writable-storage limit tests；
- [x] stdout/stderr 截断不会导致宿主 OOM；
- [x] timeout/崩溃后清理完整进程树和 namespace；任意时刻 interrupt 仍遵循 M2 安全状态边界；
- [x] symlink/procfs/device 等 escape regression；
- [x] image identity 和 sandbox metadata 进入 trajectory；
- [x] source unchanged 与其他 session isolation；
- [x] 原 restricted_test contract 和 failure recovery golden 保持语义兼容。

验收证据：`src/coding_agent/sandbox/` 提供窄 `SandboxExecutor`、策略校验、Linux
namespace backend 和私有 runner；`tests/test_m4_sandbox.py` 的 7 个测试覆盖 capability
fail-closed、workspace/rootfs/secret/network/symlink/proc/device、五类资源限制、超大
输出、子进程清理、并行 session 和 SQLite recovery/trajectory。M4.1 后的默认路径是
`restricted_test → ToolHarness → SandboxPolicy → LinuxNamespaceExecutor`，没有宿主进程
回退。

当前边界：只对 capability probe 成功的 Linux 环境启用 native backend；其他平台或缺少
namespace/mount 能力时拒绝执行。M4.1 没有增加通用 Shell、approved network、OCI image
拉取或 syscall filter；这些不是本次验收承诺。

## 6. M4.2：安全执行能力扩展（已完成，2026-09-05）

M3 固定 eval 的已有 case 没有把“需要任意命令”作为独立变量；在用户明确要求继续
M4.2 后，采用最小结构化扩展，保持命令不经过 shell、仍由 Harness 和 M4.1 sandbox
承载。新增 `run_command`，而不是 `run_shell(command: str)`：

```json
{
  "profile": "python_project",
  "argv": ["python3", "-m", "compileall", "src"]
}
```

实现与验收：

- `CommandProfile`/`CommandProfileRegistry` 固定 executable allowlist、image、环境、
  cwd 默认值、network 和 `ResourceLimits`；默认 `python_project` 只允许 `python3`，
  使用 `none` network。
- schema 只接受 `profile`、`argv` 和受限相对 `cwd`；argv 最多 32 项，单项最多 4096
  字符，cwd 最多 256 字符；未知的 `command`/shell 字段被拒绝。
- executable 必须精确命中 profile allowlist；shell interpreter 永不允许。argv 直接
  传给同一 `ExecutionSpec`，不调用 shell parser。
- `run_command` 使用 `EXECUTE_COMMAND` permission 和 `NON_IDEMPOTENT` journal mode；
  非零 exit code 是结构化 observation，timeout/resource/sandbox failure 复用
  `ExecutionResult` 映射。
- 每次执行仍经过 ToolHarness、SandboxPolicy、capability probe、SQLite intent/running/
  result journal、observation 和 M4.1 cleanup/metadata。
- approved network 或超过默认 command limits 的 profile 在无显式 approval 时
  `PERMISSION_DENIED`/`command_approval_required`，不会执行；本阶段未引入 approval
  UI/默认网络，也未扩大默认 profile。

M4.2 checklist：

- [x] 结构化 `profile + argv (+ cwd)` schema，禁止 shell 字符串和未知字段；
- [x] profile executable allowlist、argv/参数/cwd 上限和 workspace containment；
- [x] `EXECUTE_COMMAND` permission、Harness/journal/sandbox 链路保持统一；
- [x] 非零退出、timeout、resource limit、sandbox rejection 的结构化结果；
- [x] network/elevated-limit profile 未获 approval 时 fail closed；
- [x] running command crash 后进入 `WAITING_APPROVAL`，不无确认重复执行；
- [x] native direct-argv 攻击回归、fake contract、成功/失败/恢复测试。

验收证据：`tests/test_m4_execution.py` 的 6 个测试覆盖 schema/allowlist/cwd、profile
控制、approval fail-closed、非零退出 observation、native direct argv 和 non-idempotent
crash recovery；M4.1 的 7 个 sandbox tests 继续通过。固定 eval suite 新增
`calculator-structured-command`，固定 eval suite 的 7 个 case 全部运行且 infrastructure failure 为 0；当前 suite 覆盖 calculator 与 todo-cli 两个 fixture，但仍是小型 scripted/offline 证据。

若需求只是搜索或 Git 检查，优先提供只读 `search_files`、`git_status`、`git_diff` 专用工具。是否增加由 eval failure coverage 决定，放入 M5 capability decision，不因“Coding Agent 应该有 Shell”自动加入。

## 7. 安全测试 suite

建立独立恶意 fixture；每个 case 在受控 CI runner 中执行：

| Case | 期望 |
|---|---|
| 读取 source/agent home/`~/.ssh` | 路径不可见或 permission denied |
| 读取注入的 fake API key | 环境不存在，trace 无泄露 |
| 写 workspace 外路径 | 失败且宿主 canary hash 不变 |
| 外部/internal symlink escape | 失败且目标不变 |
| DNS、HTTP、直接 IP 连接 | 默认失败 |
| fork/process bomb | 命中 pids limit，sandbox 被回收 |
| memory allocation bomb | 命中 memory limit，不影响 runner |
| CPU busy loop | 命中 CPU/wall limit |
| huge stdout/stderr | 有界截断，Runtime 继续 |
| child ignores TERM | 最终强制回收整个 execution |
| `/proc`/device 探测 | 不暴露宿主敏感信息 |
| 并行两个 session | mount、PID、输出和清理互不影响 |

攻击测试的“通过”必须来自 host canary、executor state 和 trajectory 三方断言，而不只看恶意程序返回码。

## 8. Observability 与恢复

每次 execution 记录：execution id、session/tool call id、profile、image digest、capability snapshot、limit、开始/结束、exit/limit kind、output truncation、workspace revisions。

不记录完整宿主路径、环境变量值或 secret。M2 tool journal 的 recovery mode 对 sandbox execution 仍适用；未知 RUNNING execution 在本实现中通过 namespace/process-group cleanup
完成后才允许 repeatable observation 重试，execution id 和每次 attempt 进入 journal/event；不能同时启动第二个同 id execution。

## 9. 部署和 CI 前置

- CI runner 明确安装并锁定 sandbox backend；
- capability test 不满足时安全 suite 失败，不标 skip 后仍声称 M4 完成；
- 普通 unit tests 可使用 fake executor，无需容器；
- integration/security job 独立执行并限制并发；
- base image 构建、SBOM、漏洞扫描和 digest promotion 有记录；
- 清理 job 只能删除带项目 namespace、session 和过期标签的对象。

## 10. M4 完成定义

1. restricted tests 默认经过 OS sandbox，宿主 fallback 被禁止。
2. threat model 中列出的关键资产均有攻击测试。
3. source、agent DB、secret、其他 session 和宿主网络受到强边界保护。
4. 资源耗尽和不服从终止的进程能够被回收。
5. Sandbox failure/limit/recovery 进入结构化 journal 和 replay。
6. 固定 eval suite 在 sandbox 中结果可重复，并记录 image digest。
7. M1—M3 全量 regression、golden、resume 和 eval tests 通过。
8. README 对安全能力的描述与实际 capability tests 一致。

## 11. 禁止项

- 不因容器命令不可用而回退宿主执行；
- 不把 Docker socket 挂入 sandbox；
- 不挂载 agent home、Provider credential 或其他 session；
- 不默认开放网络；
- 不使用浮动 image tag 作为评测环境身份；
- 不通过 `shell=True` 或 `sh -c` 执行模型字符串；
- 不把 source fingerprint 当 OS 写保护的替代；
- 不以“用了容器”代替攻击测试证据。
