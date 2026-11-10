# Seekino 系列共享与团队范围 TRD

状态：第一阶段的前后端已经实现。2026-10-19 已备份并迁移本机原生开发数据库，API 和 Web 目前运行的是 Workspace 实现。本轮没有迁移独立的手机云端数据库。第二阶段仍是提案。已确认的需求是：注册用户可以将自己的系列设为私有或公开，公开系列显示分享者。团队范围尚待确认，初步设想是一个用户可以加入多个团队，一个团队可以有多名成员。

背景：[用户与租户 TRD](user-tenant-trd.md)、[本地／云端设计](local-cloud-trd.md)第 6 节。

## 1. 结论

`Tenant` 继续负责内容归属和授权，不引入所有人共享的默认 Tenant。新增**系列级读取规则**：调用者是系列所属 Tenant 的成员，或者系列已显式公开且发布者有效，就可以读取该系列。可见性存为 `series.visibility ∈ {private, public}`；`private` 表示仅所属 Tenant 的成员可读，个人 Tenant 中只有所有者可读，未来团队 Tenant 中则是全团队可读。

因此，首版不必增加 `team` 可见性，也不必为每个系列单独保存所有者。第二阶段接入团队成员时，只需把 `tenant_id == identity.tenant_id` 这一成员判断改为查询成员关系，共享规则本身不变。

## 2. 第一阶段前的实现基线

| 范围 | 当时行为 | 源码 |
|---|---|---|
| Tenant 权限 | 云端只允许读取 `identity.tenant_id` 或平台公共 Tenant。系列、媒体、检索和 Agent 入口都先经过这项检查 | [access.py](../../../server/service/access.py) `require_tenant_read` |
| 发布 | `is_published` 只能由管理员修改，且只用于平台公共 Tenant | [library.py](../../../server/api/library.py) `update_series` |
| 公开条件 | `publication_ready` 要求全部视频均为 READY、已索引且可播放，同时没有待处理上传 | [media.py](../../../server/service/media.py) |
| 公开列表 | `list_public_series_page` 先读取一个 Tenant 下的全部已发布系列，再用 Python 过滤未就绪内容 | [library.py](../../../server/service/library.py) |
| 检索范围 | ES 按单个 `tenant_id`、一组 `series_id` 和 `(video_id, parse_task_id)` 白名单过滤；命中后仍按同一个 Tenant 复核 | [elasticsearch.py](../../../server/storage/elasticsearch.py) `_scope_query` |
| Agent 来源 | `source_tenant_id = body.source_tenant_id or body.tenant_id`，只允许一个 Tenant；再次读取历史证据时重新鉴权 | [agent.py](../../../server/api/agent.py) `_authorize_query_scope` |
| 媒体地址 | `playback_url` 是普通的 `/api/videos/content?…` 地址，每次请求（包括 Range）都鉴权。云端响应使用 `Cache-Control: private, no-store` | [media.py](../../../server/service/media.py)、[api/media.py](../../../server/api/media.py) |
| 系列身份 | 创建系列和未显式提供 `series_id` 的文件导入，都使用 `derive_series_id(tenant_id, name)` | [identifiers.py](../../../server/model/identifiers.py)、[parse_import.py](../../../server/service/parsev1/parse_import.py) |
| 默认昵称 | 注册时将邮箱 `@` 前的部分作为 `nickname` | [identity.py](../../../server/repository/identity.py) `login_email` |
| 个人记录 | `agent_conversation.owner_id`、`upload_session.creator_id`、`series_parse_job.creator_id`、`video_parse_task.creator_id`、`model_usage.user_id` 已存在 | [database.py](../../../server/database.py) `_check_identity_schema` |

第一阶段的改动较小，原因有两点：

- **可见性不必写入 ES。** 检索和 Agent 会先从 MySQL 取得可访问的系列，再构造 ES 查询范围。因此修改或撤回公开状态都不需要重建索引。
- **撤回后，下一次读取就会被拦截。** 媒体地址不是访问凭证，历史证据每次读取也都经过 `require_series_access`。这些检查换用新规则后，系列撤回即可阻止后续读取，不必再增加一套撤回机制。

