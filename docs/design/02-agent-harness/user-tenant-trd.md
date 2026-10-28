# Seekino 游客与用户租户体系 TRD

更新日期：2026-10-10。

状态：首版身份、归属、公开课程权限与请求额度代码已实现，隔离验证已进行；迁移 SQL 和 ES 复制工具已准备。未执行现有业务数据库迁移、现有索引复制、正式云部署或真实 SMTP 发信。本文记录当前实现和仍需部署验收的边界。

## 1. 目标与已确认决定

交付云端公开课程体验与邮箱登录：用户无需登录即可浏览、检索、播放公开课程；登录后可在个人资源范围内上传、解析、检索和管理资料。游客与登录用户的功能和额度由服务端控制。

用户已确认：

- 新增两个核心身份与归属模型：`User` → `user`、`Tenant` → `tenant`。
- 首版使用邮箱验证码登录／注册，需要配置邮件发送服务；不采用密码登录。
- 验证码、尝试次数、冷却时间及限流放在 Redis；User 不保存验证码挑战字段。
- 不新增 Workspace 表或产品层级，内容结构保持 `Series → Video → VideoChunk`。

首版采用游客和个人用户的统一身份模型，个人 Tenant 只有一个所有者；只新增 User、Tenant 两张 SQL 表，不新增 AuthSession、验证码表、成员关系、邀请、第三方账号绑定或 API Key。平台公开课程属于平台 Tenant，游客访问课程时不创建个人 Tenant。

本 Agent 负责云端游客与登录体系、资源归属和访问控制。实时识别 sub-agent 与 Chat Supervisor 的算法开发由对应任务推进，本方案约定与其交接的身份、资源范围与用量归属。

## 2. 当前源码基础

旧版本有 22 张业务表；首版新建数据库注册 24 张表，增加 `user` 和 `tenant`，没有 Workspace 实体。以下五张表的资源范围已从 `workspace_id` 改为 `tenant_id`：

| 表 | 当前含义 |
|---|---|
| `series` | 课程资源范围 |
| `agent_conversation` | 对话范围 |
| `upload_session` | 上传范围 |
| `model_usage` | 模型用量范围 |
| `parse_model_call` | 解析调用范围 |

这五处范围及其 DTO、VO、查询、媒体和索引链路已调整。旧数据库的字段和身份结构不匹配时，`create_schema` 明确报升级错误，不自动改字段或补建两张身份表来掩盖旧结构。

现有关系可复用：Video 关联 Series，Chunk 关联 Video，Message 关联 Conversation，UploadSegment 关联 UploadSession，解析子任务和产物关联父任务或 Chunk。

源码依据：[内容模型](../../../server/model/video.py)、[对话模型](../../../server/model/agent.py)、[上传模型](../../../server/model/upload.py)、[任务模型](../../../server/model/job.py)、[用量模型](../../../server/model/parse_usage.py)、[索引同步模型](../../../server/model/index_sync.py)。

## 3. 新增模型的职责

以下字段已在 ORM 中实现，对应迁移 DDL 尚未执行。ORM 类沿用业务名称，不使用 `Record` 后缀；时间字段在具体模型中显式定义。

| 模型 | 建议核心字段 | 语义 |
|---|---|---|
| `User` | `user_id`、可空且唯一的规范化 `email`、`is_guest`、`status`、`auth_version`、`role`、创建／更新时间 | 表示使用者；游客和正式用户使用同一模型；凭据版本支持失效控制 |
| `Tenant` | `tenant_id`、`owner_user_id`、`name`、`type`、`status`、创建／更新时间 | 表示资源归属；首版区分平台公开资源和个人资源 |

个人 Tenant 的 `owner_user_id` 必须明确，首版每个正式用户默认拥有一个个人 Tenant。平台 Tenant 由管理员／系统维护，不能归属为某个访问课程的游客。

`tenant_id` 与 `user_id` 是独立 UUID，不依赖二者相等进行授权。个人 Tenant 的权限通过显式所有者关系判断；固定本地 Tenant 为 `local-default`，固定本地身份为 `local-user`，平台公共 Tenant 为 `platform-public` 且 owner 为空。后续确需团队协作时再增加成员关系。

