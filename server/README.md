# Seekino Server

API 为 Web 和外部工具提供上传、任务状态、检索和观看问答。启动后，在 Web 同源入口打开 `/api/docs` 查看接口、参数与响应。宿主机 API 的默认端口为 9621。用户范围字段为 `tenantId`，视频时间为相对原片起点的整数毫秒。

## 本机 MCP：供 Codex 等外部 Agent 调用

`SEEKINO_AUTH_MODE=local` 时，正常 API 进程同时提供 Streamable HTTP MCP：`http://127.0.0.1:9621/mcp`。若 API 端口不同，使用实际端口。它随 API 进程启动。Cloud 部署提供同路径的 HTTPS 服务。

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

本机端点校验回环来源和 Host/Origin，拒绝转发头。连接本机 API 时直接使用其监听端口。

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

## Cloud MCP

在 Cloud 网页的 `/docs/agents` 可复制带本站地址的命令。也可以在剪辑终端把下方示例的域名换成部署地址，取得游客只读凭据。已有网页登录身份可用相同接口和当前 CSRF 凭据换取令牌。令牌随该会话过期，身份失效时撤销。

```sh
SEEKINO_CLOUD='https://your-seekino-cloud.example'
SEEKINO_MCP_TOKEN=$(curl -fsS -X POST "$SEEKINO_CLOUD/api/auth/mcp-token" | python3 -c 'import json,sys; print(json.load(sys.stdin)["accessToken"])') && export SEEKINO_MCP_TOKEN
```

在同一个终端为所用客户端添加服务，随后启动客户端。

```sh
claude mcp add --transport http seekino_cloud "$SEEKINO_CLOUD/mcp" --header 'Authorization: Bearer ${SEEKINO_MCP_TOKEN}'
```

```sh
codex mcp add seekino_cloud --url "$SEEKINO_CLOUD/mcp" --bearer-token-env-var SEEKINO_MCP_TOKEN
```

`list_series` 返回平台公开租户已发布且 READY 的系列。`browse_events` 返回 Event 引用，`retrieve` 用自然语言检索。先 `read_evidence(evidence_ref)` 核对来源，再给出 `start_ms` 与 `end_ms` 获取 `media.clip_url`。区间使用整数毫秒、左闭右开，每段最多 60 秒。链接最长有效 5 分钟，下载器可直接获取该 MP4，下载时服务端重新核对发布状态、证据版本和用户身份。检索与转码共用既有云端请求和并发额度。以不同名称添加本机端点即可在同一剪辑会话调用两个库。

### 剪辑 Skill、风格参考与 series 共享模板

连接后可用 `resources/read` 读取 `skill://seekino-editing-materials/SKILL.md`。支持 MCP Skills 扩展的客户端可通过 `skills/list` 和 `skills/get` 发现指引与随附的本机分层脚本。也可以给本机 Claude Code 和 Codex 一次安装。

```sh
npx skills add https://github.com/owariband/Seekino/tree/main/server/skills/seekino-editing-materials -a claude-code -a codex -g -y
```

用 `find_edit_templates` 搜索剪辑技法和可见 series 的共享模板，`get_style_card` 读取整片风格并回看例证。用 `get_edit_template` 和 `list_edit_template_versions` 读取所属 series 的版本、来源和反馈。通过 `save_edit_template_version` 指定 `series_id` 保存完整代码与来源，`append_edit_template_note` 追加反馈或本机检查，`set_default_edit_template_version` 明确指定默认版本。

模板归属 series。任何有权查看该 series 的用户都可以读取模板、历史版本和反馈，作者字段用于署名与追溯。游客凭据换新后仍可读取同一公开 series 的模板。写入沿用 series 现有编辑权限。Cloud 编辑者可在账号菜单选择“复制剪辑写入凭据”，或用 `POST /api/auth/mcp-token?scope=personal`、当前会话 Cookie 和 CSRF 请求头领取写入凭据。本机沿用本地 series 权限。客户端首次调用时可先请它读取剪辑 Skill。

从旧版升级时，先停止 API 写入并备份数据库，运行 `uv run python -m scripts.migrate_edit_template_series` 预览，再用 `--apply --writers-stopped` 应用，随后按原方式执行 `init-db`。API 镜像也包含同一脚本，可在容器中用 `python -m scripts.migrate_edit_template_series` 执行预览和升级。迁移给现有模板表增加 `series_id` 和索引。历史未绑定模板会保留，管理者须经确认后将其 `series_id` 与 `tenant_id` 显式绑定到目标 series；绑定前不会出现在共享读取中。模板引用的原片不能作为自动公开旧代码的依据。

### 本机制作 Skill 与镜头运动参考

MCP 分发 `skill://motion-showreel/SKILL.md`、`skill://video-shotcraft/SKILL.md` 及各自的许可证和固定版本信息。在剪辑机上完整安装后，Agent 才能读取各自的参考资料、素材和制作脚本。

motion-showreel 适用于代码驱动的图形、文字、界面动画及支持的人物分层合成，用确定性的 `render(t)`、共享时间线和逐帧渲染制作视频。它原生要求先在工作区预览、确认后导出，除非用户明确免除预览。常规原片选取与裁剪沿用 Seekino 剪辑指引。

```sh
npx skills add https://github.com/ConardLi/garden-skills/tree/2b9a89340b70f632d31c0f1c69fce94a7912423e/skills/motion-showreel -a claude-code -a codex -g -y
```

video-shotcraft 提供 Remotion 镜头配方、demo、模板和工作台，供 Agent 按任务需要参考。

```sh
npx skills add https://github.com/Vincentwei1021/video-shotcraft/tree/6700db2ba80572ebc777b99d72049679fed9840c -a claude-code -a codex -g -y
```

Skill 提供镜头运动的参考和建议，Agent 根据镜头意图、节奏与素材，在剪辑工程里自行编写速度、位移及形变代码。钟形速度与积分得到的 S 形位移可以作为平移或推拉的参考；拉伸、拖影等可按瞬时速度和方向联动。Agent 可以复用或调整上游辅助函数，并回看实际效果。原片观测与实测轨迹继续用于核对来源。

[video-shotcraft](https://github.com/Vincentwei1021/video-shotcraft) 的代码与文档使用 Apache-2.0，MCP 分发中保留许可证、来源与修改说明。喜欢这套镜头库，欢迎给上游项目 Star。完整包中的媒体和渲染依赖按上游各自的授权说明使用。

[motion-showreel](https://github.com/ConardLi/garden-skills/tree/2b9a89340b70f632d31c0f1c69fce94a7912423e/skills/motion-showreel) 来自 ConardLi/garden-skills，使用 MIT，分发中保留原版权与许可证。觉得有帮助，欢迎给上游项目 Star。
