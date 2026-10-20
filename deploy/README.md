# 部署与本地运行

已有原生 MySQL、Elasticsearch 和 Python 环境时，可以继续使用；HTTPS 入口不要求
Docker。macOS 的 Nginx 安装、本地证书、8443 入口与手机信任步骤见
[原生 Nginx 本地 HTTPS](nginx/README.md)。Nginx 独立于 Python API 和 Vite 运行。

## 可选：Docker 数据库 + 本地应用

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

## 本地模拟验证码邮件

使用可选的 [Mailpit](https://mailpit.axllent.org/docs/install/docker/) 收件箱测试邮箱登录，
无需域名、邮箱授权码或付费发信服务。Seekino 仍通过现有 SMTP 发送验证码；Mailpit
捕获邮件供本地查看，不投递到收件人的真实邮箱。验证码的随机生成、Redis 校验、
有效期和发送限制继续生效。

在 `.env.local` 中将现有 SMTP 配置改为：

```dotenv
ANIVAG_SMTP_HOST=127.0.0.1
ANIVAG_SMTP_PORT=1025
ANIVAG_SMTP_USERNAME=
ANIVAG_SMTP_PASSWORD=
ANIVAG_SMTP_SENDER=noreply@seekino.test
ANIVAG_SMTP_USE_SSL=false
ANIVAG_SMTP_START_TLS=false
```

应用运行在宿主机，因此使用 `127.0.0.1`，而不是容器服务名 `mailpit`。
SMTP 默认启用 STARTTLS，此处必须显式关闭，匹配 Mailpit 的本地无 TLS 接口。
若 1025 或 8025 被占用，可在 `.env.local` 设置 `ANIVAG_MAILPIT_SMTP_PORT` 或
`ANIVAG_MAILPIT_WEB_PORT`；更改 SMTP 发布端口时，同时更新 `ANIVAG_SMTP_PORT`。

启动本地验证码依赖：

```bash
docker compose --env-file .env.local --profile cloud-auth --profile mock-email up -d --wait redis mailpit
```

测试账号登录时，Server 需以 `ANIVAG_AUTH_MODE=cloud` 启动，并配齐至少 32 字节的
独立随机签名秘密及 Redis 连接。`ANIVAG_AUTH_QUERY_LEASE_SECONDS` 必须大于
`config.yaml` 的 `execution_timeout_seconds`（当前为 1200 秒，可为本次测试设租约
1800 秒）。使用普通 HTTP 的内网测试入口时设置 `ANIVAG_AUTH_COOKIE_SECURE=false`；
正式 HTTPS 入口使用 `true`。SMTP 配置在 Server 启动时读取。

在 Seekino 登录框填写测试邮箱（例如 `tester@example.com`），点击发送验证码；
在电脑打开 `http://127.0.0.1:8025` 查看邮件，再回到原浏览器输入验证码完成登录。
手机测试时也可在电脑查看验证码，并在手机申请验证码的同一个浏览器中完成验证。
Mailpit 的 SMTP 和收件箱端口都只开放给本机；`mock-email` profile 默认不启动。
邮件使用容器内临时存储，Mailpit 退出后清空。

只停止模拟收件箱：

```bash
docker compose --env-file .env.local --profile mock-email stop mailpit
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

## MySQL binlog

The binlog stays on: replicas, point-in-time recovery and any future CDC depend
on it. Both `compose.yaml` and the native MySQL started by `./start.sh`
(`scripts/local_services.py`) run with the same settings, so local runs show the
binlog growth production would see:

| Setting | Local value | Why |
|---|---|---|
| `binlog_expire_logs_seconds` | `604800` (7 days) | Caps disk use. In production, match the backup / PITR window instead. |
| `binlog_row_image` | `MINIMAL` | The before image holds only the primary key and the after image only the changed columns, so a timestamp-only update no longer copies a large `JSON` `detail`. A changed JSON column is still logged in full. `NOBLOB` is not enough here: it skips TEXT/BLOB but not `JSON` (see below). |
| `binlog_transaction_compression` | `ON` | zstd-compresses each transaction payload (MySQL ≥ 8.0.20); JSON-encoded vectors compress about 4×. |

Measured on MySQL 8.4.11 with one row holding ~176 KB of float-like text,
updating only a `BIGINT` timestamp (binlog bytes per UPDATE):

| Large column type | `FULL` | `FULL` + compression | `NOBLOB` | `NOBLOB` + compression | `MINIMAL` |
|---|---:|---:|---:|---:|---:|
| `LONGTEXT` | 352,384 | 80,222 | 315 | 272 | 303 |
| `JSON` | 352,453 | 80,249 | 352,453 | 80,249 | 298 |

So for this schema only `MINIMAL` stops unchanged JSON from being logged, and
compression is the only lever that works with `FULL`; both are enabled. The application avoids
needless large-JSON rewrites (batched Event cache saves; no `update_time`-only
task updates), which matters under every row image.

Production equivalents:

- Self-managed MySQL: put the same three keys under `[mysqld]` in `my.cnf`
  (`binlog_expire_logs_seconds`, `binlog_row_image = MINIMAL`,
  `binlog_transaction_compression = ON`). Both of the last two are dynamic, so
  `SET PERSIST` also works without a restart.
- Managed MySQL (RDS / Aurora / Cloud SQL / ApsaraDB, etc.): set them in the
  instance parameter group. Retention there is usually the provider's own setting
  (for example RDS `binlog retention hours`), not `binlog_expire_logs_seconds`.
- `MINIMAL` keeps replication and PITR working; every table here has a primary
  key, which `MINIMAL` row matching relies on (keep it that way for new tables).
- **If CDC is added, switch back to `binlog_row_image = FULL`.** Debezium and
  most CDC connectors require full row images, and so do binlog-based
  row-rollback / flashback tools. Keep compression on and rely on the
  application-side write limits to keep `FULL` affordable.

Check the live values with:

```bash
mysql -uroot -h127.0.0.1 -e "SHOW VARIABLES WHERE Variable_name IN ('log_bin','binlog_row_image','binlog_transaction_compression','binlog_expire_logs_seconds');"
```

Reclaim space from old logs (discards PITR history before that point):

```bash
mysql -uroot -h127.0.0.1 -e "PURGE BINARY LOGS BEFORE NOW() - INTERVAL 1 HOUR;"
```

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
