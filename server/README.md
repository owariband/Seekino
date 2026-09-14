# AniVAG Server

Server 当前实现 M1 的事实源与 Vector Index Manager：MySQL 业务数据、正式业务 ID、
`pipeline.parse.json` 过渡导入、持久化 Index Sync Intent、Qdrant 派生索引，以及按
`videoChunkId[]` 批量回源。它尚不启动 Web API 或常驻 Worker，也不负责模型推理。

Qdrant 的连接、写入和恢复属于产品持久化逻辑，因此放在 `server/storage/`；Engine
只负责生成解析结果、向量与检索算法。Qdrant payload 不保存业务正文；当前
Embedding 的 durable source 是 MySQL，Qdrant 始终只是可重建派生索引。

## 数据库隔离

开发、测试和正式环境必须使用不同的数据库。CLI 会强制检查数据库名：

- `development`：数据库名以 `_dev` 或 `_development` 结尾；
- `test`：数据库名以 `_test` 结尾；
- `production`：拒绝上述非正式环境后缀；
- SQLite 只允许用于隔离测试。

Qdrant Collection 使用相同的物理隔离规则：测试以 `_test` 结尾，开发以 `_dev` 或
`_development` 结尾，正式环境拒绝这两类后缀。

创建开发库后初始化表结构：

```sql
CREATE DATABASE anivag_development
  CHARACTER SET utf8mb4
  COLLATE utf8mb4_0900_ai_ci;
```

```bash
export ANIVAG_ENVIRONMENT=development
export ANIVAG_DATABASE_URL='mysql+pymysql://anivag:password@127.0.0.1:3306/anivag_development?charset=utf8mb4'
export ANIVAG_QDRANT_URL='http://127.0.0.1:6333'
export ANIVAG_QDRANT_COLLECTION='anivag_video_chunks_development'
export ANIVAG_TEXT_VECTOR_DIMENSIONS=1024
export ANIVAG_VISION_VECTOR_DIMENSIONS=1024

uv sync
uv run python -m server.cli init-db
```

上述 `init-db` 只负责初始化空库或校验已经使用新命名的库。若数据库仍包含旧
`video_segment*` 表，或 `context_snapshot/index_sync_intent` 仍使用旧列名，Server 会在
创建任何新表前抛出 `SchemaUpgradeRequiredError`，防止形成新旧 Schema 混合状态。本次
术语重构没有自动修改现有 MySQL；旧开发库必须先受控重命名表、列、外键和约束并核对
原有 24 行数据，再运行新代码。

当前 Schema 包含六个领域模型：`Series`、`Video`、
`VideoChunk`、`VideoChunkInfo`、`ContextSnapshot`、
`VideoChunkEmbedding`，以及一个工程一致性模型 `IndexSyncIntent`，没有 `Episode`
表。所有 MySQL 表使用 InnoDB。

## 导入第七集已有产物

`import-parse` 是上传系统完成前的过渡入口。原视频、AnalysisWindow 和规范 VideoChunk
必须真实存在；导入器会把媒体保存为稳定的 `file:` URI。省略显式 ID 时，
Series ID 由 workspace 与规范化名称生成，Video ID 由 Series ID 与视频内容哈希生成，
VideoChunk ID 再由 Video ID 与序号生成稳定、不可反解关系的 UUID。`episodeOrder` 不参与
Video 身份计算，调整集序不会产生另一个业务 ID。

```bash
uv run python -m server.cli import-parse \
  --parse-result outputs/engine/ep07-full-60s/pipeline.parse.json \
  --canonical-chunk-dir outputs/engine/ep07-recall-smoke/eval-only-canonical-media/bfa4a91901efd2b7-h720 \
  --embedding-cache-dir outputs/engine/ep07-recall-smoke/embedding-cache/segments \
  --series-name 'BanG Dream! It’s MyGO!!!!!' \
  --series-description 'MyGO 动画测试数据' \
  --episode-order 7 \
  --pipeline-version 50661d1 \
  --series-bible-file assets/mygo_background_referrence/characters.md
```

整个 Video 快照在一个事务中写入。相同命令重复执行会复用相同的 `videoId` 与
`videoChunkId` 并更新解析器拥有的数据，不会增加记录；并发首导入遇到唯一键竞争时
会回滚后重试。重导不会清空已有的 Series/Video 管理元数据、Canonical VideoChunk
媒体引用或下游 enrichment，也不会用改变后的文本继续配用旧 Text Embedding。
同一 ID 对应到另一源视频或另一套 VideoChunk 边界时会明确报冲突。

上例的 `embedding-cache/segments` 是现有第七集历史缓存目录；导入器只把其中的
`segment-NNNN.*.json` 当兼容输入读取，新领域模型与新产物统一使用 VideoChunk/chunk。

这里的 Artifact Store 规划只负责原视频、Proxy、VideoChunk、AnalysisWindow、关键帧和
Clip 等媒体或二进制产物；它尚未实现，也不与 Embedding 外置绑定。当前 text/vision
向量持久化在 MySQL `VideoChunkEmbedding`，未来是否外置及外置到哪类存储是独立决策。

事实与对应的 `IndexSyncIntent` 在同一个 MySQL 事务中提交。双向量完整时意图为
`UPSERT`；向量不完整或新事实要求清除旧索引时为 `DELETE`。导入不直接访问 Qdrant，
因此 Qdrant 故障不会回滚已经提交的业务事实。

## 索引同步与恢复

启动本地 Qdrant 后消费当前到期的 Intent：

```bash
docker compose -f deploy/qdrant/compose.yaml up -d
uv run python -m server.cli sync-index --limit 100
```

正式 point ID 直接使用稳定的 `videoChunkId` UUID；Collection 只包含 `text`、
`vision` 两个 named vector，payload 只包含 `seriesId`、`videoId`、
`videoChunkId`、`sourceRevision`、`isFallback`。相同意图重复执行是覆盖写，不会增加
point。两个向量维度是启动时明确给定的 Collection Schema；任意首条异常向量都不能
反向决定或污染正式 Collection 的维度。

写入失败时 Intent 进入 `RETRY_WAIT`，保存错误、尝试次数和指数退避后的下次执行时间；
再次运行 `sync-index` 会继续消费已到期任务。校准命令检查每个当前 revision，发现缺失、
过期或应删除但仍存在的 point 后立即走同一幂等路径修复：

```bash
uv run python -m server.cli reconcile-index
```

如果 Collection 被清空或丢失，使用全量重放恢复。该命令不会主动删除 Collection，
只以 MySQL `VideoChunkEmbedding` 中的当前向量事实为准重放全部期望状态：

```bash
uv run python -m server.cli rebuild-index
```

只有一个 Series 的全部 VideoChunk 都具备 text/vision 向量，并且最新 UPSERT Intent 均已
确认同步，Series 才进入 `READY`；其他情况保持 `INDEXING`。

批量回源接受一个或多个正式 ID，并通过一条联表 SQL 返回 Series、Video、VideoChunk 和
解析信息。返回字段使用工程 API 风格的 `videoCompact` 与 `isFallback`，不返回原始向量：

```bash
uv run python -m server.cli hydrate \
  00000000-0000-0000-0000-000000000000 \
  11111111-1111-1111-1111-111111111111
```