普通账号登录不自动获得管理员权限。平台维护者可通过固定角色或受控配置表达，首版不建设权限表体系。

## 4. 现有表的归属方案

`tenant_id` 表示资源或业务记录所属范围；用户字段表示私人数据所有者、创建者或当次操作人。不同语义使用明确字段名，不用一个 `user_id` 同时承担全部职责。

用户归属可以通过父表确定，不要求每条子记录重复存用户和租户字段。

| 现有表 | 首版调整 | 用户归属 |
|---|---|---|
| `series` | `workspace_id` 调整为 `tenant_id`；增加独立的 `is_published`，默认 false | 内容权限由 Tenant 决定；首版不重复保存用户所有者 |
| `video`、`video_chunk` | 保留父表关联 | 沿 Video／Series 获取资源归属 |
| `video_chunk_info`、`video_chunk_embedding`、`context_snapshot` | 保留现有关联 | 沿 Chunk／Video／Series 获取资源归属 |
| `agent_conversation` | 范围调整为 `tenant_id`；增加 `owner_user_id` | 对话属于具体使用者，公共 Tenant 内也必须按用户隔离 |
| `agent_message` | 保留 `conversation_id` | 继承 Conversation 的所属用户和范围，无需重复增加字段 |
| `upload_session` | 范围调整为 `tenant_id`；增加 `created_by_user_id` | 记录上传发起者；写权限仍检查目标 Series 的 Tenant |
| `upload_segment` | 保留 `upload_session_id` | 继承上传会话归属 |
| `series_parse_job`、`video_parse_task` | 增加 `created_by_user_id`；Tenant 沿 Series／Video 获取 | 记录首次创建任务的用户，不表示每次重试发起者 |
| `chunk_parse_task`、`interval_parse_task`、`chunk_parse_content`、`interval_parse_content`、`video_chunk_checkpoint` | 保留父任务关联 | 继承主任务的资源归属，不逐层重复增加字段 |
| `index_sync_intent` | 保留 Chunk 关联 | 继承内容资源归属，后台执行者不改变资源所有者 |
| `parse_usage_attempt`、`video_parse_usage_attempt` | 保留现有任务／执行身份，不新增用户字段 | 当前 V2 执行在持久化 runtime 中保存当次 actor，并传入 `model_usage`；不由旧 attempt 推测 |
| `model_usage` | 范围调整为 `tenant_id`；增加 `actor_user_id` | 调用发生时的用户归因，用于用户用量查询和额度控制 |
| `parse_model_call` | 现有资源范围调整为 `tenant_id`；保留任务／执行关联 | 保留旧调用明细；当前 V2 按当次 actor 查询的统一用量使用 `model_usage` |

### 4.1 对话与消息

两个游客可以访问同一个平台 Tenant 的公开课程，但各自拥有独立 Conversation。所有对话列表、详情、删除、发送消息、历史引用和 SSE 订阅都必须验证 `owner_user_id`。

Message 通过父对话确定归属，包括 USER 消息和 ASSISTANT 回复。`role` 表示发送方类型，不能用它替代用户归属验证。

Conversation 创建幂等范围应包含 `(tenant_id, owner_user_id, idempotency_key)`，避免两个游客使用同一个幂等键时复用彼此的对话。上传幂等也需包含发起用户，目标视频位置的占用约束仍按资源判断。

`Conversation.tenant_id` 表示对话所属业务范围，不等于本轮全部检索来源。游客对话可放在平台体验范围内并按 User 隔离；正式用户的个人对话可以读取授权的公开课程。对公开课程的读取权限不授予平台内容写权限或其他人的对话访问权。

首版按选定来源分别检索平台公开资源或个人资源，支持个人对话读取授权的公共 source；对话 owner 与检索资源范围分别验证。跨多个 Tenant 的混合召回尚未交付。

### 4.2 上传、解析与用量

当前 `upload_session` 是上传会话，不是登录会话。分段读取、续传、完成和删除均先检查父上传会话及目标资源权限。

解析任务属于被解析的视频资源，`created_by_user_id` 仅记录首次创建者。Worker、重试和索引同步不会因此成为资源所有者。

