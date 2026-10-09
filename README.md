# anivag

anivag 是一个面向事件级视频检索与时间证据定位的多模态视频理解产品。M1 以动画名场面为首个落地场景：用户导入若干集动画视频后，可以用自然语言查询梗、动作、人物关系或情绪变化，并获得可跳转的集数、时间戳与关键帧证据。

## 一张图看懂 anivag

从私有视频到有据可查的回答：anivag 在后台解析语音、画面与时间线，建立可检索的多模态索引，再把自然语言问题对应到具体片段。

[![anivag 架构图：私有视频输入、持久化解析、多模态索引、检索 Agent 与可跳转的视频证据](assets/diagrams/anivag-open-source-architecture.png)](assets/diagrams/anivag-open-source-architecture.png)

*点击图片查看完整架构图。*

- **本地优先，私有范围**：视频、解析产物和索引留在自己的运行环境，模型连接由自己的配置管理。
- **一次解析，多路检索**：语音文本、场景与关键帧汇入时间线，支持文本、语义和视觉证据召回。
- **答案能回到原片**：结果附带集数、时间段与关键帧，方便核对并跳转到对应视频位置。

仓库根目录就是 anivag 的工程边界，不再额外套 `packages/`、`src/` 或
`anivag_*` 分发包。`server/`、`agent/`、`engine/`、`gateway/` 是产品内部模块
命名空间，不是需要独立发布的 Python 包，也不通过 `__init__.py` 批量暴露实现。

四层职责固定为：Gateway 统一模型调用，Engine 定义领域算法与检索策略，Agent
运行 Agent／LangGraph 程序，Server 负责可靠执行、持久化和对外协议。默认解析模式
为 V2，新上传任务由 `engine/parsev2/`、`agent/video_parse_v2/` 和 Server V2 Worker 执行，
需要配置 Elasticsearch。正式 Server 只接受 `ANIVAG_PARSE_EXECUTION_MODE=v2`，
冻结在 `engine/parsev1/` 的旧链仅供历史参考和离线诊断。

模型消耗页面从现有 MySQL 的 `model_usage` 查询已记录 Token，覆盖 V2 解析/index 与助手问答。
Server 在实际模型调用完成后追加记录；数据库写入失败保留业务结果，页面只声明已记录范围。
`config.yaml` 默认关闭可选 tracing，统计无需 Langfuse 地址或凭据。新表由现有 init-db 入口登记，
实际建表和服务运行由运行负责人核实操作授权。合同和统计边界见 [模型 Token 用量](docs/parse-model-usage.md)。

## M1 边界

- Anime-first：课程视频只作为未来迁移验证，不改变产品定位。
- Video-only：用户不需要上传外挂字幕文件。
- 语音、镜头与关键帧进入查询前的异步建库链路；Server 驱动 V2 LangGraph，
  Engine 构造 ASR/OCR 事件，Agent 决定理解与 Refine 流程。
- 在线查询融合语音文本、视觉与角色事件候选，返回时间证据。
- V2 使用 MySQL 持久化 Queue + anivag Server 进程内 Consumer；RocketMQ 不是单用户 MVP 的前置依赖。

## 冻结的 Parse V1

历史 V1 Pipeline 和适配代码保留用于参考，不接入正式 Server 的启动、上传、删除或检索。
旧链的算法顺序为：

```text
准备窗口与媒体 → ASR Agent → VideoUnderstandingAgent → EmbeddingAgent → MySQL 业务事实 → Qdrant 索引
```

各阶段通过显式 DTO 和 `ParsePersistence` 端口交接。provider、model 与 tier 只决定
运行配置和 provenance，不改变 V1 Pipeline 拓扑；内容审核 fallback 复用同一份
Transcript，不会补跑 ASR。