## 3. 第一阶段：个人系列公开／私有共享（必要）

### 3.1 Schema

| 修改 | 说明 |
|---|---|
| `series.visibility` | `VARCHAR(16) NOT NULL DEFAULT 'private'`，并增加 `CHECK (visibility IN ('private','public'))`。先把 `is_published = 1` 的记录回填为 `'public'`，再删除 `is_published`。 |
| 不增加 `series.published_at` | 公开列表继续按现有 `create_time` 和 `id` 排序。 |
| 索引 | 为跨 Tenant 的公开列表增加 `(visibility, create_time, id)`。 |
| 暂不增加 `series.created_by` | 每个非平台 Tenant 目前只有一个所有者，可以通过 `tenant.owner_id` 确定创建者。Tenant 增加第二名成员前必须补上这个字段（见第 4 节）。 |
| 暂不增加成员表 | 第一阶段的成员判断是 `tenant_id == identity.tenant_id`，本地模式跳过检查。 |

之所以改名而不复用 `is_published`，是因为字段含义已经从“仅管理员控制的平台精选发布”变成“所有者控制、其他 Tenant 也可读取”。原有读取方本来就需要逐一检查，改名还能让类型检查和测试找出遗漏。枚举也为以后增加 `unlisted`（仅持链接者可读）留出了空间，不必再次改名。

### 3.2 授权规则

系列相关资源不再一律先按 Tenant 拒绝，而是在 [access.py](../../../server/service/access.py) 中使用下面两个函数：

```text
is_member(identity, tenant_id)     # 第一阶段：本地模式，或 tenant_id == identity.tenant_id
                                   # 第二阶段：存在有效的 tenant_member 记录

can_read_series(identity, series):
    local mode                                   -> allow
    is_member(identity, series.tenant_id)        -> allow (full view)
    series.visibility == 'public'
      and tenant.status == ACTIVE
      and (tenant.type == 'public' or owner user.status == ACTIVE)
                                                 -> allow (public view)
    otherwise                                    -> 404

can_manage_series(identity, series):
    local mode                                   -> allow
    email user and is_member(identity, series.tenant_id)  -> allow
    admin and series.tenant_id == public tenant  -> allow
    otherwise                                    -> 404
```

- **修改可见性**需要通过 `can_manage_series`。管理者可在任意解析阶段显式设为 `public` 或撤回。
- **管理员下架：**管理员可以把任意系列改为 `private`，用于撤下用户公开的内容。管理员对非所属 Tenant 执行下架时，只能提交可见性字段；接口返回 `204`，不返回系列数据，也不因此获得私有内容的读取权限。
- **Tenant 级资源仍仅成员可用：**上传会话、解析任务、解析与模型用量、对话继续使用 `require_tenant_read`／`require_tenant_write`。公开系列只开放自身的 Series、Video、图片、Chunk 和证据读取，不会同时开放同一 Tenant 下的其他系列、上传、任务、用量或对话。
- **检查顺序：**先按 `(series_id, tenant_id)` 查出系列，确认保存的 `tenant_id` 一致，再执行 `can_read_series`。不能像原来一样，在查询系列之前就被 Tenant 权限挡住。

以下入口改用 `can_read_series`：`list_series`、`get_series`／`read_series`、`get_video_content`（播放及 Range）、`get_image_content`、`get_video_chunk`、检索（`api/search.py`）、Agent 发送消息（`_authorize_query_scope`）、Agent 历史与证据（`require_series_access`），以及 SSE 证据投影。

### 3.3 公开访问者能看到什么

公开访问者可以看到标题、简介、封面、已入库的视频、时长及逐集可用的播放和缩略图。管理字段（`latest_job_id`、`last_error`、`original_filename`、`total_bytes`、`important_extra_data`、转写提示）返回 null；`pending_uploads` 和 `character_bible` 返回空值。Chunk、检索、Agent 历史和 SSE 证据中也不能暴露原始文件名。`variant=original` 仅成员可用，公开访问者使用 `playback`。

