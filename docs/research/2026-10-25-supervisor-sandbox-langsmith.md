# Supervisor 接入 Sandbox 与 LangSmith 研究

研究日期：2026-10-25。依据：当前 dev 工作区、已安装 Python 包、LangChain / LangSmith 官方文档。
本轮仅研究与接口核对，未修改生产逻辑、安装依赖、启用外部 tracing、创建云资源、上传素材或发起真实模型调用。
文中区分官方能力、当前项目事实与工程建议；配置示例是拟议接法，不代表已经实施。

**建议保留 AniVAG 当前服务、Kimi Supervisor 和 MySQL checkpoint，先以 LangSmith Sandboxes 作为代码执行环境接入；LangSmith tracing 可独立增加，Agent Server / Managed Deep Agents 则作为后续部署选项。**
官方提供独立 `SandboxClient`，DeepAgents 提供 `LangSmithSandbox` backend，因此采用 LangSmith sandbox 不要求先迁移现有服务到 Agent Server。[LangSmith Sandboxes](https://docs.langchain.com/langsmith/sandboxes)、[DeepAgents sandbox 接入](https://docs.langchain.com/oss/python/deepagents/sandboxes)

## 1. LangSmith 的三种职责应分别决定

| 能力 | 提供什么 | 对当前 AniVAG 的意义 |
| --- | --- | --- |
| Observability / Evaluation | 模型、工具和检索调用的 traces；离线数据集评估、线上评估 | 观察代码生成失败、工具循环和渲染耗时；可接入现有应用，不要求改用指定模型。[Tracing](https://docs.langchain.com/langsmith/observability-quickstart)、[Evaluation types](https://docs.langchain.com/langsmith/evaluation-types) |
| Sandboxes | 隔离执行命令、文件读写、镜像快照、运行中服务预览 | 承载生成的 HTML / PPT / 视频工程、构建与验证。[Sandboxes](https://docs.langchain.com/langsmith/sandboxes) |
| Deployment / Agent Server | Agent 服务运行、流式请求、任务与线程管理、部署伸缩 | 迁移后会涉及服务运行和持久化职责；不是本轮获取 sandbox 能力的先决条件。[Deployment](https://docs.langchain.com/langsmith/deployment) |

LangSmith 的 trace `thread_id` 用于把多次请求的 trace 聚成一次会话；这属于观测关联。AniVAG 当前 LangGraph checkpoint 的 `thread_id` 属于运行状态恢复。建议两者使用同一个 `conversationId`，但仍由各自系统保存各自数据；trace 不能替代 checkpoint 或作品文件。[Observability concepts](https://docs.langchain.com/langsmith/observability-concepts)、[当前 checkpoint 装配](../../server/service/agent_checkpoint.py)

LangChain / LangGraph 已有 tracing 集成，也支持选择性启用、metadata 和自定义 run name。Kimi 可以继续作为模型来源。AniVAG 使用自定义模型适配层，后续需要核对调用树与用量字段是否完整进入 trace；本轮不把“使用 LangChain”视为“已经采集完整 trace”。[LangChain tracing](https://docs.langchain.com/langsmith/trace-with-langchain)、[当前模型适配层](../../gateway/agent_model/deepagents.py)

当前服务的 `server/bootstrap.py` 将 `tracing` 设为 `None`；已有 `gateway/tracing.py` 是 Langfuse 相关机制，模型用量由本地 SQL 账本接收。这些实现不会因安装 LangSmith SDK 自动变为 LangSmith 已接入。[装配](../../server/bootstrap.py)、[现有 tracing](../../gateway/tracing.py)、[用量服务](../../server/service/parse_usage.py)

## 2. 当前安装版本已经具备核心接口

以下来自本机 `importlib.metadata.version()`、导入与 `inspect.signature()`，未调用云端 API。

| 包 | 当前版本 | 本轮确认 |
| --- | --- | --- |
| deepagents | 0.7.23 | `deepagents.backends.LangSmithSandbox` 已导出 |
| langchain | 1.4.4 | 当前 Supervisor 所用 Agent middleware 可继续复用 |
| langgraph | 1.2.11 | 当前图运行与 checkpoint 机制 |
| langsmith | 0.14.7 | `SandboxClient`、`Sandbox`、命令句柄、快照与 service URL 接口存在 |
| websockets | 16.1.1 | SDK 的 WebSocket 传输依赖已安装 |

本地源码证据：[`LangSmithSandbox`](../../.venv/lib/python3.12/site-packages/deepagents/backends/langsmith.py)、[`SandboxClient`](../../.venv/lib/python3.12/site-packages/langsmith/sandbox/_client.py)、[`Sandbox`](../../.venv/lib/python3.12/site-packages/langsmith/sandbox/_sandbox.py)。本机 `.venv` 链接用于本轮复核，跨机器须以锁文件安装结果为准；正式工程依赖见 [pyproject.toml](../../pyproject.toml)、[uv.lock](../../uv.lock)。

核心接入无需先为“找到类”升级依赖；正式使用时应把直接导入的 `langsmith` 依赖及已验证版本范围显式纳入项目依赖管理。本轮没有核实 `LangSmithSandbox` 的最早引入版本，不给出未经证明的最低版本承诺。

版本差异不能忽略：

- 本地 `create_sandbox()` 接收 `snapshot_id` / `snapshot` / `snapshot_name`；本地客户端没有 `create_template()` 或 `create_volume()`。搜索仍会命中旧的 template / volume REST 参考，不应将它们混入当前 SDK 接入代码。[本地客户端](../../.venv/lib/python3.12/site-packages/langsmith/sandbox/_client.py)、[当前 snapshots 文档](https://docs.langchain.com/langsmith/sandbox-snapshots)
- 线上 SDK 文档与本地 `Sandbox.run()` 对命令空闲及日志保留的默认值存在差异：本地签名为 `idle_timeout=300`、`ttl_seconds=600`。正式实现应显式传入所需预算，并以固定版本测试结果为准。[本地 run](../../.venv/lib/python3.12/site-packages/langsmith/sandbox/_sandbox.py)、[SDK lifecycle](https://docs.langchain.com/langsmith/sandbox-sdk#command-lifecycle-and-ttl)
- 当前文档的 RAM 快照恢复属于 REST-only 能力，且部分 overlay filesystem 环境不支持；本地 `capture_snapshot()` 不接受这些 RAM 参数。首版应以文件可恢复为验收目标。[Memory snapshots](https://docs.langchain.com/langsmith/sandbox-snapshots#resume-from-memory)

## 3. LangSmith Sandboxes 能承接哪些工作

### 可用性和接入条件

官方当前列出 GCP US、GCP EU、GCP APAC、AWS US 和 BYOC 为 GA。SDK 使用 LangSmith API key 与对应区域 endpoint；当前用户账号是否具有权限、可用额度和足够并发尚未验证。本轮不承诺免费额度或具体价格。[可用区域](https://docs.langchain.com/langsmith/sandboxes#environment-availability)、[SDK 配置](https://docs.langchain.com/langsmith/sandbox-sdk#create-and-run-a-sandbox)

### 执行、文件与取消

本地 SDK 已提供：

- `Sandbox.run(command, timeout=..., cwd=..., env=...)`，返回 stdout、stderr、退出码；`wait=False` 返回 `CommandHandle`。
- 输出回调、按 `command_id` 重连，以及 `handle.kill()`；可将实际渲染日志接入 AniVAG 的进度流。
- `Sandbox.write(path, str_or_bytes)` / `Sandbox.read(path)`；DeepAgents backend 另外适配 `upload_files()` / `download_files()`。
- `LangSmithSandbox.execute()` 将 `run()` 结果转为 DeepAgents `ExecuteResponse`。本地默认执行预算为 30 分钟，且采用同步 `run()`；AniVAG 的取消事件不会因此自动取消远端进程。

这些接口已通过本地源码和签名核对；真正的超时、断线重连、远端取消行为尚需受控实测。[本地 backend](../../.venv/lib/python3.12/site-packages/deepagents/backends/langsmith.py)、[命令句柄](../../.venv/lib/python3.12/site-packages/langsmith/sandbox/_models.py)、[官方命令执行](https://docs.langchain.com/langsmith/sandbox-sdk#run-commands)

### 环境镜像与持久化

可以从 Docker 镜像或 Dockerfile 构建 snapshot，也可以捕获已有 sandbox 的文件系统。默认快照保留文件，不保证进程继续执行。停止状态保留文件系统；删除则移除 sandbox 及其文件系统。[Snapshot 机制](https://docs.langchain.com/langsmith/sandbox-snapshots)、[Sandbox retention](https://docs.langchain.com/langsmith/sandbox-sdk#sandbox-lifetime-and-retention)

工程建议：制作一个固定版本的创作镜像，预装 Node、Python、FFmpeg、Chromium、字体和选定的 PPT / 视频库。能构建自定义镜像是官方能力；这些软件的完整组合能否在目标区域成功渲染，仍是本项目的验证项，不能由“可执行 shell”直接推导成功。

本地 `create_sandbox()` 已支持 `idle_ttl_seconds` 和 `delete_after_stop_seconds`。应明确“空闲停止”和“停止后删除”是两阶段策略；正式作品及可编辑工程必须在 AniVAG 的存储中保留可恢复副本，避免把 sandbox 保留期当成永久作品存储。

同一 conversation 的多轮制作宜复用其 sandbox。`with client.sandbox()` 在本地 SDK 中会启用退出时自动删除，适合一次性探针，不适合直接包住每轮业务请求；会话生命周期应由 Server 管理。[本地 context-manager 实现](../../.venv/lib/python3.12/site-packages/langsmith/sandbox/_client.py)

### 素材和网络

LangSmith 当前默认对非 Enterprise 组织应用受限出网；组织级策略会覆盖调用方自设的允许列表。列出一个素材域名或给它配置凭据，并不自动获得访问权。[Restricted egress](https://docs.langchain.com/langsmith/sandbox-auth-proxy#organization-level-restricted-egress)

因此，首版建议由 AniVAG 宿主读取已获准使用的片段，再通过文件 API 上传，或从已验证可达的对象存储读取。远端 sandbox 无法直接把本机 `127.0.0.1` 当作 AniVAG 服务地址。Kimi API 与数据库访问继续留在宿主，生成代码只处理准备好的素材和工程文件；这是对现有架构的工程建议。

当前 mount 接口支持 S3、GCS、公开 Git 仓库和 Context Hub。基础 mounts 的官方 Python 版本要求为 `langsmith>=0.8.16`，当前 0.14.7 已高于它；较新的 IAM-role 等细分选项另有要求，不能用基础最低版本推定全部可用。[Mounts](https://docs.langchain.com/langsmith/sandbox-mounts)

### HTML 预览和下载

`sandbox.service(port=...)` 可给正在运行的 HTTP 服务生成访问地址。当前文档的 service token 默认 10 分钟，最长 24 小时；链接持有者可访问，无需 LangSmith 登录。地址不会替用户启动 HTTP 服务。[Service URLs](https://docs.langchain.com/langsmith/sandbox-service-urls)

SDK 还提供文件下载链接；链接对应路径而非不可变内容版本，后续修改同一路径不应被视为稳定的版本交付。[Download links](https://docs.langchain.com/langsmith/sandbox-download-links)

工程建议：service URL 用于制作过程的临时预览。正式作品保存到 AniVAG ArtifactStore 后，再由现有租户鉴权与新的作品合同交付前端；不要直接把短期 bearer 预览链接当永久作品地址。浏览器 iframe 的跨域 cookie 行为、预览隔离策略、字体与资源加载都要实测。

## 4. 推荐架构：Agent 在宿主，生成代码在 sandbox

DeepAgents 官方给出了“Agent 在 sandbox 内”和“Agent 在宿主、工具访问 sandbox”两种模式。AniVAG 已经有模型接入、数据库、权限和检索工具，推荐采用后者：继续在现有 Server 中运行 Supervisor，把文件与命令执行交给 backend。[架构模式](https://docs.langchain.com/oss/python/deepagents/sandboxes#integration-patterns)

拟议的身份关联：

| 标识 | 归属 | 职责 |
| --- | --- | --- |
| `conversationId` / LangGraph `thread_id` | AniVAG / LangGraph | 对话与原生上下文 |
| sandbox name / ID | Sandbox 服务 | 当前可执行工作环境 |
| snapshot ID | Sandbox 服务 | 基础镜像或文件恢复点 |
| artifact ID / version | AniVAG | 用户获得的作品与可编辑工程版本 |
| trace run ID | LangSmith Observability | 一次请求及其子调用的观测记录 |

这几个标识不应互相替代。可以在业务会话下保存 sandbox 绑定，trace metadata 同时带上 conversation、message 和 sandbox 标识；成功作品另行发布。当前使用 `conversationId` 作为 checkpoint thread 的逻辑可以保留。[当前 Supervisor 调用](../../agent/chat/supervisor.py)、[当前持久化机制](../../gateway/agent_model/deepagents.py)

`LangSmithSandbox` 负责执行能力，但不会自动给 AniVAG 增加素材导出、作品发布、HTML 预览、PPT 渲染或视频播放器。对这些产物，应提供所需的镜像工具链、创作指令与确定性检查，并通过宿主发布工具提交作品。这是接入方案，不是官方产品自动提供这些业务能力的承诺。

### 4.1 当前代码的实际缺口

| 位置 | 当前事实 | 建议调整与目的 |
| --- | --- | --- |
| [DeepAgents adapter](../../gateway/agent_model/deepagents.py) | 每轮创建 `StateBackend()`，自定义 `FilesystemMiddleware(tools=["read_file"])`；接收 `session_context` 后直接丢弃 | 从宿主取得本会话执行 backend，启用写文件与执行工具；继续使用当前 Kimi adapter、模型预算与 checkpoint 确认逻辑 |
| [Supervisor](../../agent/chat/supervisor.py) | 已有检索、观察帧、提交原片区间等工具；`query_reports` 等缓存仅存本轮内存 | 增加受范围约束的素材准备与作品发布工具；文件、代码与渲染使用 SDK 工具，不另造模型循环 |
| [AgentChatService](../../server/service/agent_chat.py) | 负责会话权限、执行生命周期、SSE、完成消息与原生上下文确认 | 负责工作区绑定、取消/超时、作品正式提交与回收；这些责任继续由服务层持有 |
| [ArtifactStore](../../server/storage/artifact.py) | 有原视频及会话图片附件存储；图片接口限制图片类型 | 增加会话生成物的不可变文件与 manifest，复用现有会话归属和完整性校验模式 |
| [前端消息合同](../../web/src/api/contracts.ts)、[SearchPage](../../web/src/features/search/SearchPage.tsx) | 支持 Markdown、三种业务工具状态、原视频区间卡 | 第一批可交付可信 Markdown 预览/下载链接；后续增加通用作品卡，再按 HTML/PPTX/视频选择展示方式 |

本地无网络接口探针已确认：

1. 当前 `StateBackend + tools=["read_file"]` 不具备执行能力。
2. 仅替换成 `LangSmithSandbox`，保留当前 allowlist，模型仍只看到 `read_file`。
3. 放开 `ls/read_file/write_file/edit_file/glob/grep/execute` 后，文件与命令工具才全部注册。
4. `TodoListMiddleware()` 提供 `write_todos`。当前 Supervisor 应显式接入规划能力，不能依赖泛化的“DeepAgents 自带规划”描述推断本项目已经配置。
5. `CompositeBackend.execute()` 始终委派给 **default backend**；将 sandbox 仅挂到 `/workspace/`、default 仍用 StateBackend，不会获得可执行能力。探针里的工具静态清单不代表模型最终可调用清单，运行前还会做 backend 能力过滤。[本地 middleware](../../.venv/lib/python3.12/site-packages/deepagents/middleware/filesystem.py)、[本地 CompositeBackend](../../.venv/lib/python3.12/site-packages/deepagents/backends/composite.py)、[Todo middleware](../../.venv/lib/python3.12/site-packages/langchain/agents/middleware/todo.py)

建议保持两种存储职责：Agent 历史卸载文件继续跟随 MySQL checkpoint；创作工程文件放在 sandbox 的真实文件系统。以下只是**新会话的装配示意**，省略业务工具、预算、取消、持久化装配；不是可以直接替换现有代码的补丁：

```python
backend = CompositeBackend(
    default=LangSmithSandbox(sandbox),
    routes={"/agent-context/": StateBackend()},
    artifacts_root="/agent-context",
)
middleware = [
    TodoListMiddleware(),
    FilesystemMiddleware(
        backend=backend,
        tools=["ls", "read_file", "write_file", "edit_file", "glob", "grep", "execute"],
    ),
    # 保留宿主控制 middleware；摘要 middleware 使用同一个 backend。
]
# create_deep_agent(..., backend=backend, middleware=middleware,
#                   skills=["/workspace/skills/"], checkpointer=...)
```

旧 checkpoint 的虚拟文件已有 `/conversation_history/...` 路径；Composite 路由会剥掉前缀后访问子 backend。直接换前缀会使旧摘要中的路径找不到文件。第一批需验证旧路径兼容或做明确的状态内路径转换，不能把换 backend 当作无状态配置改动。[现有摘要回归](../../tests/gateway/test_deepagents.py)、[路由实现](../../.venv/lib/python3.12/site-packages/deepagents/backends/composite.py)

### 4.2 工作区生命周期与 checkpoint 的关系

建议按 conversation 隔离工作区，在首次创作操作时按需分配；普通检索不应因为能力开关打开就无条件分配长期运行实例。Server 校验会话归属后获取 backend，通过现有每轮 `SupervisorSessionContext` 边界传入。sandbox 绑定可复用 `conversation.detail.runtimeSessions` 的命名空间及现有保存方法，绑定键应独立于 Kimi 的具体模型名；目前不存在这项 sandbox 绑定。[宿主会话合同](../../gateway/agent_model/contracts.py)、[现有绑定保存](../../server/repository/agent.py)

- 同一会话下一轮优先复用工作区；按现有并发约束，同一会话只允许一个活动制作请求。
- sandbox 停止后恢复文件；被删除时根据已保存工程重建，更新 sandbox 绑定。`conversationId` 与 checkpoint thread 保持不变。
- **checkpoint 丢弃不会撤销文件写入、下载或远端进程。** 推荐第一版采用明确草稿语义：失败/取消保留工作文件，下一轮重新读取实际工作区；只有检查完成的产物进入正式作品版本。若需要逐轮文件回滚，再单独实现文件快照/源码版本，不把历史回滚误称为工作区回滚。
- 成品及可编辑源工程应另存 ArtifactStore；文件落盘后，再将可信引用提交到业务消息。未成功提交的孤立文件由现有风格的回收流程处理。
- 长命令显式设置执行、空闲与日志 TTL，使用 command handle 映射到宿主取消；当前 `_HostControlMiddleware` 只在工具前后检查，无法中断正在阻塞等待的原生 `execute()`。进程重启后的命令识别与清理也应纳入验证，不仅处理浏览器断线。

以上是工程建议；当前代码仍是每轮内存运行、完成后确认原生 checkpoint。[确认/丢弃实现](../../gateway/agent_model/deepagents.py)、[Service 提交顺序](../../server/service/agent_chat.py)

### 4.3 HTML、PPT 与视频共用执行层，分别配置制作规范

DeepAgents skills 可以携带工作流、脚本、参考与模板，按需加载。因此这三种制作能力适合提供为领域 skills，共用文件/执行工具与作品发布通道。[DeepAgents skills](https://docs.langchain.com/oss/python/deepagents/skills)

| 作品 | 建议工具链 | 交付与验收 |
| --- | --- | --- |
| HTML | HTML/CSS/JS；Chromium/Playwright 做浏览器检查 | 保存完整网页或自包含 HTML；检查页面错误、资源加载、关键交互与截图，提供隔离预览及下载 |
| 可编辑 PPT | Node + PptxGenJS；LibreOffice 转 PDF/图片作预览检查 | 交付 `.pptx` 源文件及预览；检查逐页布局、字体与溢出，不能将 HTML 幻灯片或页面截图称作可编辑 PPTX |
| 视频 | 优先复用项目已有 HyperFrames；FFmpeg/FFprobe 准备素材、检查媒体；Chromium 检查时间线 | 根据用户目标交付离线 HTML 短片或 MP4；核对时长、音轨、关键帧、素材范围及引用来源 |

PptxGenJS 官方支持 Node 创建并保存 `.pptx`；LibreOffice 官方提供 headless 与格式转换参数。[PptxGenJS quick start](https://gitbrent.github.io/PptxGenJS/docs/quick-start/)、[LibreOffice CLI](https://help.libreoffice.org/latest/en-US/text/shared/guide/start_parameters.html)

HyperFrames 官方 CLI 提供 lint、snapshot 和 render，渲染依赖浏览器与 FFmpeg。建议在固定版本 sandbox 镜像内运行其本地渲染模式，不把 Docker-in-Docker 当作必要前提；实际镜像、字体、内存和渲染参数须通过 PoC 固定。[HyperFrames CLI](https://hyperframes.app/docs/5-packages/cli)、[Rendering](https://hyperframes.app/docs/3-guides/4-rendering)

项目已有 [离线 HTML 打包器](../../.agents/skills/anivag-short-video/scripts/package_offline_html.py) 与 [素材准备脚本](../../.agents/skills/anivag-short-video/scripts/anivag_materials.py)。其中素材脚本仍使用旧 `workspaceId`，配套 skill 还限定本地 Codex；当前用户已选择 Kimi，本次仅将其作为已有实现证据，不能直接复制旧启动配置或宣称脚本已适配现有 `tenantId` 合同。

生成过程推荐采用同一个模型循环：先列计划，取必要素材，写源文件，执行，读错误/截图并修改，再由宿主发布工具收取产物。先不用增加独立 Planner/Coder/Renderer Agent；是否需要分工由实际上下文与耗时结果决定。

### 4.4 前端交付建议分两步

第一步复用当前 Markdown 消息：宿主 `publish_artifact` 接收 sandbox 中的文件路径，校验会话范围、文件类型/大小、实际内容及检查结果，导出到 ArtifactStore，返回宿主生成的预览/下载地址。链接由确定性逻辑登记并进入回答，不能让模型自行声明任意路径已发布。现有 Supervisor 的网页链接过滤也需认识宿主正式生成物，避免合法作品链接被当成未核实网页链接替换。[现有答案链接处理](../../agent/chat/supervisor.py)

第二步再扩展 `artifact` render block，前端统一展示作品名称、类型、状态、预览与下载：HTML 用隔离预览；PPTX 展示 PDF/图片预览与源文件下载；MP4 用视频播放器。沿用已有 SSE 和 `agent_message.detail.renderBlocks`，保存产物 ID 与必要元数据，不把 HTML 正文、PPTX 或 MP4 字节写进聊天 JSON。[消息序列化](../../server/api/agent.py)、[现有 renderBlocks 存储](../../server/repository/agent.py)

HTML 预览的执行边界需要明确：生成脚本运行在独立预览 origin 或受 sandbox CSP/iframe 约束的上下文，不能获得主应用会话能力。第一批不要求新建全功能编辑器；作品下载和隔离预览可先形成可验证交付。

## 5. 为什么先不迁移 Agent Server

LangSmith Deployment 提供 Agent Server 及部署生命周期，官方区分 Cloud、BYOC/Hybrid、自托管与 standalone；standalone 还需要 PostgreSQL、Redis 及对应许可。选择它会扩大当前服务、持久化和运维改造范围。[Deployment 选项](https://docs.langchain.com/langsmith/deployment)

本轮目标是让已有 Supervisor 做创作任务，现有独立 SandboxClient 已能提供所需边界。建议先完成一条代码与产物链路，之后若需要任务恢复、队列伸缩或统一托管，再单独评估 Agent Server / Managed Deep Agents。这个先后顺序是基于 AniVAG 现状的工程判断。

如果 LangSmith 账号或区域能力不适合当前素材路径，DeepAgents 的 sandbox backend 接口允许切换其他已集成供应商；官方列出 Daytona、E2B、Modal、Runloop、Vercel、AgentCore 等。首版只应实现一个供应商，不提前建设多供应商调度层。[官方 integrations](https://docs.langchain.com/oss/python/deepagents/sandboxes)

## 6. 接入前应验证的实际问题

以下是后续受控 PoC 的验收项，本轮未执行：

1. 账号所在区域可创建 / 查询 / 停止 / 删除 sandbox；所需 CPU、内存、磁盘与并发配额足够。
2. 固定镜像能生成一份 HTML、一份 PPTX 和一段短视频；浏览器截图与视频检查成功，记录真实冷启动及渲染耗时。
3. 代码收到执行失败后能读取错误并修复；取消会停止对应远端命令，断线不会无限保留后台渲染。
4. 在同一会话修改已有作品；服务重启后能找到 sandbox，sandbox 被删后能从已保存工程恢复。
5. 临时预览链接过期后可重新获取；正式作品不依赖沙箱继续运行，跨会话与跨租户访问遵守 AniVAG 权限。
6. 选择性 tracing 能关联 Supervisor、检索、执行、发布步骤，并正确记录 Kimi 模型名与已有用量口径；不把可见 trace 数量当业务成功证明。

## 7. 分批实施建议与首批候选修改树

**A：环境可行性。** 用固定快照验证浏览器、字体、文件上传下载、短命令、取消和重连；再完成最小 HTML、PPTX、视频样例。先测这些能力，可避免在业务接好之后才发现账号/网络/渲染资源不适合。

**B：首条业务闭环。** 当前 Supervisor 能规划、写 HTML、读取错误并修正、发布预览/下载链接，在同一 conversation 继续修改，重启后恢复工作区。直接复用当前 Markdown 展示，不要求先改整个前端消息协议。这个阶段依然需要宿主发布与鉴权，不能以终端执行成功代替产品交付。

**C：扩展制作与呈现。** 增加 PPTX 和视频 skills、镜像依赖及格式验收；随后增加作品卡、渲染日志和版本展示。沿用同一执行与存储通道。LangSmith tracing 可作为独立小批接入，用一轮任务验证 trace 树及 Kimi 用量字段。

下面是 B 阶段的**候选**最小范围，尚未实施。A 阶段若推翻 backend/恢复/预览方案，应重新估算；它不授权现在修改这些文件。

```text
AniVAG/
├── [M] pyproject.toml                          ← 显式声明直接使用的 sandbox SDK 依赖
├── [M] uv.lock                                 ← 锁定已验证依赖
├── agent/
│   └── chat/
│       ├── [M] supervisor.py                   ← 暴露发布工具并绑定可信作品链接
│       └── skills/
│           └── authoring/
│               └── [A] SKILL.md                ← HTML 编写、执行检查与交付规范
├── gateway/
│   └── agent_model/
│       ├── [M] contracts.py                    ← 经现有每轮宿主上下文传入执行 backend
│       └── [M] deepagents.py                   ← plan、文件/执行工具、旧 context 路径兼容
├── server/
│   ├── [M] bootstrap.py                        ← 装配 sandbox 客户端与工作区服务
│   ├── config/
│   │   ├── [A] sandbox.py                      ← 区域、快照、资源、执行及保留预算
│   │   └── [M] settings.py                     ← 加载唯一配置来源
│   ├── caller/
│   │   └── [A] sandbox.py                      ← SDK 适配、命令句柄与远端取消
│   ├── service/
│   │   ├── [A] agent_workspace.py              ← 会话绑定、工作区恢复、生成物发布
│   │   └── [M] agent_chat.py                   ← 连接每轮执行、取消与正式提交
│   ├── storage/
│   │   └── [M] artifact.py                     ← 生成物不可变文件和 manifest
│   ├── api/
│   │   └── [M] agent.py                        ← 鉴权后的生成物下载/隔离预览响应
│   └── router/
│       └── [M] agent.py                        ← 注册生成物读取入口
├── deploy/
│   └── sandbox/
│       └── [A] Dockerfile                      ← 固定创作运行环境快照来源
└── tests/
    ├── gateway/
    │   └── [M] test_deepagents.py              ← 旧 checkpoint、规划与真实工具能力合同
    └── server/
        ├── [A] test_agent_workspace.py         ← 会话隔离、取消、恢复、文件与发布合同
        └── [M] test_agent_chat_service.py      ← 完成/失败/取消时的作品和上下文关系
```

初步量级：12 个生产逻辑文件、3 个测试文件、4 个依赖/构建/skill 文件，共 19 个候选文件（6 新增、13 修改，无删除/重命名）。生产逻辑约新增 350–650 行、删除 30–100 行；测试约新增 150–250 行；配置/构建/skill 约新增 80–160 行；锁文件量取决于依赖解析结果。此为基于现有分层的估算，尚非逐行实现承诺。

B 阶段预期为新增读取接口、扩展内部会话 JSON 和文件 manifest；当前消息/视频卡合同保持兼容，预计无需数据库 schema 迁移。实现完成后需要重启服务装配新能力；研究阶段不执行重启、迁移、commit 或 push。C 阶段才扩展前端作品合同，届时另列文件树与兼容验证。

直接保护本次行为的验收应覆盖：Kimi 确实能写入并执行；执行失败后可修复；旧摘要文件可读；同会话续改；跨会话文件不可见；取消停止远端命令；发布文件在 sandbox 删除后仍可下载；服务重启后关联仍可恢复。现有检索不使用 sandbox 时仍能完成，是必要的既有行为回归。

## 本轮检查范围

- 已查官方文档、已安装 SDK 源码及接口签名。
- 已执行无网络、无模型调用的 middleware/backend 能力探针，确认工具 allowlist、规划工具与 CompositeBackend 默认执行路由。
- 未测试真实 LangSmith 账号、区域、计费、网络可达性或 CPU 渲染性能。
- 未调用真实模型，未迁移或改动现有服务。
- 当前研究文档为本轮唯一新增文件；工作区原有变更由其原任务继续管理。

本轮实际修改树（上方第 7 节为未来候选范围）：

```text
AniVAG/
└── docs/
    └── research/
        └── [A] 2026-10-25-supervisor-sandbox-langsmith.md
            影响：保存官方来源、当前代码证据、方案与候选实施范围；不改变运行行为。
```

实际范围为 1 个新增 Markdown、0 个生产/测试代码文件；37 个本地引用均存在，代码围栏配对及空白检查通过。未运行生产测试套件，因为本轮没有修改可执行代码；接口探针只检查已安装包的装配行为，不代表真实云端验收。