V1 不是 LangGraph。失败重试由 ParseJobConsumer 与 MySQL Queue 管理，MySQL
Checkpoint、Artifact 和 Lease/Fencing 才是恢复真相；Pipeline 内存状态只覆盖当前
一次执行。当前 Video Parse V2 由 `agent/video_parse_v2/` 定义 LangGraph 程序，调用
`engine/parsev2/` 的领域算法，并把持久任务、恢复和版本选择留给 Server。
历史 V1/V2 混合数据库记录仍由任务删除事务清理，正式流程无需连接 Qdrant。

## 快速开始

仓库根目录是运行配置的唯一入口：`config.yaml` 保存可提交的模型默认值和全局 Chat Agent 运行参数，`config/model-pricing.yaml` 保存 Parse 模型人民币单价，`.env.local` 保存 API Key 和个人 Endpoint，不进入 Git。Chat Agent 参数位于 `config.yaml` 的 `agent_runtime` 节，也可从前端「设置」页编辑。YAML 中显式填写的值优先于同名 `ANIVAG_AGENT_*` 环境变量；缺失字段沿用环境变量或程序默认值。

模型连接配置拆成两层：`model_sources` 描述协议 Adapter、用量 Provider 和
认证方式，`models` 分别选择 Agent、ASR、画面理解及视觉降级模型；向量配置保持原样。
旧的 `agent_model`、`video_parser.*_model` 等配置仍然兼容。
DS + 第三方 ASR、第三方视觉接口和迁移规则见 [`docs/model-config.md`](docs/model-config.md)。
DeepSeek 是 `adapter: openai` 下的 `profile: deepseek`；Codex 使用 app-server，Claude
使用 Agent SDK。可复制的 source 示例及认证说明见
[`gateway/README.md`](gateway/README.md#chat-agent-模型来源)。角色模型环境变量覆盖仍然有效，
fallback 的来源与模型都在 YAML 中显式配置。

设置页保存和手工编辑 `agent_runtime` 后，超时、上下文预算、检索及工具输出上限等参数由下一条新消息使用；进行中的回答继续使用开始时的参数。`maximum_concurrent_kicks` 保存后需要重启 Server，设置页会区分保存值和当前生效值。模型连接、数据库等启动配置仍需重启；配置错误会在读取或保存时报告。设置页通过 Server API 修改本机的 `config.yaml`，浏览器不会直接访问服务器文件。

```bash
cp .env.example .env.local
# 编辑 .env.local，按所选 provider 填写需要的 API Key 和个人 ep-* Endpoint。

uv sync
uv run --env-file .env.local python -m tests.manual.gateway_embedding --help
```

视频解析命令见 [`engine/README.md`](engine/README.md)，模型调用与跨模态向量
冒烟见 [`gateway/README.md`](gateway/README.md)，业务事实与索引同步命令见
[`server/README.md`](server/README.md)。所有命令都从仓库根目录运行。

本地验证时，先准备 Elasticsearch，再懒启动 MySQL、anivag Server（API + V2
进程内 Parse Consumer）和 Web：

```bash
./start.sh          # 已运行的服务直接复用，只启动缺少的进程
./start.sh status   # 查看服务监听，以及 API / Parse Consumer readiness
./start.sh stop     # 只停止本脚本启动的进程，保留数据
```

默认 Web 为 `http://127.0.0.1:5173`，API 文档为 `http://127.0.0.1:9621/docs`。
脚本读取根目录 `.env.local`；模型 API Key 以该文件为准，其它显式环境配置会保留。缺少数据库配置时，默认连接
`root` / 空密码的 `127.0.0.1:3306/anivag_development`。
ES 的 URL 和索引名必须通过 `ANIVAG_ELASTICSEARCH_URL` / `ANIVAG_ELASTICSEARCH_INDEX` 显式配置，
示例文件使用本机 9200 端口和开发索引。
首次自建 MySQL 使用该 root 账号；复用已有认证实例时，在 `.env.local` 配置其数据库账号。

需要本机已有 `uv`、Node、npm、MySQL 8、ffmpeg / ffprobe，以及可访问的 Elasticsearch。
脚本会在缺少 `.venv` / Web 依赖时执行 `uv sync` / `npm ci`。
可通过 `ANIVAG_MYSQLD_BIN` 指定二进制；也会查找 Homebrew
MySQL 8.4。Gateway / Engine 由 anivag Server 进程内调用，
无需额外进程。

自建数据、PID 与日志保存在 `outputs/local-services/`，媒体仍使用
`ANIVAG_ARTIFACT_ROOT`（默认 `outputs/server/artifacts`）。启动会创建缺失的开发库 / 表 /
ES 索引，并校验现有 Schema；旧 Schema 会报错，需要按迁移说明处理。
不清空数据、不自动 ALTER 旧表。启动失败后已启动的进程会保留，可查看日志、重试或运行 stop。

可在 `.env.local` 修改 `ANIVAG_HTTP_PORT`、`ANIVAG_WEB_PORT`、数据库 URL 端口和
ES URL 端口；ES 进程由运行环境准备，脚本只验证和复用。
Vite 代理会跟随 API 端口。此脚本仅用于 localhost development 环境。
修改 `config.yaml` 中可即时生效的 `agent_runtime` 参数后，新消息直接使用新值；
`maximum_concurrent_kicks`、模型/解析配置及其他启动配置修改后，先运行
`./start.sh stop` 再启动。脚本会检查需要重启的配置摘要，防止复用旧配置。

### 可选：用 Docker 运行数据库

`./start.sh` 可以复用自行准备的数据库。若希望省去本机安装 MySQL / Elasticsearch，
可以先用根目录 [`compose.yaml`](compose.yaml) 启动数据库，再由原脚本复用它们：

```bash
docker compose up -d --wait && ./start.sh
```

前后端、ffmpeg 和视频处理仍在本机运行；数据库使用 Docker 持久卷，端口仅发布到
`127.0.0.1`。`./start.sh stop` 不会停止 Compose 容器，需要时另执行 `docker compose stop`。
每次选择 Docker 模式都先运行上述命令，避免数据库未运行时原脚本启动本机实例。
自定义端口、已有数据和视频测试说明见 [Docker 开发说明](deploy/README.md)。

Compose 默认启动 MySQL + ES。在 `.env.local` 显式配置 `ANIVAG_ELASTICSEARCH_URL` 和
`ANIVAG_ELASTICSEARCH_INDEX` 后，`./start.sh` 会校验 ES 集群及索引映射。
Interval 在 Refine 成果提交后由 V2 Worker 写入并核验 ES，
再完成任务。API、Agent Chat 与 CLI search 会同时选择 V2 ES 检索且不会回退到
V1/Qdrant；Web 已接入 V2 Task 列表、单任务事件和 retry/reparse/delete 操作门禁。
启用前需应用 Server 说明列出的受控迁移。状态迁移及能力边界见
[Server 说明](server/README.md)。

### 云端游客、邮箱登录与公开课程

`ANIVAG_AUTH_MODE=local` 使用明确的 `local-user` / `local-default` 本地身份，保留无需登录的本机流程。云端设置 `ANIVAG_AUTH_MODE=cloud`，并配置 [.env.example](.env.example) 中的签名秘密、Redis 与 SMTP；缺少这些配置会拒绝启动。正式入口使用同源 HTTPS，受控反代保留公开 Host 并正确传递 HTTPS scheme 和客户端地址，使 IP 额度按访问者计数；Cookie 默认为 HttpOnly、Secure、SameSite=Lax。

开发 Redis 是 Compose 的可选 profile，端口只监听 loopback，AOF 保存在独立持久卷：

```bash
docker compose --profile cloud-auth up -d --wait redis
```

首次访问先读取 `/api/auth/me`，cloud 缺少或过期 Cookie 返回 401，前端显式调用 `/api/auth/guest` 获取游客。游客能浏览、检索、播放 `platform-public` 中已发布且解析完成的课程；对话按本人 owner 隔离。验证码登录后拥有个人 Tenant，可在个人资料和公共课程之间切换。公开课程读取权限不授予平台写权限。普通账号不能修改全局模型配置；管理员由受控邮箱配置及数据库角色表达。

首版仅新增 `user`、`tenant` 两张 SQL 表。个人 Tenant 使用独立 UUID 和显式 owner；验证码的 HMAC 摘要、TTL、尝试次数、冷却及限流放在 Redis，不保存在 User，不返回开发验证码。正式账号退出会撤销同账号的旧版本凭据并换发游客身份。接口包含 me、guest、email/send、email/verify、logout，cloud 写请求携带 `X-CSRF-Token`。

模型请求默认按 86400 秒窗口限制：游客共享 IP 预算 20 次，登录用户按 user_id 200 次；并发分别为 1 和 3，同时受服务总并发上限控制。游客更换身份不会重置 IP 预算。租约默认 600 秒，Agent 执行超时必须小于租约；游客每轮最多 4 次模型调用、8 次工具调用。local 免请求额度。这些是请求次数、并发及单轮调用限制，实际 Token 的预扣/结算硬额度尚未实现，`model_usage` 用于已记录消耗统计。

旧数据库需要受控应用 [20261010 身份迁移](docs/migrations/20261010_user_tenant_identity.sql)；`init-db` 遇到旧 22 表、`workspace_id` 或缺少身份字段时明确报升级错误，不自动修改。SQL 保留资源 ID 和旧范围值，历史 creator/actor 保持 NULL，本机历史对话归 `local-user`，所有课程默认未发布。脚本预检只接受已明确的 `local-default` 旧范围，其他范围先建立显式映射。历史证据 JSON 由读取适配规范化为新 Tenant 合同。

ES v5 旧索引先通过独立工具检查复制计划：

```bash
uv run python -m scripts.migrate_tenant_index --source old_development --destination tenant_development --environment development
```

停 API/Worker 写入后追加 `--apply`，复制为 v6 新物理索引并验证成功，才切换 `ANIVAG_ELASTICSEARCH_INDEX`；保留 `ANIVAG_ELASTICSEARCH_INDEX_GENERATION`、旧索引、文档 ID、external version 和向量，不重算 embedding。生产环境命令选择 `--environment production` 和对应索引名。该工具和 SQL 已准备，本轮未对现有数据执行。

隔离 auth 与真实 Redis 请求额度测试合计 **49 passed、零 skip**（临时 SQLite、独立 Redis、邮件/模型替身），ES 复制预检 **27 项测试**通过；前端 **229 项单元测试**、生产构建和 **2 项 Chromium 流程测试**通过，浏览器用例使用模拟 API 验证游客初始化、邮箱登录、退出及身份切换。身份 SQL 已在隔离 MySQL 8.4 验证旧 22 表升级至 24 表，并保留历史记录。真实 SMTP 发信、云端部署、课程准备与真实服务端浏览器联调仍需部署验收。职责按 [config](server/config/auth.py)、[constants](server/constants/auth.py)、[error](server/error/auth.py)、[utils](server/utils/auth.py)、[service](server/service/auth.py)、[repository](server/repository/identity.py) 分层，详细设计见 [用户/Tenant TRD](server/design/user-tenant-trd.md)。

HTTP 进程的总装配入口是 [server/main.py](server/main.py)，`python -m server.api` 转交该入口，原启动参数保持可用。装配顺序为：

```text
main.init_application(settings)       # 配置检查与进程总装配
├── caller.init(settings, ...)        # MySQL → Redis → Elasticsearch
└── bootstrap.build_application_services(settings, resources, ...)
    └── Repository → Service → Agent／Consumer
main.create_http_application(...)    # 将上述运行时交给 HTTP 生命周期
└── api.app.build_app(services, ...)  # 中间件、异常处理、路由绑定
```

MySQL、Elasticsearch、Redis 的客户端创建、连接适配和释放在 [server/caller](server/caller/__init__.py)：[mysql.py](server/caller/mysql.py)、[elasticsearch.py](server/caller/elasticsearch.py)、[redis.py](server/caller/redis.py)。`CallerResources` 持有共享客户端，`ApplicationServices` 持有业务服务，`ApplicationRuntime` 负责先停止业务、再释放客户端；构造或启动失败也执行清理。`AuthService` 只借用 Redis，SQL Schema 管理继续在 `database.py`，ES Mapping、索引及检索继续在 `storage/elasticsearch.py`。

[server/cli.py](server/cli.py) 是一次性命令入口，按命令借用 caller 的资源上下文：`init-db` 使用 MySQL，`init-es-index` 使用 ES，`search` 使用 MySQL、ES 和查询编码器。上下文退出时自动释放资源；CLI 不启动 HTTP、后台 Consumer 或登录 Redis。测试中注入 `create_app(services)` 的服务仍由注入方管理。

## 仓库结构

```text
anivag/
├── pyproject.toml                    # 统一依赖与测试配置；package = false
├── uv.lock                           # 全仓唯一依赖锁
├── config.yaml                       # 可提交的模型默认配置
├── config/model-pricing.yaml          # Parse 模型计价属性与价格版本，人民币
├── gateway/                          # 模型供应商调用；agent_model/ 提供模型执行协议
├── engine/                           # parsev1、parsev2 与 rag 领域算法
├── agent/                            # Chat 与 Video Parse V2 的 Agent／LangGraph 程序
├── server/                           # 可靠执行、事实源、持久化与外部接口
├── tests/                            # 按 agent/server/engine/gateway/evals 分区的测试
├── evals/                            # 算法评测工具、数据集与报告
├── outputs/                          # 本地运行产物，不进入 Git
├── deploy/                           # 本地基础设施与部署配置
├── assets/                           # 架构图、本地样例素材与领域参考
│   └── diagrams/                     # README 架构图
└── docs/                             # 方案、决策与开发记录
```

这里列的是当前已经存在的目录，不代表最终系统只有这些文件。Server 已包含 HTTP API、
MySQL DB Queue、V2 进程内 Consumer 和环境隔离的本地 Artifact Store；Web 层独立推进。
Artifact Store
只负责媒体与其他二进制产物，Embedding 的 durable source 仍是 MySQL，不因引入
Artifact Store 自动外置。完整目标结构与阶段记录维护在
`docs/design/technical_solution copy.mdv.document.working.md`。

## 运行入口

根 `pyproject.toml` 只负责依赖和工具配置，没有构建系统、分发包入口或全量公共 API。
当前服务、管理和评测使用以下显式模块入口：

```bash
uv run python -m engine.parsev1.cli --help
uv run python -m evals.recall.cli --help
uv run python -m server.cli --help
uv run python -m server.api
```

手动冒烟和单任务诊断入口放在 `tests/manual/`，从仓库根目录显式运行：

```bash
uv run python -m tests.manual.gateway_embedding --help
uv run python -m tests.manual.server_mq_once --once
```

`server.api` 是唯一常驻 Backend PSM；单任务诊断只领取至多一个到期任务，
不应与 API 并行启动。上述手动脚本不属于默认 pytest 收集的测试文件。

## 上游归属

前端请求与公开返回模型见 [UI DTO / VO 契约](docs/api-ui-views.md)，包含逐消息 Series 范围、分页 / SSE、媒体读取与旧数据库迁移说明。

模型消耗页面按模型或任务/Chat 展示已记录 Token、已知小计与未知/截断状态，详见 [模型 Token 用量](docs/parse-model-usage.md)。历史价格和金额保留在旧表，不参与本地统计。
