# 本地配置指南：API 模型 + Docker 数据库

适用范围：2026-10-25 的 `dev` 工作区，包括尚未提交的启动器和 DeepAgents checkpoint 改动。本文供开发者和早期试用者配置环境；对外发布前，需将依赖的改动一并发布，并用干净 clone 验收。

当前可用链路是上传视频、解析入库、检索、观看与连续对话。HTML 页面、HTML 幻灯片和 HTML 短片是后续创作产物方向；Chat 尚未接入可写文件、执行代码的 sandbox，安装本地服务不会自动获得这些能力。

## 1. 选择安装路线

推荐首次安装使用 **模型走 API、数据库用 Docker、应用运行在宿主机**。视频和业务数据保存在自己的电脑上，模型调用仍会向所选供应商发送相应内容。

| 组件 | 当前运行方式 |
| --- | --- |
| MySQL 8.4、Elasticsearch 8.19.22 | 根目录 Compose；含健康检查、持久卷、loopback 端口 |
| API、视频解析 Consumer | 宿主机 Python 3.12，同一服务进程 |
| Web | 宿主机 Node 24+ / Vite |
| ffmpeg / ffprobe | 宿主机安装 |
| 对话、视觉、转写、embedding | 本指南配置为远程 API |
| Redis、Mailpit | Compose 可选 profile；普通 local 模式不需要 |
| 创作 sandbox、HTML 产物发布和预览 | 当前尚未接入 |

macOS 是现有本机运行环境。Linux 可按同样的宿主机 + Compose 路线配置，但本次未实机验收；Windows 建议在 WSL2 内完成这套配置，未验证原生 Windows 启动。启动器依赖 Bash、`fcntl`、`ps`、`lsof` 和 POSIX 进程信号。

这里的整套服务包含数据库、索引、视频处理和应用，不能套用“单个创作 sandbox 用 1 核 2 GB”的资源估算。先降低解析并发，再按自己的视频规模观察内存和磁盘用量。

## 2. 安装基础工具