当前解析支持复用任务 ID。A 创建任务、B 发起重试时，原任务创建者保持为 A；不能覆盖为 B，也不能将 B 本次产生的消耗一概记给 A。

首版已将当次 `actor_user_id` 写入 V2 任务的持久化 runtime，跨异步 Worker 执行传入 `model_usage`。A 创建、B 手动重试时，`created_by_user_id` 保持 A，本次 runtime actor 更新为 B；自动重试沿用已保存的执行归因，不从当前请求或 Worker 身份猜测。这条链路用于调用归因和用户统计，按实际 Token 扣减硬额度仍未交付。旧 attempt 和历史调用不补造 actor。

`ModelUsage.actor_user_id` 保存调用发生时的用户身份。幂等重放不得覆盖原调用的 Tenant、actor 或用量，也不得再次计入新增消耗。历史记录和系统操作的 actor 无法确认时允许为空，不根据 Tenant 所有者补造。

统一用量 Tenant 的含义：`video_parse` 归目标 Series 的 Tenant，`chat_retrieval` 归 Conversation 的业务 Tenant；被检索课程的来源 Tenant 单独验证。正式用户在个人对话中读取公开课程时，不能将整段对话和用量自动归为平台公开数据。游客对话位于平台体验范围时，用量仍按 actor 区分。

公开课程的历史解析用量属于发布／解析过程；游客检索和问答产生的新调用属于本次体验用户，不把历史解析成本记到访问者名下。

## 5. 游客、邮箱验证与账号生命周期

### 5.1 登录／注册流程

1. 前端先请求 `GET /api/auth/me`。cloud 无 Cookie 或凭据过期时返回 401；前端显式请求 `POST /api/auth/guest` 创建稳定游客并设置 Cookie，不创建个人 Tenant。有效 Cookie 下重复请求 guest 返回原身份。
2. 当前游客提交邮箱，服务端创建与该游客绑定的验证码挑战并发送邮件。
3. 验证成功后，新邮箱升级当前游客 User，保留用户 ID 和对话，创建默认个人 Tenant；已有邮箱则登录已有 User。
4. 登录已有账号时，事务内仅迁移 `owner_user_id` 等于当前游客的对话，保留每条对话的来源 Tenant；原游客被禁用并提升凭据版本。平台课程、其他游客对话和历史用量归因不迁移。
5. 登录成功后换发正式凭据，读取个人登录状态和可用功能；退出或凭据过期后恢复游客体验，前端清理私人状态。

验证码是六位数字，在 Redis 保存 HMAC 摘要、尝试次数、TTL、发送代次和冷却状态；challenge 绑定游客、凭据版本和规范化邮箱，key 中的邮箱/IP 也经过 HMAC。User 不保存 OTP。Lua 原子处理尝试次数及单次消费；发送失败只清理同一次发送的状态，保留后续发送的冷却时间。默认有效期 300 秒、最多 5 次错误、发送冷却 60 秒。邮件中包含验证码，API、日志和源码中没有开发验证码返回入口。

发送和验证分别按邮箱、游客及来源 IP 限流；默认发送窗口 3600 秒，邮箱/游客/IP 上限 5/10/30；验证窗口 300 秒，上限 20/30/100。游客创建本身按 IP 限制 30 次/3600 秒。Redis 不可用时拒绝认证及需要额度准入的云端模型工作，不放行无状态验证码。

配置清单已写入 [.env.example](../../../.env.example)。cloud 必须设置至少 32 字节的签名秘密、Redis 和 SMTP host/sender，缺失时启动失败。SMTP 服务商与真实发信验收仍待部署配置；秘密通过运行环境注入，不写入文档或源码。

### 5.2 凭据、退出与失效

采用 HS256 签名凭据和 HttpOnly Cookie（默认 Secure、SameSite=Lax），普通 API、媒体和 SSE 使用同一身份。凭据包含用户身份、`exp`、认证等级和 `auth_version`；默认 604800 秒有效，每次请求验证签名、期限、SQL 账号状态及版本。cloud 写接口验证 `X-CSRF-Token`；首次 guest bootstrap 不要求已有 CSRF，但拒绝跨站浏览器请求。部署保持前后端同源，配置受控反代并正确传递公开 Host、HTTPS scheme 和真实客户端地址；来源 IP 不能任意信任浏览器提交的转发头。

