# 可选：Docker 数据库 + 本地应用

根目录 `compose.yaml` 默认运行 MySQL 8.4 和 Qdrant 1.19.1；
Elasticsearch 8.19.22 在 `elasticsearch` profile 下显式启用。
Python Server（含 Parse Consumer）、Vite、ffmpeg、模型 SDK 在宿主机运行。
无需构建应用镜像，修改代码后按原来的方式调试。
`./start.sh` 的默认 Qdrant 流程仍不依赖 Docker；只有主动执行 Compose 命令才使用
这套容器。显式配置 ES 时，脚本会额外验证 ES 集群和索引映射。

## 首次运行

1. 安装并启动 Docker Desktop，或 Docker Engine + Compose（需支持 `up --wait`）。
2. 本机安装 uv、Node 24+、npm、ffmpeg / ffprobe。
3. 在仓库根目录执行 `cp .env.example .env.local`（已有文件则保留），填写模型密钥。
   默认模型配置需要 `ARK_API_KEY` 和 `DEEPSEEK_API_KEY`；模型与 Endpoint 仍按 `config.yaml` 配置。
4. 执行 `docker compose up -d --wait && ./start.sh`，访问 `http://127.0.0.1:5173`。

Compose 先启动数据库并等待健康检查通过；原脚本随后复用已运行的数据库，
创建/校验 MySQL 表和 Qdrant 集合，再启动本地应用。
`&&` 保证容器启动失败时不会继续运行原脚本。
首次下载镜像或 Python 依赖需要网络；容器启动成功不表示模型密钥和视频解析已验证。

只准备基础设施时，无需模型密钥或本地 Python 环境：

```bash
docker compose up -d --wait
docker compose ps
docker compose logs -f mysql qdrant
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
| Qdrant 索引 | Docker volume `anivag-local_qdrant_data` |
| Qdrant 快照 | Docker volume `anivag-local_qdrant_snapshots` |
| 可选 ES 索引 | Docker volume `anivag-local_elasticsearch_data` |

准备 ES 接入测试时，先运行 `docker compose --profile elasticsearch up -d --wait elasticsearch`，
再在 `.env.local` 配置 `ANIVAG_ELASTICSEARCH_URL` 和 `ANIVAG_ELASTICSEARCH_INDEX`。
显式配置后，`./start.sh` 会检查 ES 集群为 yellow/green 并校验索引 Schema；脚本不会
自建原生 ES 进程。默认 `ANIVAG_PARSE_EXECUTION_MODE=v1`，API/Chat 检索和 V1 索引维护
走 Qdrant。显式切到 `v2` 后，新上传任务由 DB Queue 的 V2 Worker 执行，并将通过
Interval 放行的 Chunk 写入 ES；API、Agent Chat 和 CLI search 同时选择 V2 ES 检索，
不会回退到 Qdrant。启用前还需应用 Server README 列出的 V2 MySQL 迁移。容器就绪
不会迁移历史数据或执行索引重建。隔离集成测试须另用
`ANIVAG_TEST_ELASTICSEARCH_URL` 和独立测试索引。

备份/迁移时需同时保留 MySQL 业务事实和宿主机媒体。不要仅复制向量索引作为完整备份。
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
`start.sh stop` 仍会停止它自己管理的本机进程，包括由它启动的原生数据库；
不会停止 Compose 容器或其它程序启动的数据库。
如果只运行 `./start.sh` 而容器未运行，原脚本仍会尝试启动本机 MySQL / Qdrant。

## 自定义端口

Compose 不解析应用连接 URL，也不会自动读取 `.env.local`。自定义端口时，
请在 `.env.local` 中同时配置容器发布端口和应用连接 URL，并显式传入 `--env-file`：

```dotenv
ANIVAG_DATABASE_URL=mysql+pymysql://root@127.0.0.1:13306/anivag_development?charset=utf8mb4
ANIVAG_MYSQL_PORT=13306
ANIVAG_QDRANT_URL=http://127.0.0.1:16333
ANIVAG_QDRANT_HTTP_PORT=16333
ANIVAG_QDRANT_GRPC_PORT=16334
```

```bash
docker compose --env-file .env.local up -d --wait && ./start.sh
# 后续管理也使用同一份配置：
docker compose --env-file .env.local stop
```

不要把宿主机连接 URL 改成 `mysql` / `qdrant` 服务名；本地 Python 使用宿主机端口访问它们。
此 Compose 使用固定项目名 `anivag-local`，多个 checkout 默认共享同一组开发卷。
需要独立环境时，设置不同的 `COMPOSE_PROJECT_NAME` 和端口，并在所有启停命令中保持一致。

## 已有本地数据

原有 `start.sh` 的原生 MySQL / Qdrant 数据位于 `outputs/local-services/`；
旧 `deploy/qdrant/compose.yaml` 也使用自己的卷。新 Compose **不自动迁移这些数据**。

已有服务占用配置端口时，`start.sh` 会验证并复用它，不强制替换。
直接 `docker compose up` 遇到端口冲突则会失败。若要迁移，先备份旧 MySQL 与媒体，
使用 MySQL 逻辑导出/导入和 Qdrant 快照恢复或项目索引同步流程；不要把原生数据库目录直接覆盖进新卷。
仅停止旧服务后启动新容器会看到一个新的空库，旧目录仍保留。

使用已有的带密码 MySQL 或带 API Key / HTTPS 的 Qdrant 时，先自行启动对应实例，
再通过 `.env.local` 配置连接信息；脚本不会将这些凭证改写成开发容器的默认设置。
