# 可选：Docker 数据库 + 本地应用

根目录 `compose.yaml` 默认运行 MySQL 8.4 和 Elasticsearch 8.19.22。
Python Server（含 V2 Parse Consumer）、Vite、ffmpeg、模型 SDK 在宿主机运行。
无需构建应用镜像，修改代码后按原来的方式调试。
`./start.sh` 会复用已有 MySQL、校验 ES 集群和索引映射；ES 必须事先准备，
脚本不会启动原生 ES 进程。正式流程只使用 V2，不需要 Qdrant。

## 首次运行

1. 安装并启动 Docker Desktop，或 Docker Engine + Compose（需支持 `up --wait`）。
2. 本机安装 uv、Node 24+、npm、ffmpeg / ffprobe。
3. 在仓库根目录执行 `cp .env.example .env.local`（已有文件则保留），填写模型密钥，
   并显式配置 `ANIVAG_ELASTICSEARCH_URL` 和 `ANIVAG_ELASTICSEARCH_INDEX`。
   默认模型配置需要 `ARK_API_KEY` 和 `DEEPSEEK_API_KEY`；模型与 Endpoint 按 `config.yaml` 配置。
4. 执行 `docker compose up -d --wait && ./start.sh`，访问 `http://127.0.0.1:5173`。

Compose 先启动数据库并等待健康检查通过；原脚本随后复用数据库，
创建/校验 MySQL 表和 ES 索引，再启动本地应用。
`&&` 保证容器启动失败时不会继续运行原脚本。
首次下载镜像或 Python 依赖需要网络；容器启动成功不表示模型密钥和视频解析已验证。

只准备基础设施时，无需模型密钥或本地 Python 环境：

```bash
docker compose up -d --wait
docker compose ps
docker compose logs -f mysql elasticsearch
```

## 视频测试

正常从 Web 选择并上传本机视频，不需要把样例视频挂载到数据库容器。
任务页查看解析进度；完成后在搜索页提问并播放返回的时间证据。
ffmpeg 和模型调用在宿主机执行，原有 CLI、断点调试和模型登录方式继续使用。

| 内容 | 位置 |
|---|---|
| 视频、切片、关键帧、解析文件 | 本机 `ANIVAG_ARTIFACT_ROOT`，默认 `outputs/server/artifacts` |
| 本机进程 PID、启动日志 | `outputs/local-services/` |
| 应用结构化日志 | `ANIVAG_LOG_DIRECTORY`，默认 `outputs/server/logs` |
| MySQL | Docker volume `anivag-local_mysql_data` |
| ES 索引 | Docker volume `anivag-local_elasticsearch_data` |

`ANIVAG_PARSE_EXECUTION_MODE` 只接受 `v2`。新上传任务由 DB Queue 的 V2 Worker 执行，
Refine 成果提交后进入索引阶段，ES 写入与验证成功后完成任务；API、Agent Chat 和
CLI search 使用同一套 V2 检索。`./start.sh` 要求 ES 集群为 yellow/green，并校验索引 Schema。
已有数据库升级步骤见 [Server README](../server/README.md)。容器就绪不会迁移历史数据
或执行索引重建。隔离集成测试须另用 `ANIVAG_TEST_DATABASE_URL`、
`ANIVAG_TEST_ELASTICSEARCH_URL` 和独立测试索引。

备份/迁移时需同时保留 MySQL 业务事实和宿主机媒体。ES 可从当前 V2 成果重建，
仅复制索引不能作为完整备份。
容器没有代码或模型密钥挂载；数据库端口仅绑定 `127.0.0.1`。
MySQL 的 root 空密码与当前单机开发默认值一致，这份 Compose 用于本地开发。

## 停止、重启和日志

```bash
./start.sh stop          # 停止脚本管理的本机应用；保留数据库容器
docker compose stop     # 需要时再停止数据库，数据保留
docker compose up -d --wait && ./start.sh  # 再次选择 Docker 模式，复用数据库卷
```

`docker compose down` 删除容器和网络但保留 named volumes。
不要在需要保留视频库时执行 `docker compose down -v`，它会删除数据库卷。
`start.sh stop` 会停止它自己管理的本机进程，包括由它启动的原生 MySQL；
不会停止 Compose 容器或其它程序启动的数据库。
如果只运行 `./start.sh` 而容器未运行，脚本会尝试启动本机 MySQL；ES 不可达时启动失败。

## 自定义端口

Compose 不解析应用连接 URL，也不会自动读取 `.env.local`。自定义端口时，
请在 `.env.local` 中同时配置容器发布端口和应用连接 URL，并显式传入 `--env-file`：

```dotenv
ANIVAG_DATABASE_URL=mysql+pymysql://root@127.0.0.1:13306/anivag_development?charset=utf8mb4
ANIVAG_MYSQL_PORT=13306
ANIVAG_ELASTICSEARCH_URL=http://127.0.0.1:19200
ANIVAG_ELASTICSEARCH_PORT=19200
ANIVAG_ELASTICSEARCH_INDEX=anivag_video_chunks_development
```

```bash
docker compose --env-file .env.local up -d --wait && ./start.sh
# 后续管理也使用同一份配置：
docker compose --env-file .env.local stop
```

宿主机 Python 使用 `127.0.0.1` 发布端口访问 MySQL 和 ES，连接 URL 不使用 Compose 服务名。
此 Compose 使用固定项目名 `anivag-local`，多个 checkout 默认共享同一组开发卷。
需要独立环境时，设置不同的 `COMPOSE_PROJECT_NAME` 和端口，并在所有启停命令中保持一致。

## 已有本地数据

原有原生 MySQL 数据位于 `outputs/local-services/`；历史 Qdrant 目录和 Docker 卷保留，
本次配置清理不会删除或迁移它们。新 Compose 不自动迁移已有数据。

已有服务占用配置端口时，`start.sh` 会验证并复用它，不强制替换。
直接 `docker compose up` 遇到端口冲突则会失败。若要迁移，先备份旧 MySQL 与媒体，
通过 MySQL 逻辑导出/导入恢复业务事实，再按当前 V2 合同重建 ES；
不要把原生数据库目录直接覆盖进新卷。仅停止旧服务后启动新容器会看到一个新的空库，
旧目录仍保留。

使用已有的带密码 MySQL 或带 API Key / HTTPS 的 ES 时，先自行启动对应实例，
再通过 `.env.local` 配置连接信息；脚本不会改写这些凭证。
