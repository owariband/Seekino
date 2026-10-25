# 部署与本地运行

首次安装推荐阅读[本地配置指南：API 模型 + Docker 数据库](local-setup.md)，包含当前 dev 的模型示例、checkpoint 初始化、日常启动和 Docker 覆盖范围。

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
   根模型配置还依赖本机 Codex、Whisper 和可选模型服务；首次试用可选上面指南的 API 模型配置。
4. 执行 `docker compose --env-file .env.local up -d --wait mysql elasticsearch`。
5. 依次执行 `uv run --env-file .env.local python -m server.cli init-db` 和
   `uv run --no-sync --env-file .env.local python -m server.cli init-es-index`，前者同时初始化 DeepAgents checkpoint 表。
6. 在当前 dev 工作区执行 `./start.sh --branch dev`；main 工作区使用 `./start.sh`，访问 `http://127.0.0.1:5173`。

Compose 先启动数据库并等待健康检查通过；原脚本随后复用数据库，
校验 MySQL 业务表和 ES 索引，再启动本地应用；脚本本身不初始化 checkpoint 表。
按顺序执行，前一步失败时先修复，再继续下一步。
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

<a id="local-assets"></a>

## 本机资产与换机恢复

本节供开发者和 AI 恢复现有环境时使用。下面的路径、模型绑定和工作区状态来自
**2026-10-23 本地 main 工作区盘点**，不是新安装的默认值；执行前重新核对实际环境。
`outputs/`、`design/`、`experiment/` 被 Git 忽略，不代表其中内容可重新生成或可直接删除。

### 先确定运行依据

1. 读取 `.env.local` 中的配置变量名与本地路径，不输出密钥值。优先核对
   `ANIVAG_MODEL_CONFIG`、`ANIVAG_ARTIFACT_ROOT`、数据库 URL、ES 索引及代际；
   不用根目录 `config.yaml` 或 `.env.example` 覆盖现有运行配置。
2. 本次 `ANIVAG_MODEL_CONFIG` 指向 `outputs/local-services/main-model-config.yaml`，
   其中模型绑定和 Agent 预算与根配置不同。该文件与 `.env.local` 必须配套恢复。
3. 主业务库为 `anivag_local_20261010_development`，媒体根为
   `outputs/server/local-20261010/artifacts`，其 `development/` 子目录也必须保留。
   手机测试另用 `outputs/local-services/cloud-auth/phone.env`、同目录 `model-config.yaml`、
   `seekino_phone_dev` 和 `outputs/phone-test/artifacts`；仅在需要恢复手机测试环境时携带这一套。
4. 检查 `git status`，保存启动脚本和当前功能所依赖的未提交/未跟踪源码。
   本次 `scripts/local_whisper.py`、`evals/benchmark/intent_routing/` 等尚未进入已有提交；
   单独 clone main 不能恢复完整的本机工作状态。
5. `./start.sh` 固定定位 main，且不会启动下面的 Whisper 或查询改写模型服务。
   API `/healthz` 正常只证明 API/Consumer 状态，不能作为模型能力恢复完成的证据。

### 当前配置需要的本地模型文件

三份权重合计约 **6.22 GiB**；整个模型目录的约 44.9 GiB 不是当前运行的最小要求。
权重可通过备份恢复，或按原版本、来源和校验值重新取得；Mac 可执行文件须在 Windows/WSL
重新安装或构建。