新邮箱升级和已有邮箱登录都要替换凭据并撤销原游客凭据。登录已有账号时，更新原游客的凭据版本，换发目标账号的正式凭据，不能只处理目标账号。不能仅因数据库中 `is_guest` 已变更，就给旧游客凭据个人资源写权限；刷新也不能将旧游客凭据自动升级为正式凭据。

首版采用账号级失效：退出先取得游客创建额度，再在 SQL 同一事务中提升账号凭据版本并创建新游客，成功时用游客 Cookie 替换正式 Cookie；其他设备的旧版本凭据也会失效。额度拒绝或新游客写入失败时，原账号状态保持有效。没有独立设备会话管理。

凭据过期返回 401，前端重建游客并隔离私人状态；不自动把过期游客 Cookie 升级为正式身份。游客身份/对话清理与保留期尚未实施，凭据过期不删除数据库记录。

## 6. 发布与权限

Series 的发布状态与解析状态分开。首版按完整 Series 发布，不新增 PublicVideo 或发布记录表。只有已发布、解析完成、索引可用且媒体可播放的课程进入游客可访问范围。

| 能力 | 游客 | 普通登录用户 |
|---|---|---|
| 浏览、检索、播放公开课程 | 允许，受体验额度限制 | 允许，额度可更高 |
| 查看和使用自己的对话 | 允许，受问答额度限制 | 允许，注册升级保留原对话 |
| 上传、创建 Series、启动解析 | 不允许 | 在自己的个人 Tenant 内允许 |
| 查看解析任务及用量 | 不提供平台任务和完整平台用量 | 仅允许授权的个人范围与本人用量 |
| 管理、删除自己的课程 | 不允许 | 允许 |
| 读取其他用户的私人课程或对话 | 不允许 | 不允许 |
| 发布平台课程、修改全局模型配置 | 不允许 | 登录本身不授予权限，需要管理员身份 |

身份来自服务端验证过的凭据，不能信任客户端提交的 `user_id`。客户端选择 Tenant、Series、Video 或消息 ID 时，服务端验证每个目标的权限与关联一致性。

API、媒体 Range 请求、图片、召回证据和 SSE 使用同一权限规则；子记录通过父表验证归属。重复保存 Tenant 的表必须与关联资源范围一致，不能用不一致的上传或调用字段绕过父资源检查。

公开课程撤回后，列表、检索、历史证据和媒体入口都应停止对游客提供内容。私人及可撤回媒体采用避免跨身份复用的缓存策略，新媒体请求、Range 请求和重新取证均执行授权；撤回验收针对后续服务读取。账号退出／失效后，已有流式连接在约定检查周期内关闭，重连重新鉴权。

首版模型请求准入在 Redis Lua 内原子预留：游客按 IP 共享 20 次/86400 秒窗口，正式用户按 user_id 200 次/窗口；游客更换身份不能重置 IP 预算。并发默认游客 1、正式用户 3，并同时执行既有服务总并发上限；租约 600 秒，正常及异常完成释放，进程丢失后到期释放。Agent 的执行超时必须小于租约，cloud 强制启用执行限制，游客每轮最多 4 次模型调用、8 次工具调用。local 免请求额度。

这些是请求次数、并发和单轮调用上限，尚未交付按实际 Token 消耗预扣/结算的硬额度；`model_usage` 保存 Token 与 actor 供统计，不应将当前请求窗口宣传成 Token 计费或 Token 硬预算。云端超限返回 429，未获准入不启动新模型调用。

## 7. 兼容与迁移要求

