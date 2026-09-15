# AniVAG

AniVAG 是一个面向事件级视频检索与时间证据定位的多模态视频理解产品。M1 以动画名场面为首个落地场景：用户导入若干集动画视频后，可以用自然语言查询梗、动作、人物关系或情绪变化，并获得可跳转的集数、时间戳与关键帧证据。

仓库根目录就是 AniVAG 的工程边界，不再额外套 `packages/`、`src/` 或
`anivag_*` 分发包。`server/`、`engine/`、`gateway/` 是产品内部模块命名空间，
不是三个需要独立发布的 Python 包，也不通过 `__init__.py` 批量暴露实现。

## M1 边界

- Anime-first：课程视频只作为未来迁移验证，不改变产品定位。
- Video-only：用户不需要上传外挂字幕文件。
- 语音、镜头与关键帧进入查询前的异步建库链路；主路径调用 Doubao Pro，明确触发内容审核时降级为按需 Lite ASR、分组抽帧、官方 DeepSeek Flash 视觉取证与文本整理。烧录字幕 OCR 暂不阻塞第一版。
- 在线查询融合语音文本、视觉与角色事件候选，返回时间证据。
- V1 使用持久化 Job Store + Worker；RocketMQ 不是单用户 MVP 的前置依赖。

## 快速开始

仓库根目录是运行配置的唯一入口：`config.yaml` 保存可提交的模型默认值，`config/model-pricing.yaml` 保存 Parse 模型人民币单价，`.env.local` 保存 API Key 和个人 Endpoint，不进入 Git。

```bash
cp .env.example .env.local
# 编辑 .env.local，填写 ARK_API_KEY、DEEPSEEK_API_KEY 和个人 ep-* Endpoint。

uv sync
uv run --env-file .env.local python -m gateway.cli --help
```

视频解析命令见 [`engine/README.md`](engine/README.md)，模型调用与跨模态向量
冒烟见 [`gateway/README.md`](gateway/README.md)，业务事实与索引同步命令见
[`server/README.md`](server/README.md)。所有命令都从仓库根目录运行。

本地验证时，可直接懒启动 MySQL、原生 Qdrant、API、视频 Worker 和 Web：

```bash
./start.sh          # 已运行的服务直接复用，只启动缺少的进程
./start.sh status   # 查看服务监听 / readiness 和 Worker 进程状态
./start.sh stop     # 只停止本脚本启动的进程，保留数据
```

默认 Web 为 `http://127.0.0.1:5173`，API 文档为 `http://127.0.0.1:9621/docs`。
脚本读取根目录 `.env.local`，保留显式环境配置。缺少数据库配置时，默认连接
`root` / 空密码的 `127.0.0.1:3306/anivag_development` 和
`http://127.0.0.1:6333` 的 `anivag_video_chunks_development` 集合。
首次自建 MySQL 使用该 root 账号；复用已有认证实例时，在 `.env.local` 配置其数据库账号。

需要本机已有 `uv`、Node、npm、MySQL 8、Qdrant、ffmpeg / ffprobe 和 `lsof`。
脚本会在缺少 `.venv` / Web 依赖时执行 `uv sync` / `npm ci`。
可通过 `ANIVAG_MYSQLD_BIN` / `ANIVAG_QDRANT_BIN` 指定二进制；也会查找 Homebrew
MySQL 8.4 和 `~/.local/bin/qdrant`。Gateway / Engine 由 API / Worker 调用，无需额外进程。

自建数据、PID 与日志保存在 `outputs/local-services/`，媒体仍使用
`ANIVAG_ARTIFACT_ROOT`（默认 `outputs/server/artifacts`）。启动会创建缺失的开发库 / 表 /
Qdrant 集合，并校验现有 Schema；旧 Schema 会报错，需要按迁移说明处理。
不清空数据、不自动 ALTER 旧表。启动失败后已启动的进程会保留，可查看日志、重试或运行 stop。

可在 `.env.local` 修改 `ANIVAG_HTTP_PORT`、`ANIVAG_WEB_PORT`、数据库 URL 端口和
Qdrant URL 端口；自建 Qdrant 的 gRPC 端口由 `ANIVAG_QDRANT_GRPC_PORT` 指定（默认 6334）。
Vite 代理会跟随 API 端口。此脚本仅用于 localhost development 环境。
修改运行配置后，先 `./start.sh stop` 再启动；脚本会检查自管应用的配置摘要，防止复用旧配置。

## 仓库结构

```text
AniVAG/
├── pyproject.toml                    # 统一依赖与测试配置；package = false
├── uv.lock                           # 全仓唯一依赖锁
├── config.yaml                       # 可提交的模型默认配置
├── config/model-pricing.yaml          # Parse 模型计价属性与价格版本，人民币
├── server/                           # 工程编排、事实源、持久化与外部接口
├── engine/                           # VideoChunk 解析与检索算法
├── gateway/                          # 模型供应商调用与数据归一化
├── tests/                            # 按 server/engine/gateway/evals 分区的测试
├── evals/                            # 算法评测工具、数据集与报告
├── outputs/                          # 本地运行产物，不进入 Git
├── deploy/                           # 本地基础设施与部署配置
├── engines/
│   └── VideoRAG/                     # 上游算法参考（Git submodule）
├── assets/                           # 本地样例素材与领域参考
└── docs/                             # 方案、决策与开发记录
```

这里列的是当前已经存在的目录，不代表最终系统只有这些文件。Server 已包含 HTTP API、
MySQL DB Queue Worker 和环境隔离的本地 Artifact Store；Web 层独立推进。Artifact Store
只负责媒体与其他二进制产物，Embedding 的 durable source 仍是 MySQL，不因引入
Artifact Store 自动外置。完整目标结构与阶段记录维护在
`docs/design/technical_solution copy.mdv.document.working.md`。

## 运行入口

根 `pyproject.toml` 只负责依赖和工具配置，没有构建系统、分发包入口或全量公共 API。
当前各进程使用显式模块入口：

```bash
uv run python -m engine.parse.cli --help
uv run python -m evals.recall.cli --help
uv run python -m gateway.cli --help
uv run python -m server.cli --help
uv run python -m server.api
uv run python -m server.mq --once
```

## 上游归属

前端请求与公开返回模型见 [UI DTO / VO 契约](docs/api-ui-views.md)，包含逐消息 Series 范围、分页 / SSE、媒体读取与旧数据库迁移说明。

解析任务的模型 Token、视频与职责归属、人民币预估费用及查询接口见 [Parse 模型消耗统计](docs/parse-model-usage.md)。任务页展开“模型消耗”查看；价格配置变更只影响后续调用。

`engines/VideoRAG` 来源于 [Slimzeo/VideoRAG](https://github.com/Slimzeo/VideoRAG)。AniVAG 会保留上游许可证与提交历史，并明确区分 Adapter 集成、独立复现和动画领域改造三类贡献。