| 能力 | 必需文件与关联依赖 | 恢复依据 |
|---|---|---|
| 查询改写 | `outputs/models/Qwen3-8B-GGUF-local/b968826d9c46dd6066d109eabc6255188de91218/Qwen3-8B-Q4_K_M.gguf`，约 4.68 GiB；匹配的 `llama-server` | 同目录 `manifest.json` 记录官方 Qwen/Qwen3-8B revision、转换器版本、量化参数及 SHA-256；详见 [查询改写实验记录](../evals/benchmark/intent_routing/README.md) |
| ASR | `outputs/local-models/whisper/ggml-large-v3-turbo.bin`，约 1.51 GiB；`outputs/tools/whisper.cpp/build/bin/whisper-server` | 本次 whisper.cpp 源码为 `48f628a`；保留版本，按其 `models/download-ggml-model.sh` 获取 `large-v3-turbo`，用 [启动脚本](../scripts/local_whisper.py) 读取当前模型绑定 |
| 声纹 | `outputs/local-models/campplus/campplus_cn_common.bin`，约 27 MiB；`outputs/local-models/campplus/3D-Speaker` 中的 `speakerlab` 和 `speaker` 可选依赖（extra） | [CAM++ 适配器](../gateway/local/campplus.py) 校验权重和源码 SHA；恢复相同实现，修正 `PYTHONPATH` 与 YAML 中的 `checkpoint_path`，不能只复制权重 |

查询改写的本机启动记录在 `outputs/local-services/query-hook/runtime.json`。
恢复时使用其中的 command 语义，不复用旧 PID 和 `/opt/homebrew`、`/Users/...` 路径：
端口 `18841`、alias `seekino-qwen3-text-8b-rewriter`、context `8192`、parallel `1`、
threads `4`、seed `42`、`enable_thinking=false`；GPU offload 参数按新机器后端设置。
Jev 还需要 `.env.local` 中的 `OPENROUTER_JEV_API_KEY`。

Whisper 使用当前配置的 `http://127.0.0.1:18842/v1`，可在依赖与二进制准备好后运行：

```bash
uv run --no-sync --env-file .env.local python scripts/local_whisper.py
```

`WHISPER_API_KEY` 是本机 OpenAI 兼容客户端的占位配置，不代表服务启用了认证。
另需恢复当前模型来源要求的 Ark、DeepSeek 凭据和本地 Codex 登录；配置中的变量名不能替代真实凭据。

以下模型**不被本次 main 的有效模型配置引用**，不属于恢复当前服务的必需文件：
`Qwen3-4B-GGUF`、`Qwen3-0.6B-GGUF`、Qwen3-8B 原始 BF16 副本、
`ggml-large-v3.bin`、WavLM、ReDimNet、ERes2Net、DeepSeek-OCR-2，以及
`deploy/llama_cpp/models/` 下的 Qwen3-VL 主模型与 projector。
保留正在进行的实验、转换或训练所需的版本；其他副本可记录来源后按需重新下载。
[llama.cpp direct-video 入口](llama_cpp/README.md) 是历史 A/B 实验，不能用它替代 `18841` 的纯文本改写服务。
自行训练或合并的权重另查 `outputs/training/`，不能按公开下载缓存处理。

### 大目录的保留与舍弃条件