需要 Git、[uv](https://docs.astral.sh/uv/getting-started/installation/)、Python **3.12**、Node.js **24+**、npm、ffmpeg、ffprobe、lsof，以及已启动的 Docker Desktop 或 Docker Engine + Compose。

Compose 需要支持 `up --wait`；[Docker 官方入门](https://docs.docker.com/compose/gettingstarted/)说明了安装前提。macOS 使用原生 ES 的替代路线见[原生 Elasticsearch](elasticsearch/README.md)，已有原生 MySQL 也可以继续使用。

在仓库根目录执行：

```bash
uv sync --locked
npm --prefix web ci
docker compose version
ffmpeg -version
ffprobe -version
```

更新仓库后也应运行 `uv sync --locked` 和 `npm --prefix web ci`。懒人脚本只在 Python 环境不存在或 Vite 缺失时安装依赖，不能代替锁文件更新后的同步。

## 3. 准备私有环境文件

```bash
test -f .env.local || cp .env.example .env.local
mkdir -p outputs/local-services
```

首次安装可沿用 `.env.example` 的开发数据库和索引名。在 `.env.local` 中设置或替换以下项目；已有环境应保留原数据库、索引和媒体目录，不用下面的默认值覆盖它们。

```dotenv
ANIVAG_ENVIRONMENT=development
ANIVAG_AUTH_MODE=local
ANIVAG_LOCAL_TENANT_ID=local-default
ANIVAG_DATABASE_URL=mysql+pymysql://root@127.0.0.1:3306/anivag_development?charset=utf8mb4
ANIVAG_ELASTICSEARCH_URL=http://127.0.0.1:9200
ANIVAG_ELASTICSEARCH_INDEX=anivag_video_chunks_development
SEEKINO_ELASTICSEARCH_EVENT_INDEX=seekino_video_events_development_v3
ANIVAG_ARTIFACT_ROOT=outputs/server/artifacts
ANIVAG_MODEL_CONFIG=outputs/local-services/local-api-models.yaml
KIMI_API_KEY=填写自己的Kimi凭据
ARK_API_KEY=填写自己的Ark凭据

# 单人首次验收从低并发开始；以后按机器和供应商限额调高。
ANIVAG_VIDEO_CONCURRENCY=1
ANIVAG_PARSE_CONCURRENCY=1
ANIVAG_REFINE_CONCURRENCY=1
ANIVAG_INDEX_CONCURRENCY=1
ANIVAG_ASR_CONCURRENCY=1
```

local 模式无需注册、Redis 或 SMTP。首次安装不要启用 `ANIVAG_AUTH_MODE=cloud`；云端身份的 Redis、SMTP、签名秘密和租约约束另见[部署说明](README.md)。

## 4. 配置不依赖本地权重的模型

把下列完整 YAML 保存为 `outputs/local-services/local-api-models.yaml`。它让生成式 Agent、视觉、OCR、摘要和重排走 Kimi，embedding 和转写走 Ark；关闭可选的 Jev / 本地 Qwen 查询改写，不配置本地 CAM++ 声纹。

这是一份减少本机安装依赖的起步配置，与开发机当前的 Whisper / CAM++ 组合不同。本次只验证配置与应用配置装配，**未对这一整套组合做真实模型调用和上传验收**。转写使用项目已有的 Ark 视频转写适配器，其效果应另测；需要 Whisper 或自己的音频 API 时，按[转写配置](../docs/model-config.md#ds--第三方-asr)替换 `transcription`。

Kimi endpoint 和 `k3.1-pro` 沿用当前 dev 绑定，也可在 [Kimi 官方文档](https://www.kimi.com/code/docs/)核对。使用与 endpoint 匹配且有模型权限的个人凭据；Ark 模型 ID 或 endpoint 也须以自己的账户实际权限为准。YAML 只存密钥变量名。

```yaml
config_version: 2
supervisor_harness: deepagents
query_hook:
  enabled: false
tracing:
  enabled: false
  environment: development

model_sources:
  kimi_coding:
    adapter: openai
    provider: kimi
    profile: kimi
    base_url: https://api.kimi.com/coding/v1
    api_key_env: KIMI_API_KEY
  ark_default:
    adapter: ark
    provider: ark
    api_key_env: ARK_API_KEY

models:
  agent:
    source: kimi_coding
    model: k3.1-pro
    supports_vision_input: true
    reasoning_effort: low
    max_output_tokens: 8192
    request_timeout_seconds: 600
  supervisor:
    source: kimi_coding
    model: k3.1-pro
    supports_vision_input: true
    reasoning_effort: low
    max_output_tokens: 8192
    request_timeout_seconds: 1200
    context_window_tokens: 64000
    context_safety_margin_tokens: 4096
    auto_compact_tokens: 48000
    compact_target_tokens: 24000
    max_input_chars: 200000
  transcription:
    kind: transcription
    source: ark_default
    model: doubao-seed-2-0-lite-260428
    request_timeout_seconds: 600
  vision:
    source: kimi_coding
    model: k3.1-pro
    supports_vision_input: true
    reasoning_effort: low
    max_output_tokens: 8192
    request_timeout_seconds: 600
  ocr:
    source: kimi_coding
    model: k3.1-pro
    supports_vision_input: true
    reasoning_effort: low
    max_output_tokens: 8192
    request_timeout_seconds: 600
  summarize:
    source: kimi_coding
    model: k3.1-pro
    supports_vision_input: true
    reasoning_effort: low
    max_output_tokens: 8192
    request_timeout_seconds: 600
  embedding:
    kind: embedding
    source: ark_default
    model: doubao-embedding-vision-250615
    dimensions: 1024
    video_fps: 1.0
    request_timeout_seconds: 600
  rerank:
    kind: rerank
    source: kimi_coding
    model: k3.1-pro
    reasoning_effort: low
    max_output_tokens: 8192
    request_timeout_seconds: 180

agent_runtime:
  limits_enabled: true
  total_timeout_enabled: true
  maximum_concurrent_kicks: 1
  maximum_parallel_queries: 1
  query_reasoning_effort: low
  supervisor_reasoning_effort: low
  stage_timeout_seconds: 480
  supervisor_timeout_seconds: 1200
  execution_timeout_seconds: 1200

video_parser:
  transcript_fps: 1.0
  video_fps: 1.0
  fallback:
    enabled: false
    frame_fps: 0.5
    group_duration_seconds: 30
    max_frames_per_group: 15
    max_height: 720
```

这个 profile 不需要 Codex 登录、`DEEPSEEK_API_KEY`、`WHISPER_API_KEY`、`OPENROUTER_JEV_API_KEY`、8B 改写权重或声纹依赖。不要同时保留旧的 `ANIVAG_AGENT_*_MODEL`、`ANIVAG_TRANSCRIPT_MODEL` 等模型覆盖；它们可能把有效模型重新改掉。已有素材库不要随意更换 embedding 模型、来源或维度，索引和编码器身份必须匹配。

可先执行纯配置检查，不调用模型、不连接数据库：

```bash
uv run --no-sync --env-file .env.local python -c 'from gateway.config import load_config, required_api_key_env_names; c = load_config(); print("harness:", c.supervisor_harness); print("required keys:", required_api_key_env_names(c))'
```

应显示 `deepagents`，所需密钥变量名为 `ARK_API_KEY`、`KIMI_API_KEY`。这只校验配置结构，不验证密钥有效性或模型效果。

## 5. 初始化数据库、checkpoint 和索引

首次安装默认数据库时执行：

```bash
docker compose --env-file .env.local up -d --wait mysql elasticsearch
uv run --no-sync --env-file .env.local python -m server.cli init-db
uv run --no-sync --env-file .env.local python -m server.cli init-es-index
```

按顺序运行，前一步失败时先修复再继续。Compose 首次初始化只创建默认库 `anivag_development`；自定义数据库 URL 时先创建对应数据库和账号权限。`init-db` 初始化当前业务 Schema 和 DeepAgents checkpoint 表，不创建数据库。

**不要省略 `init-db`。** 当前懒人脚本调用 `create_schema()`，只覆盖业务表；它没有调用 checkpoint saver 的 `setup()`。已有表且服务健康，不代表新库的连续对话已可用。

旧数据库不能按新安装处理。业务 Schema 不兼容时，遵照报错和[已有数据库升级说明](../server/README.md)评估迁移；ES mapping 不兼容时需规划重建。上述命令不会自动迁移旧业务数据或替换不兼容索引。

Compose 端口与应用连接要一起修改：例如 MySQL 改发布端口时，同时调整 `ANIVAG_MYSQL_PORT` 和 `ANIVAG_DATABASE_URL`；ES 对应 `ANIVAG_ELASTICSEARCH_PORT` 与 `ANIVAG_ELASTICSEARCH_URL`。这份空密码、loopback Compose 用于本地开发。

## 6. 启动、检查与停止

当前 `dev` 工作区使用：

```bash
./start.sh --branch dev
./start.sh --branch dev status
```

默认 `./start.sh` 仍定位已检出的 `main` 工作区。`--branch dev` 只选择已有 dev worktree，不会切换、拉取或创建分支。启动器复用数据库，校验 API / Consumer、资料库查询和 Web 代理；源码或启动配置变化时，会更新自己管理的 API/Web。

访问 [Web](http://127.0.0.1:5173)、[API 文档](http://127.0.0.1:9621/docs)、[健康状态](http://127.0.0.1:9621/healthz)。首次没有任何视频属于正常空库状态，仓库不附带可直接使用的已解析素材库。

验收依次做：创建系列 → 上传一个短视频 → 等待解析和索引完成 → 检索片段 → 播放来源视频 → 连续追问两轮。`/healthz` 正常只证明服务就绪，不能代替转写、embedding、模型工具调用和 checkpoint 的真实验收。

日常停止：

```bash
./start.sh --branch dev stop
docker compose --env-file .env.local stop mysql elasticsearch
```

第一条只停止启动器管理的进程，Docker 数据库需单独停止；已有原生 ES、Whisper 和查询改写服务也不归它管理。停止保留数据。需要继续运行时，先启动所用数据库，再执行启动器。

### 手动启动替代方式

希望前台调试或安装版本尚不支持 `--branch` 时，完成前面的初始化后，分别在两个终端运行：

```bash
# 终端一，仓库根目录
uv run --no-sync --env-file .env.local python -m server.main
```

```bash
# 终端二，仓库根目录
uv run --no-sync --env-file .env.local npm --prefix web run dev -- --host 127.0.0.1 --port 5173 --strictPort
```

用 Ctrl-C 分别停止。手动命令中，已 export 的同名 shell 环境变量优先于 env 文件；启动器则会先清除 `.env.local` 中已有变量的 shell 覆盖。自定义 Web 端口时同步修改 `ANIVAG_WEB_PORT`、允许的来源与手动 Vite 的 `--port`。

## 7. 数据、日志与常见问题

| 内容 | 默认位置 |
| --- | --- |
| 私有配置 | `.env.local`、`outputs/local-services/local-api-models.yaml` |
| 媒体、切片、帧、附件 | `ANIVAG_ARTIFACT_ROOT`，默认 `outputs/server/artifacts` |
| 启动器日志和 PID | `outputs/local-services/`，重点查看 `api.log`、`web.log` |
| 应用日志 | 默认 `outputs/server/logs` |
| Compose 业务库和 checkpoint | `anivag-local_mysql_data` volume |
| Compose ES 数据 | `anivag-local_elasticsearch_data` volume |

备份至少包含 MySQL、对应媒体和有效配置；仅复制 ES 或 Git 仓库不足以恢复素材库。不要把 `outputs/` 整体当缓存，也不要用 `docker compose down -v` 作为日常停止命令。

| 症状 | 优先检查 |
| --- | --- |
| 找不到 main 工作区 | 当前版本用 `./start.sh --branch dev`，或按手动启动章节运行 |
| 仍提示 Codex / DeepSeek / Whisper 密钥 | `ANIVAG_MODEL_CONFIG` 是否指向本指南 YAML；检查 shell 和模型角色覆盖 |
| API 正常，首次 Chat 报 checkpoint 表不存在 | 使用同一 `.env.local` 执行 `server.cli init-db` |
| 首次模型调用 401 / 403 / 模型不存在 | 密钥与 endpoint 是否对应、模型权限、账户可用模型 ID；启动器只检查必需密钥是否非空 |
| 本地模型服务未运行 | 本指南 profile 不依赖它们；使用根配置时需另行启动 Whisper / Qwen 并准备 CAM++ |
| 上传后解析失败 | `api.log`、任务错误、转写/视觉/embedding 权限、ffmpeg 和磁盘空间 |
| Docker 数据库启动端口冲突 | 是否已有原生 MySQL/ES；选用其中一套或同时修改发布端口和应用连接 |
| 页面显示正常但查不到内容 | 空库、解析未完成或索引合同不匹配；按任务状态和日志定位 |

本地 UI 也支持连接固定的远端资料源，配置见 [Web README](../web/README.md)；它不自动同步本机视频，也不表示本地和远端模型配置共用。

## 8. Docker 当前覆盖与验收边界

根 Compose 已有数据库镜像、健康检查、持久卷和可选的 Redis/Mailpit。仓库当前没有 Seekino 应用 Dockerfile、API/Web 镜像发布、全栈 Compose 或创作 sandbox 镜像；CI 也没有完整 Compose 安装验收。`docker compose up` 只启动基础设施。

本指南核对了当前源码、Compose YAML、模型配置与纯配置装配。检查机器没有 Docker CLI，未执行容器拉取、全新 Docker 安装、Linux/WSL2 验收或这份 API profile 的付费模型调用。现有开发机的健康状态只代表其已配置环境，不能当作新用户安装成功的证据。

后续降低安装门槛，优先补齐可发布的 API 模型模板、统一的业务表/checkpoint 初始化入口、API/Web/ffmpeg 应用镜像，以及从空卷启动到首轮对话的安装验收；HTML 创作 sandbox 还需单独接入。
