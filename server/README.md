# Seekino Server

API 为 Web 和外部工具提供上传、任务状态、检索和观看问答。启动后，在 Web 同源入口打开 `/api/docs` 查看接口、参数与响应。宿主机 API 的默认端口为 9621。用户范围字段为 `tenantId`，视频时间为相对原片起点的整数毫秒。

## 本机 MCP：供 Codex 等外部 Agent 调用

`SEEKINO_AUTH_MODE=local` 时，正常 API 进程同时提供 Streamable HTTP MCP：`http://127.0.0.1:9621/mcp`。若 API 端口不同，使用实际端口。无需单独启动 MCP 进程；Cloud 模式不提供这个入口。

| 工具 | 参数与结果 |
|---|---|
| `list_series` | `offset=0, limit=20`，分页列出本地系列与状态，`next_offset` 用于续页 |
| `retrieve` | `query` 必填，可选 `series_ids`、`top_k=5`、`event_types`、`speaker`；返回 Event 证据及 `evidence_ref`。使用相同条件和 `next_cursor` 继续读取排序结果 |
| `browse_events` | 指定 `series_id`，按集和时间翻完已发布事件。可用 `visible_names` 筛选出镜角色，自动展开明确的系列别名。单页 `limit` 为 1–50，传回 `next_cursor` 直到为空 |
| `read_evidence` | 接收 `evidence_ref`，重验当前任务/内容版本，返回事件、分页 Chunk 上下文与媒体 URL |

使用剪辑素材策略解析后，可以搜索“镜头向右横移，灯塔从左边出画，小船在右边入画”。命中的 `SCENE.camera_ref` 给出镜头方向、速度类别、起幅、落幅和画面依据。对这条事件调用 `read_evidence`，适合二维复现的运镜会附带 `camera_replay`，其中有版本化 ES 模块、入口 `cameraAt(elapsedMs, params, world, viewport)` 和原片时长。返回值是新画面中的裁取矩形，可直接用于 Canvas 的 `drawImage`。

`params.progress_samples` 存在时，横移曲线使用带原片时间的背景位移采样。每对数字分别是镜头时长占比和行程占比，`travel_in_view_widths` 以一屏宽为单位。新画幅和起止锚点由剪辑端选择。未测得行程时，`travel_basis` 明示构图默认值。环绕等无法用此模板表达的动作仍能按描述检索。旧素材需按 `clip` 重新解析才能生成运镜记录。

`top_k` 是不同 Chunk 的数量（最多 20），不是 Event 数量；同一 Chunk 可以包含多条命中事件。默认搜索本地 READY 系列，超过 50 个时先用 `list_series` 选择范围。Event 索引未覆盖会返回 `EVENT_INDEX_NOT_READY`，不会伪装成“没有匹配”。

视频时间全部使用整数毫秒，区间左闭右开。`read_evidence` 的 `next_evidence_ref` 继续读取同一来源版本的上下文；`EVIDENCE_STALE` 表示需要重新检索。`original_url` 可用于下载原片，`playback_url` 仅在编解码检查通过时提供；地址指向本机 API，源媒体缺失时返回警告。外部 Agent 自行下载、按毫秒区间剪辑，MCP 不运行制作代码。

在已恢复正常 API/数据库/ES/媒体的本机，可为一次 Codex 会话临时配置：

```bash
codex -c 'mcp_servers.seekino.url="http://127.0.0.1:9621/mcp"' \
  -c 'mcp_servers.seekino.required=true'
```

然后让 Codex“调用 Seekino 的 list_series；选择已可检索的系列，retrieve 一个实际内容，再 read_evidence 核对来源并取得媒体”。如需持久保存连接，使用 `codex mcp add seekino --url http://127.0.0.1:9621/mcp`，该命令会修改本机的 Codex MCP 配置。连接方式见 [Codex 官方 MCP 文档](https://learn.chatgpt.com/docs/extend/mcp)。

MCP SDK 根据服务端函数签名、类型和说明生成工具 JSON Schema，通过 `tools/list` 提供给客户端；实际请求通过 `tools/call` 调用现有 Service。协议客户端测试不等于真实素材可用，正式演示还需要已解析并完成 Event 索引的视频和对应媒体。

本版限定直接 loopback 连接，同时检查 Host/Origin 并拒绝转发头；请直接连接 API 端口，不经过 Vite、Nginx 或 Docker 代理。它没有提供 Cloud PAT/OAuth 接入，也不能把此本机端点直接暴露到公网。

## 模型 Token 用量

`GET /api/usage/overview?workspaceId=...` 从 `model_usage` 查询最近 30 天已记录调用。
可用 `fromStartTime`/`toStartTime`（Unix 毫秒）、`seriesId`、`runId` 缩小范围；最大窗口 31 天。
SQL 先按 workspace、半开 UTC 窗口、精确 run/系列筛选，再稳定排序读取最多 20,000 条。
响应固定 `source=local`，状态为 `available` 或 `unavailable`；只包含 Token，不返回费用或货币。

V2 解析/index 使用真实视频解析任务 ID；助手 direct、Web 和检索共用真实助手消息 ID。
Chat 在实际执行线程绑定 owner，内部 embedding/rerank 继承它，不创建独立 retrieval run。
Gateway 最终事件触发一次短事务，写入失败记录 warning 并保留业务结果。
SDK 明细替代父用量，缓存和推理子集不重复加入总量；未知、main_loop 或截断保持完整总量 null，
已知数字通过 `knownTokens` 单独报告。读取失败不会返回 available 的全零结果。

新表沿用现有模型登记和 init-db，真实建表须先核实操作授权。默认 `tracing.enabled=false`，
bootstrap 不装配 Langfuse writer/reader；可选模块和空凭据字段保留。
V1 用量不并入新统计；本轮退役迁移删除旧 Job 用量，现行 V2 用量和 `model_usage` 保留。
`model_usage` 没有业务对象外键，删除视频/消息不级联删除已记录用量。
预算、租约、heartbeat、取消和模型控制仍由原执行管理器负责。

## 数据库与升级

新库使用 `python -m server.cli init-db` 初始化 MySQL，用 `python -m server.cli init-es-index` 创建或检查检索索引。启动器在已有库上校验 Schema，升级历史数据需要单独执行与版本对应的迁移。

升级前备份数据库和媒体，停止相关写入者。SQL 和操作步骤见[迁移目录](../deploy/migrations/)，包括[身份与归属](../deploy/migrations/20261010_user_tenant_identity.sql)、[毫秒时间单位](../deploy/migrations/20261008_video_timeline_milliseconds.sql)、[解析成果](../deploy/migrations/20261031_video_parse_content.md)和[展示元数据](../deploy/migrations/20261104_video_parse_display.md)。部分脚本要求旧表为空，执行前务必核对各自条件。

服务端保存原片及其解析版本；ES 保存派生索引。媒体目录、停服和恢复步骤见[部署说明](../deploy/README.md#local-assets)。
