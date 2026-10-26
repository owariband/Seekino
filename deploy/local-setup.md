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

不维护按供应商拆分的 YAML。继续使用现有 `model_sources`、`models`、`supervisor_harness` 和 `ANIVAG_MODEL_CONFIG`，完整字段见[模型配置](../docs/model-config.md)。

`.env.docker` 中的 `ANIVAG_MODEL_CONFIG=./config.yaml` 指向**首次导入的宿主机文件**，相对路径以仓库根目录为准，也可以选择自己的既有 YAML。初始化后，容器使用持久卷中的 `/data/config/config.yaml`，设置页修改也保存到这里。普通启动和镜像重建不会覆盖这份有效配置。

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

在 `.env.docker` 填写 `KIMI_API_KEY`。上述示例只切换 Supervisor；`models.agent`、`vision`、`ocr`、`summarize`、`rerank` 可以分别选择同一来源。现有 fallback 也要按自己的选择核对；转写和 embedding 保持各自专用模型。凭据要求取决于所有仍被选中的来源，不能只改 Supervisor 就删除其他角色需要的密钥。Kimi endpoint 和模型权限可对照 [Kimi 官方文档](https://www.kimi.com/code/docs/)。

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

如果修改了宿主机 YAML 或 `ANIVAG_MODEL_CONFIG`，明确重新导入，以下步骤会替换有效模型 YAML，但保留媒体和数据库：

```bash
docker compose --env-file .env.docker stop web api
docker compose --env-file .env.docker run --rm --no-deps initialize cp /app/model-template.yaml /data/config/config.yaml
docker compose --env-file .env.docker up -d --wait
```

重新导入会同时替换设置页保存的 Agent 参数。如需保留，先导出有效配置再编辑：

```bash
docker compose --env-file .env.docker cp api:/data/config/config.yaml ./config.saved.yaml
```

可将 `ANIVAG_MODEL_CONFIG` 指向这个文件。不要在已有素材库上随意更换 embedding 模型、来源或维度，索引与编码器身份必须匹配。

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
| 模型、全局 Agent 设置 | `anivag-local_app_data` 卷的 `/data/config/config.yaml` |
| 视频、切片、帧、附件 | 同一卷的 `/data/artifacts` |
| 应用日志 | 同一卷的 `/data/logs`；容器标准输出也可用 `compose logs` 查看 |
| Codex 登录与会话 | 同一卷的 `/data/codex` |
| 业务数据、DeepAgents checkpoint | `anivag-local_mysql_data` 卷 |
| ES 索引 | `anivag-local_elasticsearch_data` 卷 |

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

开发机器当前没有 Docker 引擎，尚未实测镜像构建、全新容器启动或 Windows Docker Desktop，也没有对默认供应商组合执行付费模型调用。运行环境和模型效果需在可用 Docker 主机上按第 2 节验收。
