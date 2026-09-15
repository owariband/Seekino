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

仓库根目录是运行配置的唯一入口：`config.yaml` 保存可提交的模型默认值，`.env.local` 保存 API Key 和个人 Endpoint，不进入 Git。

```bash
cp .env.example .env.local
# 编辑 .env.local，填写 ARK_API_KEY、DEEPSEEK_API_KEY 和个人 ep-* Endpoint。

uv sync
uv run --env-file .env.local python -m gateway.cli --help
```

视频解析命令见 [`engine/README.md`](engine/README.md)，模型调用与跨模态向量
冒烟见 [`gateway/README.md`](gateway/README.md)，业务事实与索引同步命令见
[`server/README.md`](server/README.md)。所有命令都从仓库根目录运行。

## 仓库结构

```text
AniVAG/
├── pyproject.toml                    # 统一依赖与测试配置；package = false
├── uv.lock                           # 全仓唯一依赖锁
├── config.yaml                       # 可提交的模型默认配置
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

`engines/VideoRAG` 来源于 [Slimzeo/VideoRAG](https://github.com/Slimzeo/VideoRAG)。AniVAG 会保留上游许可证与提交历史，并明确区分 Adapter 集成、独立复现和动画领域改造三类贡献。