这里限制的是接口返回内容，不是另建一套供模型使用的保密上下文。解析和 Agent 推理仍会使用系列背景与人物信息；已知图片 URL 在所属系列公开后仍可读取，这些信息也可能影响生成答案。如果要在公开推理中隐藏部分背景，需要另行设计模型和工具的输入策略，不属于本批后端改动。

### 3.4 发布者（分享者）身份

每个系列摘要和详情都返回 `publisher`，由所属 Tenant 推导：

| Tenant 类型 | `publisher` |
|---|---|
| `personal`／`local` | `{kind: "user", display_name: owner.nickname, avatar: ImageAsset \| null}` |
| `public`（平台） | `{kind: "platform", display_name: tenant.name, avatar: tenant avatar \| null}` |
| `team`（第二阶段） | `{kind: "team", display_name: tenant.name, avatar: …}`；如需显示上传者，可从 `created_by` 生成署名 |

接入前还要明确两项合同：

- **隐私：**新账号使用生成的 `用户-<short hash>` 昵称。公开投影遇到空昵称，或与邮箱、邮箱前缀相同的旧昵称时，也替换为生成名；自定义昵称保留。`publisher` 不返回邮箱，也不需要给 User 增加字段。
- **头像格式：**第一阶段返回 `null`，界面使用占位图。以后支持上传头像时，应在现有图片存储中保存图片 ID 并返回 `ImageAsset`，不能直接暴露现有自由文本头像字段。

### 3.5 API 修改

| 接口 | 修改 |
|---|---|
| `POST /api/series/update` | 用 `visibility` 替代 `isPublished`。所有者可管理自己的系列，管理员也可改为 `private` 下架。空系列和处理中系列均可显式公开。成员修改后返回系列详情；非成员管理员下架返回 `204`。 |
| 新增 `GET /api/series/public` | 分页列出跨 Tenant 的显式公开且发布者有效的系列，并返回 `publisher`。游标范围不能包含调用者自己的 Tenant。 |
| `GET` 系列列表／详情 | 增加 `publisher` 和 `access: "member" \| "public"`，前端据此隐藏编辑、上传和任务控件。`tenantId` 继续作为寻址参数，并核对它与保存值一致。 |
| 检索／Agent 发送 | `sourceTenantId` 可以指向其他用户的 Tenant，但必须显式提供 `seriesIds`，并逐个通过 `can_read_series`。只有平台公共 Tenant 仍允许空 `seriesIds` 表示“全部公开系列”，避免借此枚举其他 Tenant。 |
| 媒体／图片／Chunk | URL 不变；授权改用 `can_read_series`，`original` 仅成员可用。 |

每轮 Agent 都要重新鉴权：既检查本轮选择的来源，也检查作为模型输入复用的全部历史来源，包括播放上下文引用的系列。检查发生在模型执行前、JSON 输出前，以及每个 SSE 事件等待前后。历史来源撤回后，原对话可能暂时无法读取或使用，直到该来源恢复可访问；用户也可以选择其他来源另开对话。

第一阶段不支持在一次查询中跨发布者检索，见第 5 节。

### 3.6 第一阶段迁移

- 迁移脚本 `docs/migrations/20261019_series_visibility.sql` 增加 `visibility`，从 `is_published` 回填，增加约束和索引，再删除旧列。ORM、启动时的 Schema 检查、后端 DTO／VO 同步修改。切换运行中的部署前，还要先更新前端合同和调用方。应用不会自动执行该迁移。
- 不需要迁移或重建 ES，也不修改 Tenant ID 和系列 ID 的生成方式。
- 本地模式行为不变：跳过权限检查，`visibility` 只保存、不生效。本地实例与手机云端实例继续使用独立进程和数据命名空间。本轮不支持从本地实例发布到云端。

### 3.7 首版公开条件：整个系列都已就绪

2026-10-19 的首版决定如下，2026-11-10 的修订见本节末尾。首版的 `publication_ready` 要求整个系列全部就绪。用户向已公开系列添加第 5 集后，前 4 集也会暂时对外隐藏，直到新集处理完成。可以选择：

1. 保持整系列就绪（当前行为，最简单，但需要说明这种使用体验）。
2. 至少一集就绪就公开系列，公开访问和检索只覆盖已就绪视频。ES 已按 `(video_id, parse_task_id)` 限定范围，因此不必修改索引。