| 目录/资产 | 当前运行是否依赖 | 迁移与整理规则 |
|---|---|---|
| MySQL 业务数据 | 是，保存资料库、任务、解析成果及应用对话等 | 做一致性逻辑导出并验证恢复，与媒体配套保存。不要只复制部分 `.ibd` 或把运行中的数据目录作为唯一备份 |
| MySQL binlog | 用于复制、CDC、按时间点恢复；不是完整逻辑备份恢复到其快照状态的必需附件 | 本次约 67.15 GiB。只有确认复制/CDC/PITR 保留需求、完整备份和恢复验证后，才能决定不携带哪些历史日志；清理走 MySQL 管理命令，不手动删除文件 |
| 实际 `ANIVAG_ARTIFACT_ROOT` | 是 | 保留完整媒体、切片、帧图、对话附件及环境子目录。数据库使用 `artifact://development/...` 相对 URI；新机可以调整根路径，但必须保持内部结构和数据库对应关系 |
| `assets/example_video_mygo/` 与手机测试媒体 | 主服务依赖已发布媒体；原始样例还被部分评测引用 | 手机环境可整套选择不恢复。原始样例只有在确认业务副本完整、哈希一致且所需评测不再引用后才可省略；不能仅凭文件名判断重复 |
| `outputs/verification/` | 不是生产启动依赖，但包含验收证据及评测辅助输入 | 保留报告、协议、配置、真值/用例、来源版本、关键结果和代表性失败证据。逐实验确认其余大日志、重复帧和中间结果可以放弃；重跑可能需要模型费用，结果也未必相同 |
| `outputs/worktrees/` | main 启动不依赖这些实验工作区 | 先保存 base commit、tracked diff（含二进制）、untracked 文件和必要的 ignored 文件，核对没有运行进程引用后才能移除 worktree。单独保存 Git patch 不包含未跟踪文件 |
| `engine/experiment/` | 不是生产启动依赖，保存实验脚本、数据与原始结果 | 保留 README、脚本、参数、模型来源、输入标识/哈希、金标与关键原始输出。可重建的向量、重复图片和全量输出是否省略，按保留的复现目标决定 |
| `outputs/model-traces/` | [诊断模块](../gateway/model_diagnostics.py) 写入，不是正式解析/检索的恢复数据源 | 保留未解决问题或已引用结论对应的 call/task 轨迹，其余可按保留期归档或舍弃；轨迹含输入输出和媒体，按私有资料处理 |

工作区盘点发现四个 `outputs/worktrees/` 子目录均有未提交改动：

| 工作区 | 已跟踪改动文件 | 未跟踪文件 |
|---|---:|---:|
| `ep04-continuous-turn-20261021` | 25 | 4 |
| `ep04-continuous-turn-20261021-v23` | 25 | 4 |
| `ep04-offscreen-20261022` | 25 | 6 |
| `ep04-segmentation-20261020` | 34 | 10 |

它们不是可直接删除的代码副本。约 5.5 GiB 总占用主要来自 segmentation 工作区内的
`outputs/`（约 5.4 GiB），其内容也需单独盘点，不能随 worktree 一起丢弃。
验收记录中，EP04 v24 的 `report.md`、v25/offscreen 的 `report.md` 是语义效果演进证据；
`outputs/verification/speaker-agreement-20261018/v11-lines.jsonl` 还被声纹算法评测用于可选历史一致性核对。

### 给恢复环境的 AI 的交付要求

- 每个必需资产注明：用途、配置引用、源路径、新机目标路径、来源/revision、已有校验值或待补校验值、
  获取/构建方式、验证步骤。仅写“下载一个 Qwen/Whisper 模型”不足以重建当前行为。
- Windows 优先在 WSL2 中恢复现有 Bash/POSIX 启动链。Python、Node 依赖按锁文件重装，
  CAM++ 需 `speaker` 可选依赖（extra）；本机二进制和 GPU 后端重新准备，修正绝对路径。
- MySQL 和 Elasticsearch 可用 Compose 恢复；ES 备份采用 snapshot，或从保留的业务事实和媒体重建并验证索引合同。
  `ANIVAG_MYSQL_BUFFER_POOL_SIZE=8G` 当前只由原生启动器消费；Compose 需显式设置对应 mysqld 参数，
  并根据新机内存调整。不要把 `.env.local` 中出现该变量等同于容器配置已生效。
- 先核对资产和配置，再验证数据库、媒体可读性、索引及模型服务身份；模型调用验收与 API 健康检查分别记录。
  验收记录包括一次已解析视频播放、已有资料检索，以及在获准调用模型后的一次 ASR/改写/声纹检查。
- 密钥配置和私有数据单独加密备份；Git 中保存脱敏模板、恢复说明和可公开的源码/文档。
  `outputs/migration/20261023/manifest.json` 记录的三个 ZIP 只覆盖部分设计资料、实验报告和训练数据，
  不含完整数据库、主要媒体、模型权重及上述工作区修改，不能作为整机迁移完成的依据。

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
