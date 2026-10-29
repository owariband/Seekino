# 本地 Docker 配置指南

默认使用 DeepAgents 和远程 API 模型。MySQL、Elasticsearch、API、解析 Consumer、ffmpeg、Web 都由 Compose 启动，宿主机无需安装 Python、Node、ffmpeg 或运行 `start.sh`。

这份配置用于本机单用户资料库，端口只绑定 `127.0.0.1`。云端公开服务的身份、HTTPS 和凭据策略另见[云端部署说明](cloud/README.md)。Chat 创作 sandbox 尚未接入；应用运行在 Docker 内不等于 Agent 已获得执行代码的工具。

## 1. 准备 Docker 和凭据

Windows/macOS 安装并启动 Docker Desktop；Linux 使用 Docker Engine 与 Compose。Windows 选择 Linux containers，通常使用 WSL 2 后端，直接在 PowerShell 执行下列命令即可，不需要额外进入 WSL 终端。环境要求见 [Docker Windows 官方说明](https://docs.docker.com/desktop/setup/install/windows-install/)。

使用支持 `env_file.required` 与 `up --wait` 的新版 Compose：

```bash
docker compose version
```

在仓库根目录首次复制简短环境模板；已有 `.env.docker` 则编辑原文件，保留自己的配置：

```bash
cp deploy/docker/env.example .env.docker
```

PowerShell 也可使用 `Copy-Item deploy/docker/env.example .env.docker`。填写：

```dotenv
ARK_API_KEY=你的Ark凭据
DEEPSEEK_API_KEY=你的DeepSeek凭据
```

默认 Supervisor、视觉、OCR、摘要、转写和向量使用 Ark；检索 Agent 和重排使用 DeepSeek。模型 ID 沿用项目已有 API 配置，实际使用前需核对账户权限或改为自己的 endpoint。默认关闭可选查询改写，不需要 Codex 登录、Whisper、Qwen 或 CAM++ 权重。

若本机已运行开发服务，在 `.env.docker` 中改发布端口，避免占用同一套端口。例如 Web 5174、API 9622、MySQL 3307、ES 9201。容器内部仍使用 `mysql:3306`、`elasticsearch:9200`、`api:9621`，发布端口变化不改变容器间地址。

## 2. 一条命令启动

```bash
docker compose --env-file .env.docker up --build -d --wait
```

首次构建需要联网下载镜像、Python 和前端依赖。启动顺序为：

1. MySQL、Elasticsearch 健康检查通过。
2. `initialize` 首次导入模型配置，检查配置与必需凭据，运行现有 `init-db` 和 `init-es-index`。
3. `init-db` 创建业务表和 DeepAgents checkpoint 表；初始化失败会阻止 API 启动。
4. API 和进程内 Consumer 就绪后启动 Nginx 静态 Web。

默认入口：

- Web：<http://127.0.0.1:5173>
- API 文档：<http://127.0.0.1:5173/docs>，也可直连 <http://127.0.0.1:9621/docs>
- 健康检查：<http://127.0.0.1:5173/healthz>

首次为空库，没有预装已解析素材。创建系列、上传一个短视频，等待解析和索引完成，再验证检索、视频播放和两轮连续对话。服务健康不代表模型权限、解析效果和工具调用已通过实际验收。

已有数据库会先检查兼容性；这条启动链不会自动迁移旧业务数据或替换不兼容索引。旧版本升级按报错和 [Server 说明](../server/README.md)处理。

## 3. 模型仍用现有配置方式

不维护按供应商拆分的 YAML。继续使用现有 `model_sources`、`models`、`supervisor_harness` 和 `SEEKINO_MODEL_CONFIG`，完整字段见[模型配置](../docs/model-config.md)。

`.env.docker` 中的 `SEEKINO_MODEL_CONFIG=./config.yaml` 指向**首次导入的宿主机文件**，相对路径以仓库根目录为准，也可以选择自己的既有 YAML。初始化后，容器使用持久卷中的 `/data/config/config.yaml`，设置页修改也保存到这里。普通启动和镜像重建不会覆盖这份有效配置。

首次启动前可以直接编辑源 YAML；已经启动过的实例，按本节末尾的步骤重新导入。凭据始终写在 `.env.docker`，不要放进 YAML。

### 可选 Kimi

项目已经声明 `kimi_coding` 来源，沿用 `adapter: openai`、`profile: kimi` 和 `https://api.kimi.com/coding/v1`。将同一份 YAML 中以下字段改为对应值，保留其余模型和预算设置：

```yaml
supervisor_harness: deepagents
models:
  supervisor:
    source: kimi_coding
    model: k3.1-pro
    supports_vision_input: true
    reasoning_effort: low
```

宿主机开发在项目根目录 `.env.local` 填写 `KIMI_API_KEY`；完整容器部署填写 `.env.docker`。密钥只提供鉴权，不会自动切换模型；endpoint、profile 和模型选择继续由同一份有效 YAML 管理，不新增同名环境变量。后端模型密钥不要放进 `web/.env.local` 或任何 `VITE_*` 变量。

上述示例只切换 Supervisor；`models.agent`、`vision`、`vision_fallback`、`ocr`、`summarize`、`rerank` 可以分别选择 `kimi_coding` / `k3.1-pro`，保留各角色现有预算、超时及视觉能力字段。转写和 embedding 保持各自专用模型。Kimi endpoint 和模型权限可对照 [Kimi 官方文档](https://www.kimi.com/code/docs/)。

推理字段须按实际适配路径设置：Chat 的 Agent / Supervisor 支持 `profile: kimi` 和 `reasoning_effort: low`；通过 `GatewayClient.create_generative_model` 构造的 `vision`、`vision_fallback`、`ocr`、`summarize`、`rerank` 当前使用 `OpenAIVisionModel`，其 `reasoning_effort` 只能设为 `none` 或不设置，沿用其他供应商的 `high` / `medium` 会在构造时失败。这条路径中的 `none` 表示请求省略推理参数，使用供应商默认行为，不等于关闭 Kimi thinking。

切换后先使用实际生效的 YAML 和环境变量，对上述五个角色执行 `GatewayClient.create_generative_model` 构造并关闭资源；这项检查不发模型请求，可以发现配置加载校验未覆盖的适配错误。构造通过后仍需以短片验证 OCR、解析、索引与问答，不能替代真实模型验收。

| 有效模型配置 | 所需凭据 |
| --- | --- |
| 当前默认 Ark + DeepSeek | `ARK_API_KEY`、`DEEPSEEK_API_KEY` |
| 仅 Supervisor 切换到 Kimi，其余保持默认 | 上述两项，加 `KIMI_API_KEY` |
| 所有生成式角色及 fallback 使用 Kimi，转写和向量保留 Ark | `KIMI_API_KEY`、`ARK_API_KEY` |

第三种方案还需处理 `models.agent.fallback`：切换到 Kimi，或移除与主模型重复的 fallback；同时核对 `models.vision_fallback`。只填 Key 或只改 Supervisor 都不能移除仍被其他角色使用的凭据。上述三种组合的配置加载校验不代表所有角色已通过运行时构造或真实调用；补齐环境模板不会改变仓库的默认模型选择。

### 可选 Codex

继续使用已有 `codex_app_server` 适配器。将 Supervisor 改为：

```yaml
supervisor_harness: codex
models:
  supervisor:
    source: codex_local
    model: gpt-5.6-sol
    supports_vision_input: true
    reasoning_effort: low
```

这是修改现有字段的片段，不是替换整个文件。Codex Supervisor 不接受 `fallback`；若原绑定有该字段，切换时移除。模型 ID 可改为自己账户可用的 ID。其他角色仍按各自绑定运行。

镜像复用项目锁定的 `openai-codex` SDK 和随包 runtime，不另装一套宿主机 Codex。先在容器内完成个人登录：

```bash
docker compose --env-file .env.docker run --build --rm --no-deps api codex login --device-auth -c 'cli_auth_credentials_store="file"'
```

按终端给出的链接和一次性验证码，在浏览器完成授权；设备登录须由个人或工作区设置允许，见 [OpenAI 登录说明](https://learn.chatgpt.com/docs/auth)。登录状态与原生会话保存在持久卷 `/data/codex`，API 使用同一 `CODEX_HOME`。这不会复用宿主机的私有 Codex 目录。

完成后使用第 2 节的 Compose 启动命令。检查登录状态可以运行：

```bash
docker compose --env-file .env.docker run --rm --no-deps api codex login status
```

### 已有实例更新配置

只修改 API Key 或环境参数后，重新执行 `docker compose --env-file .env.docker up -d --wait`，让 Compose 重建环境发生变化的服务；仅 `restart` 不会更新容器环境。

如果修改了宿主机 YAML 或 `SEEKINO_MODEL_CONFIG`，明确重新导入，以下步骤会替换有效模型 YAML，但保留媒体和数据库：

```bash
docker compose --env-file .env.docker stop web api
docker compose --env-file .env.docker run --rm --no-deps initialize cp /app/model-template.yaml /data/config/config.yaml
docker compose --env-file .env.docker up -d --wait
```

重新导入会同时替换设置页保存的 Agent 参数。如需保留，先导出有效配置再编辑：

```bash
docker compose --env-file .env.docker cp api:/data/config/config.yaml ./config.saved.yaml
```

可将 `SEEKINO_MODEL_CONFIG` 指向这个文件。不要在已有素材库上随意更换 embedding 模型、来源或维度，索引与编码器身份必须匹配。

宿主机服务地址 `127.0.0.1` 在容器里指容器自身。连接自己部署的模型时，应使用容器可达地址；Linux Compose 已配置 `host.docker.internal`。现有 `openai_audio` 校验仅允许远端 HTTPS 或 loopback HTTP，宿主机 Whisper 的非 loopback 地址需要 HTTPS；Docker 不绕过这个校验，也不自动部署本地模型权重。

## 4. 日常运行与数据

```bash
docker compose --env-file .env.docker ps -a
docker compose --env-file .env.docker logs --tail=100 initialize api web
docker compose --env-file .env.docker stop
docker compose --env-file .env.docker up -d --wait
```

更新代码后再次执行带 `--build` 的启动命令。`initialize` 成功后显示 `Exited (0)` 属于正常状态，它是一次性初始化服务。

| 数据 | 默认位置 |
| --- | --- |
| 模型、全局 Agent 设置 | `seekino-local_app_data` 卷的 `/data/config/config.yaml` |
| 视频、切片、帧、附件 | 同一卷的 `/data/artifacts` |
| 应用日志 | 同一卷的 `/data/logs`；容器标准输出也可用 `compose logs` 查看 |
| Codex 登录与会话 | 同一卷的 `/data/codex` |
| 业务数据、DeepAgents checkpoint | `seekino-local_mysql_data` 卷 |
| ES 索引 | `seekino-local_elasticsearch_data` 卷 |

`stop` 和普通 `down` 保留命名卷；`down -v` 会删除它们，不能作为日常停止命令。备份至少保留 MySQL、`app_data` 和私有环境文件，ES 不能替代完整备份。应用镜像不包含 `.env`、个人登录目录、视频或本机权重。

| 问题 | 检查方式 |
| --- | --- |
| `Set credentials in .env.docker` | 按报错变量名填写所选来源需要的密钥；不会打印密钥值 |
| 修改 YAML 后没生效 | 已有有效配置保存在持久卷，按“已有实例更新配置”重新导入 |
| `initialize` 失败 | 查看它的日志；检查旧库兼容性、ES mapping、配置和凭据 |
| API 正常但模型调用失败 | 核对供应商模型权限、endpoint、转写与 embedding；启动不会做付费探测 |
| 端口占用 | 修改 `.env.docker` 发布端口，或停止占用的本机服务 |
| Windows 构建失败 | 确认 Docker Desktop 已运行、选用 Linux containers，检查代理与镜像下载网络 |

## 5. 宿主机开发仍可使用

需要断点调试或 Vite 热更新时，只启动数据库，再按 [README 的宿主机步骤](../README.md#宿主机开发可选)运行 API/Web：

```bash
docker compose up -d --wait mysql elasticsearch
```

该路线使用 `.env.local` 和宿主机工具；`./start.sh --branch dev` 仍只管理宿主机进程。完整 Docker 路线不经过它，因此不依赖 Windows 上的 Bash、`fcntl` 或 `lsof`。两条路线若同时运行，需要隔离端口、数据库和媒体。

## 验证边界

本次配置已通过以下检查：

- Compose CLI 解析默认服务、可选 profile、自定义端口和包含空格的模型路径。
- 初始化脚本的首次导入、失败中止、重复运行，以及设置页原子写入后的配置保留测试。
- 默认 DeepAgents、现有 Kimi / Codex 绑定和服务配置 / 装配回归测试。
- 前端生产构建、Nginx 配置语法检查；Linux amd64 / arm64 的锁定依赖解析 dry-run。

以上是原容器部署配置的验证记录。2026-10-28 已在新 Mac 验证 MySQL/Elasticsearch 容器健康、宿主机依赖和前端构建；完整应用镜像构建、Windows Docker Desktop 与供应商付费调用仍未实测。

## 6. 默认模型凭据与 M5 本地替代

### 默认配置实际需要的 Key

宿主机开发在根目录 `.env.local` 填写；完整容器部署在 `.env.docker` 填写。
第三方凭据名称保持供应商名称，不属于项目命名空间：

| 变量 | 当前模型角色 | 缺失时的影响 |
| --- | --- | --- |
| `ARK_API_KEY` | Supervisor、转写、视觉理解、OCR、摘要、文本/图片/视频向量，以及 Agent fallback | 默认配置无法完成解析和向量检索；当前启动器会在启动应用前拒绝缺失的必需凭据 |
| `DEEPSEEK_API_KEY` | 检索 Agent、重排、视觉 fallback | 默认配置无法运行这些角色；当前启动器同样会拒绝缺失凭据 |

`KIMI_API_KEY`、`ANTHROPIC_API_KEY`、查询改写相关凭据均不被当前默认配置选中，无须填写。
`ARK_API_KEY` 需要在方舟账户中有对应模型权限；有 Key 不代表 `config.yaml` 中所有模型 ID 都已开通。
初始化 MySQL/ES、前端编译和离线单元测试不需要模型 Key。两项 Key 的非空检查不能代替真实模型验收。

官方入口：[方舟 Key 管理](https://docs.volcengine.com/docs/ark/manage-api-keys?lang=zh)、
[方舟模型开通与首次调用](https://docs.volcengine.com/docs/ark/quick-start?lang=zh)、
[DeepSeek API 鉴权](https://api-docs.deepseek.com/api/deepseek-api/)。

### 只有一个 Ark Key 的配置方案

可以在现有 `config.yaml` 中把 `models.agent`、`models.rerank`、`models.vision_fallback`
的 `source` 改为 `ark_default`，`model` 改为账户可用的豆包生成模型；当前配置中的
`doubao-seed-2-0-lite-260428` 是候选值。移除 `models.agent.fallback` 中重复的同来源 fallback。
保留其他角色与预算字段，并核对所有仍选中的 fallback。

2026-10-28 已在内存中使用上述调整通过配置校验，`required_api_key_env_names` 仅返回
`ARK_API_KEY`。主配置仍保留原供应商组合，未替用户切换模型，也未执行付费探测。
这是可减少供应商凭据数量的方案；实际质量、延迟与模型权限仍需验证。

### 零云端 Key 的本地模型路线

本次机器实测硬件为 Apple M5、16 GiB 统一内存。以下是候选组合，不代表已安装或已通过端到端模型验收。

| 职责 | 本地候选 | 当前接入边界 |
| --- | --- | --- |
| 对话、工具调用、视觉/OCR、摘要、生成式重排 | Qwen3-VL-4B-Instruct 的 4-bit MLX 版本，使用 MLX-VLM；文本角色也可用独立小型文本模型 | 需要验证工具调用和结构化输出；当前 `openai` / `openai_vision` 来源只允许 HTTPS，本机 HTTP 需要有边界的 loopback 支持或本机可信 HTTPS |
| 语音转写 | 多语言 Whisper small / medium，使用 whisper.cpp 的 Metal 支持 | 项目已有 `openai_audio`，允许 loopback HTTP；服务端需要提供匹配的 Audio Transcriptions 路径和带时间戳输出，原生 whisper.cpp HTTP 接口不能未经核对就当作完全兼容 |
| 文本、图片、视频向量 | Qwen3-VL-Embedding-2B，评估 MLX-VLM 的多模态 embedding 服务 | 当前 `gateway/embedding.py` 只实现 Ark，需要新增本地 adapter、对齐预处理/编码器身份；即使仍取 1024 维，也必须重新编码并建立新索引 |

依据：[Qwen3-VL](https://huggingface.co/Qwen/Qwen3-VL-4B-Instruct)、
[Qwen3-VL-Embedding](https://huggingface.co/Qwen/Qwen3-VL-Embedding-2B)、
[MLX-VLM](https://github.com/Blaizzy/mlx-vlm)、
[whisper.cpp](https://github.com/ggml-org/whisper.cpp)。

16 GiB 同时承载系统、IDE、数据库、VLM、转写和向量模型会有内存压力。建议先从小模型、
4K–8K 上下文、较少视频帧和串行处理开始，按阶段加载/卸载模型；不要沿用云模型的长上下文和高并发预算。
这些是待本机压测的起点，不能推断为相同云端质量或实时处理速度。
本地模型不需要云供应商 Key；个别兼容 SDK 的非空 `api_key` 字段可能使用本机占位值，但不能把占位值填进当前云模型配置来绕过校验。

## 7. Seekino 命名空间切换（2026-10-28）

项目包名、环境变量、容器项目、默认数据库/索引、Cookie、浏览器存储、Agent 内部标识与文件名已统一使用 Seekino。
环境变量使用 `SEEKINO_*`；默认开发库为 `seekino_development`，默认 Chunk 索引为
`seekino_video_chunks_development`，Compose 项目为 `seekino-local`。

这是一次完整命名空间切换，不提供旧配置前缀的透明兼容：

- 两份本机 `.env.local` 已同步改名，供应商密钥值不改动。
- Series、Video、Chunk、ContextSnapshot 和 ParseJob 的 UUID5 输入命名空间已切换；已有实体不会自动获得新的 ID。
- ES 索引合同版本已更新；旧索引不能直接视为兼容的新索引。
- 旧 Cookie、主题、语言、播放记录和会话缓存不会自动导入新浏览器命名空间；既有用户可能需要重新登录和设置偏好。
- 本机切换前已核对：业务表为空，只有 checkpoint schema 的迁移记录；搜索索引为 0 文档。
  因此本机使用新的空开发库/索引，并保留切换前 SQL 与索引定义备份。
- 有真实业务数据的其他部署必须先制定数据库外键、确定性 ID、素材引用、会话和 ES 重建迁移方案，不能仅替换名称或直接重建。

本轮已验证宿主机前端构建、新数据库容器健康和初始化；完整应用镜像构建、实际供应商请求与本地模型效果仍需各自验收。

### 本机验证结果与尚存问题

2026-10-28 命名空间改名后：`uv sync --locked --offline`、前端生产构建通过；
前端 551 项测试通过；真实 MySQL/ES/Checkpoint 相关测试 51 通过、1 跳过。
后端非 live-agent 全量测试为 4960 通过、107 跳过、1 未选中、9 失败。
随后在改名前源码快照上复跑全部 9 个失败用例，9 个均复现，因此不能归因为本次改名，
也不能宣称后端测试全部通过。测试使用临时 NumPy/SciPy 依赖层，未改项目依赖声明。

现存失败包括：测试引用已不存在的 `engine.common.model_factory`；Langfuse 接入头断言；
3 个模型默认配置/独立 Supervisor 断言；1 个多模态 fallback 上下文预算用例；
3 个真实视频处理用例。视频失败已确认是 FFmpeg 9 不再接受代码使用的
`-filter_script:v` 参数，后续完成视频端到端验收前需要修复此兼容性问题。

`./start.sh --branch dev` 已完成数据库和索引检查，但因两个默认供应商 Key 为空，
按预期停止在 API/Web 启动前；付费模型调用与完整视频流程未验收。
新命名 MySQL 和 Elasticsearch 容器健康。旧容器停止后，旧数据卷和网络应继续保留；
确认备份可恢复且新实例验证完成后，再显式清理旧资源。不要用删除卷作为常规启动步骤。