第一阶段选择方案 1，方案 2 当时留作后续改进。

2026-11-10 修订。首次发布和后续读取均按显式可见性、有效发布者和读者权限执行。每集按原片提供播放。普通聊天可使用当前课程，包括零索引课程。需要检索时逐集判断最新索引状态，反馈实际覆盖、指定集处理中或依赖不可用。撤回后的新 Range 请求、历史证据和后续对话重新鉴权。六阶段设计、现场基线和验收见[公开课程解析期间的访问](../03-product-reassessment/public-course-readiness-trd.md)。

### 3.8 验收矩阵

对第 3.2 节列出的每个入口，分别组合访问者（游客、其他用户、所有者、管理员）和系列状态（私有；公开且就绪；公开但未就绪；公开后撤回；发布者被禁用）逐项验证，还要覆盖：

- 撤回后的 Range 请求。
- 撤回后重新读取 Agent 历史证据。
- SSE 证据投影。
- 某 Tenant 有公开系列时，非成员访问其上传、任务、用量和对话仍返回 404。
- 两个用户使用同一个公开系列时，各自对话保持独立。

### 3.9 前端实现

- 只有服务端返回 `access: "member"` 时，系列卡片、详情和资料库当前系列才显示竖向省略号可见性菜单。修改请求使用该系列自己的 Tenant，成功后使列表和详情缓存失效。其他用户看不到编辑菜单，但最终权限仍由服务端决定。
- 公开内容通过 `/api/series/public` 发现。卡片和详情显示发布者；受管头像上传实现前，用姓名首字符作为占位。
- 系列和播放链接携带资源的 `tenantId`，详情缓存键也包含它。查看其他发布者时，不切换当前账号和连接。
- Chat 的对话和附件仍属于访问者当前范围，检索则单独发送 `sourceTenantId`。每次查询只选择一个发布者的系列。播放中的对话继续使用当前视频来源；追问历史片段时选择被引用的系列，而不是默认发布者。
- 本版不增加内容管理后台。平台管理员保留后端受限下架能力；所有者菜单不能让管理员任意编辑他人的私有内容。
- 本地可见性控件只保存标志用于测试，不会把本地文件上传或同步到另一个云端实例。

## 4. 第二阶段：团队范围（确认前均为提案）

### 4.1 模型

| 修改 | 说明 |
|---|---|
| `tenant.type` | 增加 `'team'`。团队 Tenant 必须有 `owner_id`（创建及计费负责人），并有独立的 `name` 和 `avatar`。 |
| `tenant_member` | 字段为 `id, tenant_id, user_id, role, status, invited_by, create_time, update_time`；增加 `UNIQUE(tenant_id, user_id)`；`role ∈ {owner, admin, member}`；`status ∈ {invited, active, removed}`。 |
| 回填 | 为现有每个 `local` 和 `personal` Tenant 写入一条 `owner/active` 成员记录，此后统一从成员关系判断权限。`owner_id` 继续用于约束和账务归属。 |
| `series.created_by` | `VARCHAR(ID_LENGTH) NULL`，外键指向 `user`。个人和本地 Tenant 按 `tenant.owner_id` 精确回填，平台 Tenant 填 `NULL`。Tenant 增加第二名成员前必须有此字段。 |

与 RAGFlow 不同，其他用户不能加入个人 Tenant，团队使用独立 Tenant。这样不依赖 RAGFlow 的 `tenant.id == user.id` 和 `me` 可见性。`private` 始终表示 Tenant 内可见，创建者另存独立字段。

### 4.2 授权

- `is_member` 改为查询有效成员关系，**每次请求都检查**；如果使用缓存，必须明确失效机制。成员关系不写入令牌，确保移除成员后下一次请求即生效。
- `AuthIdentity.tenant_id` 仅表示当前／默认 Tenant，用于填充默认值，不再代表全部授权范围。
- 角色权限矩阵仍待确认：