- `workspace_id → tenant_id` 的变化覆盖上述五张表、索引约束、DTO／VO、查询、游标、媒体入口、前端来源状态和检索索引范围。不能只修改 ORM 字段。
- 保留旧范围 ID、Series／Video／Chunk ID 和父子关联；字段改名时保留 Series UUID 的原始种子内容，不重新计算已有资源身份。
- MySQL 与 ES 的归属过滤同步调整，旧索引需要明确兼容读取或字段迁移方案；范围字段变化本身不要求重新计算 embedding。
- 旧本机资料映射到明确的本地兼容 Tenant 和可信本地身份，保留 `local-default` 的历史范围；不能根据缺失身份推测原上传者或解析者。
- 本机继续无需登录，原有上传、解析、检索和播放可用；登录云端不自动上传本机资料或更改本机归属。
- 本地兼容身份由显式服务端运行模式提供，云端匿名请求不能通过参数触发本地完整权限。
- 切换账号或本地／云端来源时，前端取消旧请求、关闭旧 SSE，隔离缓存、播放状态和对话，避免旧私人数据出现在新身份下。

迁移交付见 [SQL](../../migrations/20261010_user_tenant_identity.sql)。它只支持已明确的 `local-default` 历史范围，其他旧范围先制定显式映射；保留原范围值和资源 ID，历史对话归 `local-user`，历史 creator/actor 保持 NULL，所有 Series 默认未发布。SQL 不递归重写历史证据 JSON；[对话 Repository](../../../server/repository/agent.py) 在读取旧 `workspaceId`/`workspace_id` 证据时规范化为 `tenantId`，返回合同只提供新字段。

ES 旧 v5 索引使用独立复制工具，先运行 dry run：

```sh
uv run python -m scripts.migrate_tenant_index --source old_development --destination tenant_development --environment development
```

