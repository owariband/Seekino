# 本地 Sandbox 与 DeepAgents 接入研究

研究日期：2026-10-26。目标：为 Seekino local mode 找到已有的本地隔离执行能力，验证能否保留 Kimi / DeepAgents，只替换命令执行后端。

**结论：本地隔离 sandbox 有现成实现，不要求 LangSmith 云账号，也不要求把模型循环换成 Codex。Codex 的独立 sandbox 命令、Anthropic Sandbox Runtime 都能隔离本地进程。** 之前围绕 LangSmith 托管服务展开的讨论没有覆盖这个方向；不能把某个托管产品的部署条件理解成本地 sandbox 的必要条件。

本文件是资料与接入边界核对，不代表已完成 Seekino 创作后端接线。实际运行结果应与下述官方能力分开记录。

## 优先阅读的官方资料

| 资料 | 对当前问题的直接答案 |
| --- | --- |
| [Anthropic：Beyond permission prompts: making Claude Code more secure and autonomous](https://www.anthropic.com/research/claude-code-sandboxing)，2025-10-20 | 官方工程博客，解释 macOS Seatbelt、Linux bubblewrap、文件和网络隔离；明确可用于任意进程、Agent 或 MCP server，不必启动容器。 |
| [Anthropic：How we contain Claude across products](https://www.anthropic.com/research/how-we-contain-claude)，2026-05-25 | 对照服务端容器、Claude Code 的本地 OS sandbox、Cowork 的本地 VM；还解释了把 Agent loop 移到 VM 外、代码执行留在 VM 内的演进。 |
| [OpenAI：Agent approvals & security](https://learn.chatgpt.com/docs/agent-approvals-security) | 说明 Codex 的平台隔离机制和单独运行 sandbox 命令的方法。CLI 语法应以实际安装版本为准。 |
| [Anthropic Sandbox Runtime 源码与 README](https://github.com/anthropics/sandbox-runtime) | 可直接复用的独立 CLI `srt` 和 JavaScript 库，包含安装、配置、平台依赖和限制。 |
| [DeepAgents：Sandboxes](https://docs.langchain.com/oss/python/deepagents/sandboxes) | 说明 `BaseSandbox` / `execute` 如何接入统一文件工具，也列出现成的 NVIDIA OpenShell backend。 |

## 1. Codex 本地隔离能与模型循环分开

OpenAI 文档说明：macOS 使用 Seatbelt / `sandbox-exec`；Linux 当前使用 bubblewrap 与 seccomp；Windows 有原生实现，WSL2 使用 Linux 实现。这些机制约束实际启动的进程及其子进程，不只是提示词中的行为约定。[官方说明](https://learn.chatgpt.com/docs/agent-approvals-security)

本机源码核对提供了更直接的接入证据（`../codex` checkout：`5eea8d0dd`）：

- `codex-cli 0.163.0` 的命令形态为 `codex sandbox [OPTIONS] -- COMMAND...`，按宿主系统选择实现；网页仍可能出现带 `macos` / `linux` 子命令的旧示例。
- CLI 的 sandbox 入口加载权限配置后直接启动指定命令，可以与 Codex 的模型调用和 Agent loop 分开使用。[入口](../../../codex/codex-rs/cli/src/debug_sandbox.rs)
- macOS 后端调用 `/usr/bin/sandbox-exec` 并生成 Seatbelt 策略。[实现](../../../codex/codex-rs/sandboxing/src/seatbelt.rs)
- 当前 Linux README 写明默认使用 bubblewrap 与 seccomp，Landlock 是显式选择的旧路径。[说明](../../../codex/codex-rs/linux-sandbox/README.md)

这些是当前本地 checkout 与已安装 CLI 的核对结果，不能据此保证所有历史 Codex 版本都有相同命令参数。它们也不表示整个宿主文件系统默认不可读：读、写、网络边界仍取决于实际配置。[权限配置文档](https://learn.chatgpt.com/docs/permissions)

## 2. 不依赖 Codex CLI 的独立本地运行时

Anthropic 已把本地隔离实现发布为独立的 `@anthropic-ai/sandbox-runtime`。`srt` 可以包装普通命令；库接口提供 `SandboxManager.initialize()`、`wrapWithSandbox()` 与清理能力。安装完成后，包装本地进程本身不涉及模型请求；将它作为 Python 子进程调用是可行的接入方式，而非要求应用改用 Claude Agent SDK。[运行时 README](https://github.com/anthropics/sandbox-runtime)

| 宿主 | 当前运行时路线 | 部署代价 |
| --- | --- | --- |
| macOS | Seatbelt / `sandbox-exec` | npm 包与 Node 环境；README 的依赖节还列出 `ripgrep` |
| Linux | bubblewrap、网络 namespace、代理 | `bubblewrap`、`socat`、`ripgrep`；须满足宿主的 user namespace 策略 |
| Windows | 专用本地用户、访问控制与 WFP 网络规则 | 当前标记 alpha；需一次管理员安装步骤 |

上表来自当前 README，不能把 2025 年的博客当成今天的平台支持矩阵。默认读取范围较宽，写入和网络也有独立策略；接入时应明确输入素材的可读范围及产物目录，而不是仅设置 `cwd`。[平台与权限说明](https://github.com/anthropics/sandbox-runtime)

这是宿主 OS 上的进程隔离，不会附赠独立 Linux 用户空间、Python/FFmpeg 安装或每任务 VM。具体隔离强度与 VM 不相同；是否需要 VM，应由任务隔离要求决定。[工程博客](https://www.anthropic.com/research/claude-code-sandboxing)

## 3. DeepAgents 生态中也有本地执行环境的现成接法

当前 DeepAgents 官方文档已列出 `langchain-nvidia-openshell` 的 `OpenShellSandbox`。NVIDIA OpenShell 官方安装资料支持本地 gateway，并列出 Docker、Podman、MicroVM 计算后端。因而“DeepAgents 的 Sandbox 生态只有远程 SaaS”不成立。[DeepAgents 集成](https://docs.langchain.com/oss/python/deepagents/sandboxes)、[NVIDIA 安装资料](https://github.com/NVIDIA/OpenShell/blob/main/docs/about/installation.mdx)

它更接近可管理的独立执行环境：需要安装 gateway 与所选 runtime。当前主仓列出的宿主包括 Linux、Apple Silicon macOS，以及 experimental WSL2；MicroVM 在 macOS 依赖 Hypervisor.framework，在 Linux 依赖 KVM。Python SDK 本身不会安装完整 CLI / gateway。[主仓说明](https://github.com/NVIDIA/OpenShell)、[安装说明](https://github.com/NVIDIA/OpenShell/blob/main/docs/about/installation.mdx)

这是需要统一 Linux 工具链或独立运行环境时的候选，不是本轮最快的前提。官方 `latest` 文档与主仓会演进，应固定版本后验证。旧的 [langchain-ai/openshell-deepagent 示例](https://github.com/langchain-ai/openshell-deepagent) 已在 2026-10-16 归档，不应直接照搬其中旧的部署命令。本轮没有安装或运行 OpenShell。

## 4. 我们需要接什么，能够复用什么

建议保持已有 Kimi / DeepAgents，采用工具访问 sandbox 的模式：

```text
Seekino 宿主：模型、检索、会话状态
    → DeepAgents 文件与 execute 工具
    → BaseSandbox 适配
    → 本地 Codex sandbox / srt
    → Python、FFmpeg、制作代码
    → 宿主取回并发布作品
```

这条结构是工程建议。DeepAgents 官方支持把 sandbox 作为工具后端；Agent 不必运行在 sandbox 内。[接入模式](https://docs.langchain.com/oss/python/deepagents/sandboxes#integration-patterns)

本机 `deepagents 0.7.23` 的 `BaseSandbox` 需要实现 `execute()`、`upload_files()`、`download_files()` 和 `id`；其余多项文件工具已有实现。网页有“仅需 execute”的简化描述，工程接线应以锁定版本的实际抽象方法为准。[本地基类](../../.venv/lib/python3.12/site-packages/deepagents/backends/sandbox.py)

薄适配仍需承担：工作区和权限策略、参数传递、stdout / stderr / 退出码、超时与取消、文件传入传出及清理。不能把“几个接口”理解成已经完成生产隔离。尤其文件传输由宿主实现时，也必须限制在当前工作区，避免绕过命令沙箱。

现有 Seekino 把后端设为 `StateBackend`，文件工具仅开放 `read_file`。因此把某个 runtime 安装到机器上还不会自动让现有 Supervisor 获得创作执行能力；需要明确接入 backend 并开放对应工具。[当前装配](../../gateway/agent_model/deepagents.py)

## 5. 最短验证路径与边界

本轮已复用本机 Codex 独立 sandbox 命令完成确定性探针，结果见下一节。这个验证没有接模型、LangSmith 或部署 VM。

下一步是把同一个入口接到 DeepAgents，让 Kimi 创建并检查产物。验收同时记录：产物可预览、实际执行次数、耗时、失败反馈能否驱动一次修复，以及退出后没有遗留制作进程。已完成的静态探针仅证明所测的本地执行与隔离行为，不能当作 Agent 创作 workflow 已跑通。

## 6. 本机确定性实测

运行环境为 macOS、本机 `codex-cli 0.163.0`。探针只运行 Python 和 FFmpeg，没有模型调用或 LangSmith 服务请求。证据：[探针源码](../../outputs/verification/local-sandbox-20261026/probe.py)、[完整命令参数](../../outputs/verification/local-sandbox-20261026/command.json)、[结构化结果](../../outputs/verification/local-sandbox-20261026/result.json)。

命令使用显式命名权限 profile `seekino-local-proof`，保留 `--include-managed-config`，以 `:minimal` 为只读基础，增加 Homebrew 工具目录、Python runtime、探针目录和源视频的读取权限，显式业务写权限只给 `work/`，`denied/` 合成测试目录设为不可访问；网络关闭。Codex 的 macOS 进程策略还包含系统临时目录等基础许可，因此这里不声称所有工作区外路径均不可写。具体参数保存在 `command.json`，不能把这套路径配置直接当成生产通用策略。

| 检查 | 实测结果 |
| --- | --- |
| 工作区内写文件 | 允许，写入后内容核对一致 |
| 工作区外合成文件写入 | 拒绝，`EPERM`；宿主确认原文件未改变 |
| 显式禁止目录内的合成文件读取 | 拒绝，`EPERM` |
| 连接宿主临时 loopback listener | 宿主对照连接成功；受限子进程连接被拒绝，`EPERM` |
| 在沙箱内运行 FFmpeg | 成功，退出码 0，产物存在且非空 |

FFmpeg 从已有的 60 秒 EP04 素材剪出了 [preview.mp4](../../outputs/verification/local-sandbox-20261026/work/preview.mp4)。`ffprobe` 确认产物为 H.264、640×360、2.961292 秒；另用 `ffmpeg -i preview.mp4 -f null -` 完整解码成功。探针记录此次调用约 0.235 秒；这是一个微型样本的单次测量，不是生产 SLA 或完整视频制作性能基准。

执行过程中有两项环境修正：

- 首次在当前工具的外层 Seatbelt 中再启动 sandbox，遇到 `sandbox_apply` 的 `EPERM`；随后通过正常 `require_escalated` 执行入口，让宿主启动本次显式受限的子沙箱，成功运行。
- 第一次实际权限配置未放行 uv Python 标准库所在的 runtime 目录，Python 启动失败；补充对应 runtime 目录的只读权限后通过。没有为此把整个用户目录或文件系统改成可读写。

这次实测证明：本机已有组件能独立运行受限进程，并完成一个视频处理动作。尚未接入 DeepAgents / Kimi，未验证外网域名过滤、并发、取消，也没有完成完整安全审计；loopback 拒绝不应扩大解释为所有网络路径均已测过。

本轮新增这份研究笔记及 `outputs/verification/local-sandbox-20261026/` 下的探针与产物。未修改生产代码、依赖或原有 LangSmith 研究，未启动云资源、启用外部 tracing、提交或重启服务。

## 7. 实际改动与验证范围

```text
Seekino/
├── docs/
│   └── research/
│       └── [A] 2026-10-26-local-sandbox-deepagents.md  ← 官方资料、源码核对与实测记录
└── outputs/
    └── verification/
        └── local-sandbox-20261026/
            ├── [A] probe.py             ← 105 行确定性探针，无模型调用
            ├── [A] command.json         ← 本次实际执行参数
            ├── [A] result.json          ← 权限检查及视频元信息
            ├── [A] outside.txt          ← 工作区外合成写入对照
            ├── denied/
            │   └── [A] fixture.txt      ← 合成读取拒绝对照
            └── work/
                ├── [A] allowed.txt      ← 可写范围对照
                └── [A] preview.mp4      ← 本地沙箱内 FFmpeg 产物
```

新增文档 1 个、验证脚本 1 个、验证数据/产物 6 个，生产文件和正式测试文件均为 0；没有修改或删除既有文件。研究笔记为 untracked，`outputs/` 中的验证产物被 Git 忽略，因此普通 `git diff --stat` 不包含本次新增内容；其余工作区 diff 属于原有或并行任务。没有 API、数据库、产品配置、前端交互或兼容性变更。

验证命令为 `.venv/bin/python outputs/verification/local-sandbox-20261026/probe.py`（在可启动 Seatbelt 的宿主进程运行），5 项检查通过，另有 `ffprobe` 元信息检查与 FFmpeg 全量解码通过。没有运行无关全量测试。后续 DeepAgents 接线、跨平台行为、超时取消与作品发布仍未实施。