| 操作 | member | admin | owner |
|---|---|---|---|
| 读取团队全部系列 | ✓ | ✓ | ✓ |
| 创建系列、上传、启动解析 | ✓ | ✓ | ✓ |
| 编辑元数据／集数顺序 | 自己创建的（`created_by`） | 全部 | 全部 |
| 删除系列 | 自己创建的 | 全部 | 全部 |
| 设为 `public` | — | ✓ | ✓ |
| 邀请／移除成员、修改角色 | — | ✓（不能操作 owner） | ✓ |
| 删除团队／转移所有权 | — | — | ✓ |

- 团队中的个人记录仍归个人：对话继续按 `owner_id` 隔离。管理员能否查看团队用量（`model_usage`、解析用量）需要单独决定，默认仅本人可见更稳妥。
- 全局管理员（`is_superuser`）、Tenant 角色、资源创建者和资源可见性是四项独立判断，任何一项都不能替代另一项。

### 4.3 API

- 团队增删改查、邮件邀请（接受／拒绝／过期）、退出和移除成员（最后一名 owner 不能退出）、角色修改。
- 提供 `GET /api/me/tenants` 和 Tenant 切换入口。切换时，前端按用户租户 TRD 第 7 节取消旧请求和 SSE。

### 4.4 与团队共享个人系列

第二阶段建议**直接在团队中创建**系列。把已有系列移到另一个 Tenant，需要同时修改 MySQL、ES 文档和携带 Tenant 的子表中的 `tenant_id`。移动后，系列也不再符合 `derive_series_id(new_tenant, name)`；以后按名称导入且未显式提供 `series_id` 时，会生成重复系列。因此，如果支持移动，后续导入必须始终使用显式 ID。也可以另设授权表，把一个系列共享给另一个 Tenant，但当前没有必要。

### 4.5 第二阶段迁移

SQL 需要放宽 `ck_tenant_type` CHECK，创建 `tenant_member`，回填 owner 成员记录，再增加并回填 `series.created_by`。无需修改 ES，检索仍按 Tenant 和系列限定范围。

## 5. 跨发布者检索（单独决策）

ES `_scope_query`、`_TrustedScope` 和 Agent `sourceTenantId` 目前都假设只有一个 Tenant。要在一次查询中搜索“所有公开系列”，可信范围需要改为一组 `(tenant_id, series_id)`：`tenant_id` 使用 `terms`，保留每个视频的 `(video_id, parse_task_id)` 白名单，并按每条命中自己的 Tenant／Series 组合复核。Agent 消息详情也要保存 `sourceTenantIds`（或每个系列各自的 Tenant），不能只保存一个 `sourceTenantId`。在此之前，平台公共 Tenant 仍是默认的“搜索全部”范围，用户发布的系列则每次只检索一个发布者。

## 6. 不采用的方案

| 方案 | 不采用的原因 |
|---|---|
| 所有人共享的默认 Tenant | 失去内容权限的归属基础，还会改变所有派生的系列 ID；每张携带 Tenant 的表也都需要另加所有者字段。 |
| 只让普通用户切换 `is_published`，不修改读取链路 | 其他 Tenant 仍会被 `require_tenant_read` 拒绝，公开列表也只查询平台 Tenant。 |
| 发布时把用户系列移到平台公共 Tenant | 会丢失原归属、改变派生 ID，还会让平台管理员获得用户内容的编辑权。 |
| 照搬 RAGFlow 的 `me`／`team` | 不支持匿名公开访问，`me` 还依赖 `tenant.id == user.id`。 |
| 第一阶段为每个系列增加 `team` 可见性 | 当前还没有团队 Tenant；在这套模型中，Tenant 私有本来就包含团队内可见。 |

## 7. 实施顺序

1. 第一阶段 Schema 与迁移、`can_read_series`／`can_manage_series`、第 3.2 节全部入口、公开字段投影和管理员下架。
2. 发布者合同（确认昵称规则、头像图片 ID）和 `GET /api/series/public`。
3. 前端可见性控件、发布者署名、按 `access` 控制界面和公开发现列表。
4. 公开读取和逐集工具覆盖的后续修订见第 3.7 节。
5. 角色矩阵确认后再实施第二阶段团队模型。跨发布者检索（第 5 节）可独立推进，不依赖第二阶段。