停 API/Worker 写入后，使用相同命令追加 `--apply` 复制到新 v6 物理索引；成功验证后才切换 `SEEKINO_ELASTICSEARCH_INDEX`。`SEEKINO_ELASTICSEARCH_INDEX_GENERATION` 保留原值，旧索引不修改、不删除，ID、external version 和向量保留，不重做 embedding。版本保留依据 [Elasticsearch 8.19 Reindex](https://www.elastic.co/guide/en/elasticsearch/reference/8.19/docs-reindex.html)。此工具及 SQL 均未对现有数据执行。

职责划分遵循分层目录：[AuthConfig](../../../server/config/auth.py)、[SMTPConfig](../../../server/config/email.py)、[身份常量](../../../server/constants/identity.py)、[Tenant 常量](../../../server/constants/tenant.py)、[认证常量](../../../server/constants/auth.py)、[数据库常量](../../../server/constants/database.py)、[认证错误](../../../server/error/auth.py)、[凭据/CSRF/HMAC 工具](../../../server/utils/auth.py)、[邮箱规范化/SMTP 工具](../../../server/utils/email.py)、[身份 Repository](../../../server/repository/identity.py)、[AuthService](../../../server/service/auth.py)、[访问与额度规则](../../../server/service/access.py)。业务事务和 Redis Lua 保留在 service/repository。

[main.py](../../../server/main.py) 是 HTTP 进程总装配入口：先检查配置，再调用 `caller.init` 创建 MySQL、Redis、ES，随后调用 `bootstrap.build_application_services` 装配 Repository、Service、Agent 和 Consumer，最后交由 `api.app.build_app` 绑定 HTTP 中间件、异常处理和路由。原 `python -m server.api` 启动命令转交 main，`api.app.create_app()` 保留为兼容入口。

[caller/base.py](../../../server/caller/base.py) 是基础设施子装配器，返回 `CallerResources`；[mysql.py](../../../server/caller/mysql.py)、[elasticsearch.py](../../../server/caller/elasticsearch.py)、[redis.py](../../../server/caller/redis.py) 各自负责 SDK 客户端创建、连接适配、失败清理和关闭。`bootstrap.py` 只消费注入的资源并返回 `ApplicationServices`，不创建或关闭上述三种客户端。`ApplicationRuntime` 统一管理业务与资源：先停止 Consumer、Agent 等业务使用者，再释放 ES、Redis、MySQL；构造、HTTP 绑定或启动失败也回收已创建资源。初始化采用显式参数传递，没有客户端全局变量或 DI 框架。

`AuthService` 只借用 Redis，验证码及额度 Lua 仍属于业务 service。SQL Schema 管理保留在 `database.py`，ES Mapping／索引／检索保留在 `storage/elasticsearch.py`，`common/storage.py` 保留本地 Artifact Store 装配。CLI 是按命令选择依赖的一次性总装配入口，通过 caller 上下文释放 MySQL／ES；本地脚本和索引迁移工具同样复用 caller。注入 `create_app(services)` 的调用方负责所注入业务与资源的生命周期。

现有 [OKR](../../dev/OKR.md) 和 [模型 Token 用量设计](model-token-usage.md) 仍有早期 Workspace／模型命名，后续按相应文档职责维护。

## 8. 首版交付与验收

以下是完整部署验收条件。核心实现完成后的全量 Python 测试为 2617 passed、83 skipped；最终配置与迁移预检追加定向验证：auth 35 个与真实 Redis query 14 个用例（共 49 passed、零 skip，临时 SQLite、独立 Redis 与邮件/模型替身），ES 复制预检 27 passed。身份 SQL 在隔离 MySQL 8.4 验证旧 22 表升级至 24 表，并核对历史记录保留。前端 229 项单元测试与生产构建通过，2 项 Chromium 用例使用模拟 API 验证游客、邮箱登录、退出和身份切换。云端真实 SMTP、现有数据迁移、课程导入和真实服务端浏览器联调仍待部署验收。

1. **公开体验**：打开云端网址，无需登录完成公开课程浏览 → 搜索 → 召回片段播放；未发布课程、私人资源和资料写接口不可访问。
2. **邮箱登录**：有效验证码完成新用户注册或已有账号登录；过期、错误、重复消费及超限验证码被拒绝。
3. **游客衔接**：新邮箱注册保留游客 User 和本人对话；已有邮箱登录仅按迁移策略衔接当前游客本人数据；旧游客凭据不能上传、读取私人资源或刷新成正式凭据。
4. **个人流程**：登录用户完成个人 Series 创建 → 视频上传 → 解析 → 检索 → 播放 → 删除，公开课程读取不授予平台写权限。
5. **用户隔离**：两个游客访问同一公开 Tenant、使用相同对话幂等键时仍拥有独立对话；两个正式账号通过 API、媒体、证据、SSE 和用量查询均不能读取彼此私人数据。
6. **执行归因**：任务首次创建者保持不变；当次 actor 跨异步执行与自动重试正确保留，调用重放不重复记账。未知历史 actor 不补造；当前用量归因不等于实际 Token 硬额度扣减。
7. **撤回与失效**：课程撤回后所有游客后续读取入口拒绝访问，覆盖媒体缓存策略、Range 请求与重新取证；退出／凭据失效后旧凭据和流式重连不能继续取得私人数据，已建立 SSE 在约定检查周期内停止继续输出。
8. **限额**：游客与登录用户分别遵守配置的频率、并发及模型额度；超额后不继续发起新模型调用。
9. **本地兼容**：旧资料、资源 ID 和检索结果可用，本机无需登录；切换账号与来源后无旧请求、SSE 或私人缓存残留。

第一轮成果围绕公开体验、邮箱验证及个人资料闭环交付，可作为后续宣传 PV 的真实演示素材；本 TRD 不包含 PV 制作任务。

## 9. 调研启示与后续细化

依据已有 [RAGFlow／Dify 调研报告](../../../outputs/reports/2026-10-09/ragflow-users-tenants-report.html#dify-reference)：Dify 的发布应用使用 EndUser 表示访问者，访问者继承发布方 App／Tenant 范围，不因访问获得个人租户；RAGFlow 分离 User、Tenant 和成员关系。

Seekino 吸收公开资源归发布方、个人资源归 Tenant 的思路，统一同一产品内游客到正式学习用户的身份。首版单所有者可由 `Tenant.owner_user_id` 表达，暂不需要成员表；账号绑定、API Token、团队邀请等在实际需求出现时加入。

后续部署工作包括真实 SMTP 配置与发信、受控执行 SQL/ES 迁移、公开课程准备、同源 HTTPS/反代、浏览器完整流程和运维观察；后续产品能力包括游客保留/清理、按实际 Token 的硬预算、团队成员和多设备会话管理。这些均不改变本轮两个核心 SQL 模型和内容层级。

个人系列 public/private 共享与团队范围的评估见 [Series Sharing TRD](series-sharing-trd.md)（Series sharing and team scope assessment）。
