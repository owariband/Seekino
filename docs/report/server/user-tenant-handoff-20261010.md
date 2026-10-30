# 游客与邮箱登录、User/Tenant 交付说明

日期：2026-10-10。范围：已经授权开发的云端游客体验、邮箱验证码登录、归属及权限，并按用户最新要求整理 constants/config/utils/error。现有本地资料不自动发布或搬到云端。

## 已实现行为

- 只新增 User→user、Tenant→tenant 两张 SQL 表；内容仍为 Series→Video→VideoChunk。游客拥有 User，不创建个人 Tenant。个人 Tenant 使用独立 UUID 和显式 owner。
- 首次访问建立稳定游客身份；邮箱验证码完成注册或已有账号登录。新邮箱升级保留游客 User ID 和对话；已有邮箱登录仅转移当前游客本人对话，同幂等键碰撞时保留双方原对话。旧游客凭据撤销。
- HS256 签名、HttpOnly/Secure/Lax Cookie、有效期、auth_version、CSRF；退出撤销账号旧版本凭据并换发游客。首版没有 AuthSession 表，未提供逐设备独立撤销。
- 验证码以 HMAC 摘要、TTL、冷却、尝试次数和多维限流放在 Redis；不落 User，不返回或记录验证码。SMTP 由部署配置提供。
- Series 使用 tenant_id 和独立 is_published；公开课程仅在全部内容解析/索引完成、无待上传且可播放时开放。API、原始媒体/Range、图片、历史证据和 SSE 按同一来源权限检查；后续读取尊重课程撤回。
- Conversation.owner_user_id 是私人对话所有者；Message 继承对话，其他子表沿父级资源授权。上传和任务记录首次创建者，当次重试 actor 保存在 V2 runtime 并传到 ModelUsage；历史未知 actor 为 NULL。
- 游客按 IP 共用默认20次/86400秒请求预算，正式账号按User默认200次；用户并发默认1/3，加上Agent服务总并发准入，租约600秒。cloud 强制启用运行限制，游客最多4次模型调用/8次工具调用。已交付请求和调用限制，实际 Token 预扣/结算硬额度未实现。
- 前端游客/登录/退出、公共/个人来源切换已接入。切换身份取消旧请求、关闭 SSE、清空查询缓存并隔离对话与播放进度。local 模式保留明确可信的本地使用流程。

## 目录分工

| 位置 | 本轮职责 |
|---|---|
| constants/auth.py、identity.py、tenant.py、database.py | 固定协议、标识、ID长度和数据库表参数 |
| config/auth.py、email.py、settings.py | 部署参数、默认值、加载与校验，包括 Redis 超时 |
| error/auth.py | 认证失败、身份冲突和访问拒绝错误 |
| utils/auth.py、email.py | 签名/验签、CSRF/HMAC、邮箱规范化和 SMTP 适配 |
| caller/mysql.py、elasticsearch.py、redis.py | 共享外部客户端创建、连接适配、失败清理与关闭；bootstrap 编排顺序 |
| repository/identity.py | 身份及对话转移事务、SQL 持久化 |
| service/auth.py、access.py | 账号生命周期、OTP Lua、权限和请求额度业务编排 |
| model/dto、model/vo、api、router | 请求与响应合同、HTTP 适配及路由 |

constants/workspace.py 改名为 constants/tenant.py。没有增加 Workspace 实体。保留前端 WorkspaceProvider 等现有布局概念和 Agent 原有模块名，避免把资源归属字段调整扩大成无关文件重命名。

## 兼容与迁移

1. SQL 五处 workspace_id→tenant_id 保留旧范围值和资源 ID；历史对话显式归 local-user，历史 creator/actor 不补造，所有 Series 默认未发布。
2. 旧22表和部分升级状态被 create_schema() 明确拒绝，不会自动补建两张身份表造成混合结构。提供受控 SQL，当前预检只接受已明确的 local-default 旧范围，其他范围需先建立映射。
3. 历史消息证据 JSON 仅在已知 evidence 范围读取适配，不递归改写原始 JSON。
4. ES v5→v6 提供默认 dry run 的复制脚本。拒绝无完整 stored _source 或向量类型/维度不符的来源；保留文档 ID、external version 和向量，旧索引保持原样。停写后受控复制、验证，再切换配置，不重新计算 embedding。
5. HTTP 合同和前端参数改用 tenantId。CLI 主参数为 --tenant-id，同时保留 --workspace-id 输入别名。

索引 external version 保留依据：[Elasticsearch 8.19 Reindex 官方文档](https://www.elastic.co/guide/en/elasticsearch/reference/8.19/docs-reindex.html)。

## 验证结果

| 验证 | 结果与边界 |
|---|---|
| 核心完成后 Python 全量：.venv/bin/pytest tests -q --tb=short | 2617 passed、83 skipped；skip 为外部集成条件未配置，后续已独立运行真实 Redis。该数字是核心完成时的全量快照 |
| 最终认证、额度、cloud API/权限/运行预算、SSE、schema、ES预检 | 294 passed、零 skip，独立 Redis16379/15、临时 SQLite、邮件/模型替身 |
| 其中 auth + query + ES预检 | 76 passed，其中身份及真实 Redis49、ES复制27 |
| auth + config | 251 passed，包含 Redis 超时配置加载与非法值拒绝 |
| web：npm test | 19文件、229项通过 |
| web：npm run build | TypeScript及生产构建通过 |
| web：npm run test:e2e -- tests/auth.spec.ts --project chromium --reporter=line | 两项 Chromium 用例通过；运行实际生产JS，API模拟，不表示真实邮件/模型联调已完成 |
| MySQL8.4.11 实际 SQL 验证 | HEAD旧22表→24表，9条历史数据保留，5scope更名，owner/creator/actor/publish及种子验证通过；重复执行预检拒绝 |
| Compose | YAML及可选 cloud-auth Redis profile 检查通过 |

最终定向回归命令：
```sh
env SEEKINO_TEST_AUTH_REDIS_URL=redis://127.0.0.1:16379/15 .venv/bin/pytest tests/server/test_auth.py tests/server/test_query_limits.py tests/server/test_business_api_auth.py tests/server/test_cloud_access.py tests/server/test_cloud_library_access.py tests/server/test_cloud_agent_runtime.py tests/server/test_sse_events.py tests/server/test_database.py tests/server/test_tenant_index_migration.py -q --tb=short
```

临时 MySQL 仅 Unix socket，skip_networking=ON、port=0，不读取.env.local；验证后 mysqladmin shutdown 成功。证据保存在 /private/tmp/seekino-identity-mysql-1k1xx2z_/validation_result.json、legacy_snapshot.json、current_snapshot.json 和 server.log。临时 Redis16379已通过 SHUTDOWN NOSAVE 关闭，未使用 FLUSHDB，未操作已有服务。

## 改动量与共享工作区边界

- 本任务触及清单共193份文件：生产/配置120、测试69、文档/迁移4；语义状态 M=159、A=33、R=1，没有本任务独立批量删除。
- 其中确定性合同替换覆盖138份文件。按操作前保存的原文计算，单纯文本替换 +1549/-1549行；这不是新增业务逻辑行数。旧文件名改为 tenant.py 在树中按 R 展示。
- 当前整个共享工作区 git diff --shortstat 快照：216 files changed, 6439 insertions(+), 3490 deletions(-)。这是所有 Agent/原有修改合计，不可作为本任务独占改动量。
- 清单覆盖的已跟踪路径 git diff --numstat 汇总：158条路径，+5010/-2256行；部分路径有并行改动及前次已授权 Mixin 清理，同样不代表本任务单独新增量。新增未跟踪文件不计入 Git numstat。
- staged 为空。快照时未跟踪文件61份，随后新增本报告；其中包含其他 Agent 及原有工作，未加入暂存。TRD 原目录被 gitignore，未 force-add。
- 合同替换基线保存在 /private/tmp/seekino-identity-baseline-pdwlnbis/manifest.json 及镜像原文，可用于分离共享文件中的文本更名。原有未跟踪的实时识别测试仅修改其中范围字段，树中标 M。
- 用户最新要求补齐 config/constants/utils/error 归属，新增相关认证模块在该职责内；未进行其他领域的错误类搬迁或目录重构。
- 全库 git diff --check 当前只报共享 agent/chat/video_search/context.py 新增末尾空行；基线只有一个换行，当前并行版本有两个。本任务没有清理该并行内容。排除该文件的其余已跟踪 diff 空白检查通过，新增ES文件另行检查通过。

## 实际修改树

树描述本任务在各文件的调整；共享文件中的实时识别、模型输出和解析框架等其他并行变化由相应 Agent 负责，本报告不将其计入游客功能实现。

```text
Seekino/
├── agent/
│   └── chat/
│       ├── common/
│       │   └── [M] state.py  ← 当前检索范围字段改为 tenant_id
│       │       影响：仅本任务合同替换；其他并行 Agent 行为不归本报告
│       ├── video_search/
│       │   ├── tools/
│       │   │   └── [M] retrieval.py  ← 当前检索范围字段改为 tenant_id
│       │   │       影响：仅本任务合同替换；其他并行 Agent 行为不归本报告
│       │   ├── [M] context.py  ← 当前检索范围字段改为 tenant_id
│       │   │   影响：仅本任务合同替换；其他并行 Agent 行为不归本报告
│       │   ├── [M] evaluator.py  ← 当前检索范围字段改为 tenant_id
│       │   │   影响：仅本任务合同替换；其他并行 Agent 行为不归本报告
│       │   ├── [M] output.py  ← 当前检索范围字段改为 tenant_id
│       │   │   影响：仅本任务合同替换；其他并行 Agent 行为不归本报告
│       │   ├── [M] query.py  ← 当前检索范围字段改为 tenant_id
│       │   │   影响：仅本任务合同替换；其他并行 Agent 行为不归本报告
│       │   ├── [M] runtime.py  ← 当前检索范围字段改为 tenant_id
│       │   │   影响：仅本任务合同替换；其他并行 Agent 行为不归本报告
│       │   └── [M] workspace.py  ← 当前检索范围字段改为 tenant_id
│       │       影响：仅本任务合同替换；其他并行 Agent 行为不归本报告
│       └── [M] supervisor.py  ← 当前检索范围字段改为 tenant_id
│           影响：仅本任务合同替换；其他并行 Agent 行为不归本报告
├── docs/
│   └── migrations/
│       └── [A] 20261010_user_tenant_identity.sql  ← 交付两表与历史归属受控迁移
│           影响：保留旧 ID，拒绝不完整或重复迁移
├── engine/
│   └── rag/
│       ├── indexing/
│       │   └── [M] pipeline.py  ← 当前检索范围字段改为 tenant_id
│       │       影响：仅本任务合同替换；其他并行 Agent 行为不归本报告
│       ├── retrieval/
│       │   ├── [M] contracts.py  ← 当前检索范围字段改为 tenant_id
│       │   │   影响：仅本任务合同替换；其他并行 Agent 行为不归本报告
│       │   ├── [M] evidence.py  ← 当前检索范围字段改为 tenant_id
│       │   │   影响：仅本任务合同替换；其他并行 Agent 行为不归本报告
│       │   └── [M] fusion.py  ← 当前检索范围字段改为 tenant_id
│       │       影响：仅本任务合同替换；其他并行 Agent 行为不归本报告
│       └── [M] contracts.py  ← 当前检索范围字段改为 tenant_id
│           影响：仅本任务合同替换；其他并行 Agent 行为不归本报告
├── gateway/
│   └── [M] tracing.py  ← 当前检索范围字段改为 tenant_id
│       影响：仅本任务合同替换；其他并行 Agent 行为不归本报告
├── scripts/
│   └── [A] migrate_tenant_index.py  ← 默认 dry run 的 v5→v6 索引复制与预检
│       影响：保留旧索引、ID、external version 和向量
├── server/
│   ├── api/
│   │   ├── [M] agent.py  ← 接入对话 owner、来源权限、准入和 SSE 重新鉴权
│   │   │   影响：游客执行预算受限，退出/撤回终止后续输出
│   │   ├── [M] app.py  ← 认证配置、受控 CORS、云端无共享缓存
│   │   │   影响：浏览器携带 Cookie，跨身份内容不复用公共缓存
│   │   ├── [A] auth.py  ← 游客、邮箱验证码、退出接口与 Cookie/CSRF
│   │   │   影响：HTTP 层负责请求身份与响应凭据
│   │   ├── [M] context.py  ← 身份获取及 SSE 期间重新鉴权
│   │   │   影响：云端缺少身份服务时拒绝业务访问
│   │   ├── [M] errors.py  ← 适配认证错误、Retry-After
│   │   │   影响：对客户端返回统一状态和错误代码
│   │   ├── [M] jobs.py  ← 解析管理写门禁及 SSE 身份检查
│   │   │   影响：正式账号只能管理授权资源
│   │   ├── [M] library.py  ← Tenant CRUD、公开课程发布与过滤
│   │   │   影响：公共来源只返回发布且可体验的课程
│   │   ├── [M] media.py  ← 媒体、图片及 Range 统一资源授权
│   │   │   影响：媒体 URL 不能绕过课程访问权限
│   │   ├── [M] parse_usage.py  ← 用户用量查询门禁及 actor 过滤
│   │   │   影响：同一公共范围内用量仍按用户隔离
│   │   ├── [M] search.py  ← 按来源授权、请求准入及结果证据校验
│   │   │   影响：公开检索仅包含可体验课程
│   │   └── [M] uploads.py  ← 登录写权限及创建/重试 actor 传递
│   │       影响：游客不能上传，审计记录实际发起者
│   ├── config/
│   │   ├── [A] auth.py  ← 集中身份、Cookie、OTP、Redis 超时及额度参数
│   │   │   影响：service 只消费已验证配置
│   │   ├── [A] email.py  ← 集中 SMTP 配置与校验
│   │   │   影响：发信工具不自行决定部署参数
│   │   └── [M] settings.py  ← 接入 AuthConfig 环境加载
│   │       影响：应用使用同一配置来源
│   ├── constants/
│   │   ├── [A] auth.py  ← 签名协议、Redis 前缀、凭据长度常量
│   │   │   影响：固定协议标识不散落在业务层
│   │   ├── [A] database.py  ← 提取 ID_LENGTH、MySQL 表参数
│   │   │   影响：身份模型不再依赖 Video 模型常量
│   │   ├── [A] identity.py  ← 固定 local-user 标识
│   │   │   影响：显式本地身份在各层一致
│   │   └── [R] tenant.py  ← 替代 workspace.py，增加公共 Tenant 标识
│   │       影响：保留 local-default 历史范围
│   ├── design/
│   │   └── [M] user-tenant-trd.md  ← 维护已实现归属方案、结构和验证边界
│   │       影响：原目录受 gitignore 管理，未强制加入跟踪
│   ├── error/
│   │   ├── [A] __init__.py  ← 建立认证错误模块入口
│   │   │   影响：错误类型拥有明确归属
│   │   └── [A] auth.py  ← 认证、身份冲突及访问拒绝异常
│   │       影响：工具、Repository、Service 共用错误类型
│   ├── model/
│   │   ├── dto/
│   │   │   ├── [M] agentDTO.py  ← 请求/响应范围字段改为 tenant_id / tenantId
│   │   │   │   影响：HTTP 合同与模型范围一致
│   │   │   ├── [A] authDTO.py  ← 定义认证请求与层间身份结构
│   │   │   │   影响：请求身份由服务端验证，不接受客户端指定 User
│   │   │   ├── [M] jobDTO.py  ← 请求/响应范围字段改为 tenant_id / tenantId
│   │   │   │   影响：HTTP 合同与模型范围一致
│   │   │   ├── [M] keyframeDTO.py  ← 请求/响应范围字段改为 tenant_id / tenantId
│   │   │   │   影响：HTTP 合同与模型范围一致
│   │   │   ├── [M] libraryDTO.py  ← 请求/响应范围字段改为 tenant_id / tenantId
│   │   │   │   影响：HTTP 合同与模型范围一致
│   │   │   ├── [M] parseDTO.py  ← 请求/响应范围字段改为 tenant_id / tenantId
│   │   │   │   影响：HTTP 合同与模型范围一致
│   │   │   ├── [M] parseUsageDTO.py  ← 请求/响应范围字段改为 tenant_id / tenantId
│   │   │   │   影响：HTTP 合同与模型范围一致
│   │   │   ├── [M] searchDTO.py  ← 请求/响应范围字段改为 tenant_id / tenantId
│   │   │   │   影响：HTTP 合同与模型范围一致
│   │   │   └── [M] uploadDTO.py  ← 请求/响应范围字段改为 tenant_id / tenantId
│   │   │       影响：HTTP 合同与模型范围一致
│   │   ├── vo/
│   │   │   ├── [M] agentVO.py  ← 请求/响应范围字段改为 tenant_id / tenantId
│   │   │   │   影响：HTTP 合同与模型范围一致
│   │   │   ├── [A] authVO.py  ← 定义身份、权限与验证码响应
│   │   │   │   影响：前端依据服务端能力展示功能
│   │   │   ├── [M] jobVO.py  ← 请求/响应范围字段改为 tenant_id / tenantId
│   │   │   │   影响：HTTP 合同与模型范围一致
│   │   │   ├── [M] libraryVO.py  ← 请求/响应范围字段改为 tenant_id / tenantId
│   │   │   │   影响：HTTP 合同与模型范围一致
│   │   │   ├── [M] searchVO.py  ← 请求/响应范围字段改为 tenant_id / tenantId
│   │   │   │   影响：HTTP 合同与模型范围一致
│   │   │   └── [M] uploadVO.py  ← 请求/响应范围字段改为 tenant_id / tenantId
│   │   │       影响：HTTP 合同与模型范围一致
│   │   ├── [M] agent.py  ← Conversation owner 与包含用户的幂等范围
│   │   │   影响：Message 继承父对话归属
│   │   ├── [M] identifiers.py  ← 范围字段及调用参数改为 tenant_id
│   │   │   影响：沿既有分层传递资源范围，保留原 ID
│   │   ├── [M] parse_usage.py  ← 用量 Tenant 范围及可空 actor
│   │   │   影响：未知历史调用不补造用户
│   │   ├── [A] tenant.py  ← 新增 Tenant ORM 与显式所有者
│   │   │   影响：用户 ID 和个人 Tenant ID 独立
│   │   ├── [M] upload.py  ← Tenant 范围、创建者及按用户幂等
│   │   │   影响：续传会话保持原上传者
│   │   ├── [A] user.py  ← 新增 User ORM 与账号版本字段
│   │   │   影响：游客与正式账号共用身份生命周期
│   │   └── [M] video.py  ← Tenant 范围、Series 发布标识及通用常量归位
│   │       影响：Video/Chunk 沿父级 Series 授权
│   ├── mq/
│   │   └── [M] mysql_queue.py  ← 范围字段及调用参数改为 tenant_id
│   │       影响：沿既有分层传递资源范围，保留原 ID
│   ├── report_docs/
│   │   └── [A] user-tenant-handoff-20261010.md  ← 记录本轮实际树、验证与部署待办
│   │       影响：为完整 Review 提供独立交付说明
│   ├── repository/
│   │   ├── [M] agent.py  ← 按 owner 取数、消息访问与旧证据 JSON 读取适配
│   │   │   影响：保留旧数据且返回新 Tenant 合同
│   │   ├── [A] identity.py  ← 身份事务、账号升级、会话转移与版本撤销
│   │   │   影响：同键冲突保留双方对话，退出撤销旧凭据
│   │   ├── [M] job.py  ← 范围字段及调用参数改为 tenant_id
│   │   │   影响：沿既有分层传递资源范围，保留原 ID
│   │   ├── [M] library.py  ← 公开标识持久化及按 owner 查询最近搜索
│   │   │   影响：公共课程不显示其他用户私人搜索历史
│   │   ├── [M] model_usage.py  ← 持久化调用 actor 并支持本人用量过滤
│   │   │   影响：幂等重放不覆盖原归因
│   │   ├── [M] parse_usage.py  ← 范围字段及调用参数改为 tenant_id
│   │   │   影响：沿既有分层传递资源范围，保留原 ID
│   │   ├── [M] upload.py  ← 上传创建者、任务首次创建者与重试 runtime actor
│   │   │   影响：异步任务跨执行保留真实操作人
│   │   ├── [M] video_parse.py  ← 范围字段及调用参数改为 tenant_id
│   │   │   影响：沿既有分层传递资源范围，保留原 ID
│   │   └── [M] video.py  ← 范围字段及调用参数改为 tenant_id
│   │       影响：沿既有分层传递资源范围，保留原 ID
│   ├── router/
│   │   ├── [M] __init__.py  ← 注册认证路由与业务身份依赖
│   │   │   影响：普通业务接口先验证身份
│   │   └── [A] auth.py  ← 注册五个认证路由
│   │       影响：路由与 API 层沿用现有结构
│   ├── service/
│   │   ├── parsev1/
│   │   │   ├── [M] parse_import.py  ← 范围字段及调用参数改为 tenant_id
│   │   │   │   影响：沿既有分层传递资源范围，保留原 ID
│   │   │   ├── [M] parse_persistence.py  ← 范围字段及调用参数改为 tenant_id
│   │   │   │   影响：沿既有分层传递资源范围，保留原 ID
│   │   │   └── [M] video_processing.py  ← 范围字段及调用参数改为 tenant_id
│   │   │       影响：沿既有分层传递资源范围，保留原 ID
│   │   ├── [A] access.py  ← Tenant/公开资源权限与原子请求额度
│   │   │   影响：统一访问范围，超额不启动新模型调用
│   │   ├── [M] agent_chat.py  ← 对话 owner 传递及来源 Tenant 区分
│   │   │   影响：公开资源来源不改变个人对话用量归属
│   │   ├── [A] auth.py  ← 编排游客、验证码、登录、退出与 Redis Lua
│   │   │   影响：业务生命周期与通用签名/邮件能力分离
│   │   ├── [M] job.py  ← 范围字段及调用参数改为 tenant_id
│   │   │   影响：沿既有分层传递资源范围，保留原 ID
│   │   ├── [M] keyframe_backfill.py  ← 范围字段及调用参数改为 tenant_id
│   │   │   影响：沿既有分层传递资源范围，保留原 ID
│   │   ├── [M] library.py  ← 按可发布条件枚举公开课程与分页
│   │   │   影响：发布状态和解析 READY 独立
│   │   ├── [M] media.py  ← 统一 Series 可体验及资源读取验证
│   │   │   影响：未发布、撤回、未完成资源拒绝公开访问
│   │   ├── [M] parse_usage.py  ← 范围字段及调用参数改为 tenant_id
│   │   │   影响：沿既有分层传递资源范围，保留原 ID
│   │   ├── [M] upload.py  ← 范围字段及调用参数改为 tenant_id
│   │   │   影响：沿既有分层传递资源范围，保留原 ID
│   │   ├── [M] video_parse_index_coordinator.py  ← 范围字段及调用参数改为 tenant_id
│   │   │   影响：沿既有分层传递资源范围，保留原 ID
│   │   ├── [M] video_parse.py  ← 范围字段及调用参数改为 tenant_id
│   │   │   影响：沿既有分层传递资源范围，保留原 ID
│   │   └── [M] video_retrieval.py  ← 范围字段及调用参数改为 tenant_id
│   │       影响：沿既有分层传递资源范围，保留原 ID
│   ├── storage/
│   │   ├── [M] elasticsearch.py  ← 范围合同改为 tenant_id，索引版本 v6
│   │   │   影响：旧 v5 索引需要受控复制切换
│   │   └── [M] langfuse.py  ← 范围字段及调用参数改为 tenant_id
│   │       影响：沿既有分层传递资源范围，保留原 ID
│   ├── utils/
│   │   ├── [A] auth.py  ← 签名、验签、CSRF 与 HMAC 摘要工具
│   │   │   影响：无 ORM 或业务流程反向依赖
│   │   └── [A] email.py  ← 邮箱规范化与 SMTP 发信适配
│   │       影响：验证码不进入日志或 API 响应
│   ├── [M] bootstrap.py  ← 装配 AuthService、Redis 生命周期及 cloud Agent 限制
│   │   影响：保留并行实时识别服务的装配
│   ├── [M] cli.py  ← 默认 Tenant 与 --tenant-id；保留旧 CLI 别名
│   │   影响：既有 --workspace-id 命令仍能输入
│   └── [M] database.py  ← 注册 User/Tenant 并验证完整身份 Schema
│       影响：旧结构明确要求受控升级，不自动迁移
├── tests/
│   ├── agent/
│   │   └── chat/
│   │       ├── [M] test_realtime_understanding.py  ← 调整该模块的 Tenant 合同测试与范围断言
│   │       │   影响：保护已有行为使用新范围字段；保留并行测试内容
│   │       ├── [M] test_runtime.py  ← 调整该模块的 Tenant 合同测试与范围断言
│   │       │   影响：保护已有行为使用新范围字段；保留并行测试内容
│   │       ├── [M] test_supervisor.py  ← 调整该模块的 Tenant 合同测试与范围断言
│   │       │   影响：保护已有行为使用新范围字段；保留并行测试内容
│   │       └── [M] test_tools.py  ← 调整该模块的 Tenant 合同测试与范围断言
│   │           影响：保护已有行为使用新范围字段；保留并行测试内容
│   ├── engine/
│   │   ├── [M] test_index_pipeline.py  ← 调整该模块的 Tenant 合同测试与范围断言
│   │   │   影响：保护已有行为使用新范围字段；保留并行测试内容
│   │   ├── [M] test_rag_api.py  ← 调整该模块的 Tenant 合同测试与范围断言
│   │   │   影响：保护已有行为使用新范围字段；保留并行测试内容
│   │   ├── [M] test_rerank.py  ← 调整该模块的 Tenant 合同测试与范围断言
│   │   │   影响：保护已有行为使用新范围字段；保留并行测试内容
│   │   ├── [M] test_retrieval_conformance.py  ← 调整该模块的 Tenant 合同测试与范围断言
│   │   │   影响：保护已有行为使用新范围字段；保留并行测试内容
│   │   ├── [M] test_retrieval_pipeline.py  ← 调整该模块的 Tenant 合同测试与范围断言
│   │   │   影响：保护已有行为使用新范围字段；保留并行测试内容
│   │   ├── [M] test_video_retrieval.py  ← 调整该模块的 Tenant 合同测试与范围断言
│   │   │   影响：保护已有行为使用新范围字段；保留并行测试内容
│   │   └── [M] test_visual_query.py  ← 调整该模块的 Tenant 合同测试与范围断言
│   │       影响：保护已有行为使用新范围字段；保留并行测试内容
│   ├── gateway/
│   │   └── [M] test_usage.py  ← 调整该模块的 Tenant 合同测试与范围断言
│   │       影响：保护已有行为使用新范围字段；保留并行测试内容
│   └── server/
│       ├── [M] test_agent_chat_service.py  ← 调整该模块的 Tenant 合同测试与范围断言
│       │   影响：保护已有行为使用新范围字段；保留并行测试内容
│       ├── [M] test_agent_system_integration.py  ← 调整该模块的 Tenant 合同测试与范围断言
│       │   影响：保护已有行为使用新范围字段；保留并行测试内容
│       ├── [M] test_api.py  ← 调整该模块的 Tenant 合同测试与范围断言
│       │   影响：保护已有行为使用新范围字段；保留并行测试内容
│       ├── [A] test_auth.py  ← 保护登录升级、验证码、撤销与配置
│       │   影响：Lua 在真实隔离 Redis 验证
│       ├── [M] test_bootstrap.py  ← 验证认证装配及 cloud 运行限制
│       │   影响：独立测试所需配置明确
│       ├── [A] test_business_api_auth.py  ← 覆盖业务接口身份及权限
│       │   影响：云端游客写入、他人资源读取被拒绝
│       ├── [M] test_cli_elasticsearch.py  ← 调整该模块的 Tenant 合同测试与范围断言
│       │   影响：保护已有行为使用新范围字段；保留并行测试内容
│       ├── [A] test_cloud_access.py  ← 覆盖实际 cloud ACL 与 SSE 失效
│       │   影响：已有流连接定期重新鉴权
│       ├── [A] test_cloud_agent_runtime.py  ← 覆盖真实运行时游客及调用预算
│       │   影响：旧 maxLoopTurns 参数不能绕过 cloud 限制
│       ├── [A] test_cloud_library_access.py  ← 覆盖公开课程、历史证据及媒体入口
│       │   影响：撤回与未发布资源不能通过其他读取入口访问
│       ├── [M] test_config.py  ← 覆盖身份配置与环境加载
│       │   影响：无效部署参数被拒绝
│       ├── [M] test_database.py  ← 覆盖24表、新旧结构拒绝与归属列校验
│       │   影响：受控迁移前旧库不会被部分补建
│       ├── [M] test_elasticsearch_integration.py  ← 调整该模块的 Tenant 合同测试与范围断言
│       │   影响：保护已有行为使用新范围字段；保留并行测试内容
│       ├── [M] test_elasticsearch_storage.py  ← 调整该模块的 Tenant 合同测试与范围断言
│       │   影响：保护已有行为使用新范围字段；保留并行测试内容
│       ├── [M] test_index_sync_service.py  ← 调整该模块的 Tenant 合同测试与范围断言
│       │   影响：保护已有行为使用新范围字段；保留并行测试内容
│       ├── [M] test_job_repository.py  ← 调整该模块的 Tenant 合同测试与范围断言
│       │   影响：保护已有行为使用新范围字段；保留并行测试内容
│       ├── [M] test_keyframe_backfill.py  ← 调整该模块的 Tenant 合同测试与范围断言
│       │   影响：保护已有行为使用新范围字段；保留并行测试内容
│       ├── [M] test_langfuse_storage.py  ← 调整该模块的 Tenant 合同测试与范围断言
│       │   影响：保护已有行为使用新范围字段；保留并行测试内容
│       ├── [M] test_library_views.py  ← 调整该模块的 Tenant 合同测试与范围断言
│       │   影响：保护已有行为使用新范围字段；保留并行测试内容
│       ├── [M] test_media_views.py  ← 调整该模块的 Tenant 合同测试与范围断言
│       │   影响：保护已有行为使用新范围字段；保留并行测试内容
│       ├── [M] test_model_usage_repository.py  ← 调整该模块的 Tenant 合同测试与范围断言
│       │   影响：保护已有行为使用新范围字段；保留并行测试内容
│       ├── [M] test_mq_consumer.py  ← 调整该模块的 Tenant 合同测试与范围断言
│       │   影响：保护已有行为使用新范围字段；保留并行测试内容
│       ├── [M] test_mysql_integration.py  ← 调整该模块的 Tenant 合同测试与范围断言
│       │   影响：保护已有行为使用新范围字段；保留并行测试内容
│       ├── [M] test_mysql_queue.py  ← 调整该模块的 Tenant 合同测试与范围断言
│       │   影响：保护已有行为使用新范围字段；保留并行测试内容
│       ├── [M] test_parse_persistence.py  ← 调整该模块的 Tenant 合同测试与范围断言
│       │   影响：保护已有行为使用新范围字段；保留并行测试内容
│       ├── [M] test_parse_usage.py  ← 调整该模块的 Tenant 合同测试与范围断言
│       │   影响：保护已有行为使用新范围字段；保留并行测试内容
│       ├── [A] test_query_limits.py  ← 覆盖真实 Redis 原子额度、并发及 TTL
│       │   影响：更换游客不重置 IP 预算，拒绝请求不扣次数
│       ├── [M] test_repository.py  ← 调整该模块的 Tenant 合同测试与范围断言
│       │   影响：保护已有行为使用新范围字段；保留并行测试内容
│       ├── [M] test_retrieval_rerank.py  ← 调整该模块的 Tenant 合同测试与范围断言
│       │   影响：保护已有行为使用新范围字段；保留并行测试内容
│       ├── [M] test_search_lexical_contract.py  ← 调整该模块的 Tenant 合同测试与范围断言
│       │   影响：保护已有行为使用新范围字段；保留并行测试内容
│       ├── [M] test_sse_events.py  ← 调整该模块的 Tenant 合同测试与范围断言
│       │   影响：保护已有行为使用新范围字段；保留并行测试内容
│       ├── [A] test_tenant_index_migration.py  ← 覆盖索引复制、源数据完整性和维度预检
│       │   影响：失败时不创建或切换目标索引
│       ├── [M] test_upload_service.py  ← 调整该模块的 Tenant 合同测试与范围断言
│       │   影响：保护已有行为使用新范围字段；保留并行测试内容
│       ├── [M] test_video_parse_index_coordinator.py  ← 调整该模块的 Tenant 合同测试与范围断言
│       │   影响：保护已有行为使用新范围字段；保留并行测试内容
│       ├── [M] test_video_parse_media.py  ← 调整该模块的 Tenant 合同测试与范围断言
│       │   影响：保护已有行为使用新范围字段；保留并行测试内容
│       ├── [M] test_video_parse_queue.py  ← 调整该模块的 Tenant 合同测试与范围断言
│       │   影响：保护已有行为使用新范围字段；保留并行测试内容
│       ├── [M] test_video_parse_repository.py  ← 调整该模块的 Tenant 合同测试与范围断言
│       │   影响：保护已有行为使用新范围字段；保留并行测试内容
│       ├── [M] test_video_parse_worker.py  ← 调整该模块的 Tenant 合同测试与范围断言
│       │   影响：保护已有行为使用新范围字段；保留并行测试内容
│       ├── [M] test_video_processing_service.py  ← 调整该模块的 Tenant 合同测试与范围断言
│       │   影响：保护已有行为使用新范围字段；保留并行测试内容
│       ├── [M] test_video_retrieval_service.py  ← 调整该模块的 Tenant 合同测试与范围断言
│       │   影响：保护已有行为使用新范围字段；保留并行测试内容
│       └── [M] test_worker_fencing.py  ← 调整该模块的 Tenant 合同测试与范围断言
│           影响：保护已有行为使用新范围字段；保留并行测试内容
├── web/
│   ├── src/
│   │   ├── api/
│   │   │   ├── [M] client.test.ts  ← 调整该模块的 Tenant 合同测试与范围断言
│   │   │   │   影响：保护已有行为使用新范围字段；保留并行测试内容
│   │   │   ├── [M] client.ts  ← Tenant 合同、Cookie/CSRF 及身份切换取消旧请求
│   │   │   │   影响：API 与 SSE 请求共享身份生命周期
│   │   │   └── [M] contracts.ts  ← Tenant、身份权限和发布状态客户端合同
│   │   │       影响：前后端请求和响应名称一致
│   │   ├── app/
│   │   │   ├── [M] app-shell.module.css  ← 账号入口与来源控件样式
│   │   │   │   影响：沿用现有应用外观
│   │   │   ├── [M] App.test.tsx  ← 调整该模块的 Tenant 合同测试与范围断言
│   │   │   │   影响：保护已有行为使用新范围字段；保留并行测试内容
│   │   │   ├── [M] App.tsx  ← 等待身份就绪后装配应用页面
│   │   │   │   影响：游客初始化完成后再查询内容
│   │   │   ├── [M] AppShell.tsx  ← 账号操作与公共/个人来源切换
│   │   │   │   影响：游客、正式账号展示对应能力
│   │   │   └── [M] workspace.tsx  ← 该页面或状态的范围字段改为 tenantId
│   │   │       影响：请求与缓存沿用新 Tenant 合同
│   │   ├── features/
│   │   │   ├── auth/
│   │   │   │   ├── [A] auth.module.css  ← 邮箱登录交互样式
│   │   │   │   │   影响：与现有弹窗结构一致
│   │   │   │   ├── [A] AuthProvider.test.tsx  ← 覆盖身份启动和切换的缓存隔离
│   │   │   │   │   影响：新身份不承接旧私有状态
│   │   │   │   ├── [A] AuthProvider.tsx  ← 身份初始化、登录退出及旧请求清理
│   │   │   │   │   影响：切换账号关闭旧 SSE，清空查询缓存
│   │   │   │   └── [A] LoginDialog.tsx  ← 邮箱验证码与发送冷却交互
│   │   │   │       影响：登录和注册共用验证流程
│   │   │   ├── library/
│   │   │   │   ├── [M] LibraryPage.tsx  ← 该页面或状态的范围字段改为 tenantId
│   │   │   │   │   影响：请求与缓存沿用新 Tenant 合同
│   │   │   │   ├── [M] SeriesBackgroundEditor.test.tsx  ← 调整该模块的 Tenant 合同测试与范围断言
│   │   │   │   │   影响：保护已有行为使用新范围字段；保留并行测试内容
│   │   │   │   └── [M] SeriesBackgroundEditor.tsx  ← 该页面或状态的范围字段改为 tenantId
│   │   │   │       影响：请求与缓存沿用新 Tenant 合同
│   │   │   ├── player/
│   │   │   │   ├── [M] playbackProgress.ts  ← 按身份和 Tenant 隔离播放进度
│   │   │   │   │   影响：账号切换不读取上一人的缓存
│   │   │   │   ├── [M] VideoPlayer.test.tsx  ← 调整该模块的 Tenant 合同测试与范围断言
│   │   │   │   │   影响：保护已有行为使用新范围字段；保留并行测试内容
│   │   │   │   ├── [M] WatchHomePage.test.tsx  ← 调整该模块的 Tenant 合同测试与范围断言
│   │   │   │   │   影响：保护已有行为使用新范围字段；保留并行测试内容
│   │   │   │   └── [M] WatchPage.test.tsx  ← 调整该模块的 Tenant 合同测试与范围断言
│   │   │   │       影响：保护已有行为使用新范围字段；保留并行测试内容
│   │   │   ├── search/
│   │   │   │   ├── [M] EvidencePlayer.test.tsx  ← 调整该模块的 Tenant 合同测试与范围断言
│   │   │   │   │   影响：保护已有行为使用新范围字段；保留并行测试内容
│   │   │   │   ├── [M] EvidencePlayer.tsx  ← 该页面或状态的范围字段改为 tenantId
│   │   │   │   │   影响：请求与缓存沿用新 Tenant 合同
│   │   │   │   ├── [M] explain.test.ts  ← 调整该模块的 Tenant 合同测试与范围断言
│   │   │   │   │   影响：保护已有行为使用新范围字段；保留并行测试内容
│   │   │   │   ├── [M] SearchPage.test.tsx  ← 调整该模块的 Tenant 合同测试与范围断言
│   │   │   │   │   影响：保护已有行为使用新范围字段；保留并行测试内容
│   │   │   │   ├── [M] SearchPage.tsx  ← 该页面或状态的范围字段改为 tenantId
│   │   │   │   │   影响：请求与缓存沿用新 Tenant 合同
│   │   │   │   ├── [M] useAgentConversation.test.ts  ← 调整该模块的 Tenant 合同测试与范围断言
│   │   │   │   │   影响：保护已有行为使用新范围字段；保留并行测试内容
│   │   │   │   └── [M] useAgentConversation.ts  ← 该页面或状态的范围字段改为 tenantId
│   │   │   │       影响：请求与缓存沿用新 Tenant 合同
│   │   │   ├── settings/
│   │   │   │   └── [M] SettingsPage.tsx  ← 该页面或状态的范围字段改为 tenantId
│   │   │   │       影响：请求与缓存沿用新 Tenant 合同
│   │   │   ├── tasks/
│   │   │   │   ├── [M] ParseUsageDetails.tsx  ← 该页面或状态的范围字段改为 tenantId
│   │   │   │   │   影响：请求与缓存沿用新 Tenant 合同
│   │   │   │   ├── [M] TasksPage.test.tsx  ← 调整该模块的 Tenant 合同测试与范围断言
│   │   │   │   │   影响：保护已有行为使用新范围字段；保留并行测试内容
│   │   │   │   └── [M] TasksPage.tsx  ← 该页面或状态的范围字段改为 tenantId
│   │   │   │       影响：请求与缓存沿用新 Tenant 合同
│   │   │   ├── uploads/
│   │   │   │   ├── [M] upload-types.test.ts  ← 调整该模块的 Tenant 合同测试与范围断言
│   │   │   │   │   影响：保护已有行为使用新范围字段；保留并行测试内容
│   │   │   │   ├── [M] UploadDialog.tsx  ← 使用 Tenant 身份范围
│   │   │   │   │   影响：游客禁用上传入口
│   │   │   │   ├── [M] UploadProvider.test.tsx  ← 调整该模块的 Tenant 合同测试与范围断言
│   │   │   │   │   影响：保护已有行为使用新范围字段；保留并行测试内容
│   │   │   │   └── [M] UploadProvider.tsx  ← 该页面或状态的范围字段改为 tenantId
│   │   │   │       影响：请求与缓存沿用新 Tenant 合同
│   │   │   └── usage/
│   │   │       ├── [M] UsagePage.test.tsx  ← 调整该模块的 Tenant 合同测试与范围断言
│   │   │       │   影响：保护已有行为使用新范围字段；保留并行测试内容
│   │   │       └── [M] UsagePage.tsx  ← 该页面或状态的范围字段改为 tenantId
│   │   │           影响：请求与缓存沿用新 Tenant 合同
│   │   └── [M] main.tsx  ← 在应用入口挂载身份 Provider
│   │       影响：业务页面共同使用已验证身份
│   ├── tests/
│   │   └── [A] auth.spec.ts  ← 新增两个 Chromium 登录生命周期流程
│   │       影响：运行生产 JS，API 模拟，明确不代替 SMTP 联调
│   ├── [M] .env.example  ← 默认范围环境名改为 VITE_TENANT_ID
│   │   影响：客户端使用 Tenant 合同
│   └── [M] vitest.config.ts  ← 限定 src 单元测试收集范围
│       影响：Playwright 和 Vitest 分别运行
├── [M] .env.example  ← 补齐身份、SMTP、Redis、额度环境参数
│   影响：部署参数由配置层统一解析
├── [M] compose.yaml  ← 增加可选 cloud-auth Redis profile
│   影响：本地开发可启动隔离 Redis，默认服务不变
├── [M] pyproject.toml  ← 增加 Redis 客户端依赖
│   影响：OTP 与额度存储使用 Redis
├── [M] README.md  ← 增加游客登录、公开课程与迁移部署说明
│   影响：部署者能区分代码验证和真实部署验收
└── [M] uv.lock  ← 锁定 Redis 依赖版本
    影响：依赖可复现
```

## 上线前剩余事项

- 部署负责人：配置真实 SMTP、强签名秘密、Redis 持久化和同源 HTTPS/反代；正确传递scheme及真实客户端IP，随后验证发信与验证码登录。
- 运行负责人：备份及停写后审阅并执行现有库SQL、ES复制与配置切换；本轮只在临时库执行 SQL，未执行现有数据迁移。
- 内容准备：确认可公开使用的课程，导入到平台 Tenant 并解析/索引，验收播放后显式发布。
- 联调验收：使用真实服务端验证游客浏览→召回→播放、邮箱登录→个人资料上传→解析→检索、退出→游客，以及双用户隔离和课程撤回。
- 产品后续：游客历史保留/清理策略、按实际Token硬额度、团队成员和多设备独立会话管理，当前两表方案不提前扩建这些功能。

本轮没有 commit、push、部署或重启已有服务；保留工作区内既有和并行改动。设计维护于 server/design/user-tenant-trd.md，部署说明同步 README.md。

## 2026-10-10 补充：caller 外部依赖生命周期

用户明确要求新增 caller 来管理 ES、MySQL、Redis 生命周期，本次已完成。原 Redis 创建、ping 和关闭从 AuthService 移到 caller/redis.py；MySQL Engine 与 ES Client 的构造和释放从 database.py、common/storage.py 及启动脚本集中到对应 caller。

bootstrap.py 保留初始化、共享客户端注入和停机顺序编排。每个已创建资源立即登记失败清理；创建本身未完成时由对应 caller 清理，创建成功后的资源由 Bootstrap 释放。正常停机先关闭 Consumer、Agent、Realtime、模型、Tracing、Usage 使用者，再关闭 ES、Redis、DB。关闭某个客户端失败时仍会继续释放其余客户端。

AuthService 只借用注入的 Redis，不负责创建、探活或关闭，已移除其 close 方法。local 模式不创建 Redis。SQL Schema 和升级检查继续 database.py；ES Mapping、索引与检索继续 storage/elasticsearch.py；common/storage.py 只保留 Artifact Store 装配。未引入客户端全局变量、注册表或新的资源聚合类。

原 server.database.create_database_engine 导入保留为薄委托，以兼容已有调用方。MySQL 本地首次建库所需管理连接具有单独入口：先校验完整目标 URL，再连接管理范围；不允许在 production 使用，正式数据库环境保护没有放宽。CLI、索引迁移工具、本地启动和状态脚本均复用 caller；ES 原来的5/3秒超时保留。

实际修改范围20份文件：生产11、测试6、文档3；新增5、修改15。预计树的唯一必要补充是 test_cloud_access.py：其 fixture 原来调用 AuthService.close，现在按借用资源的新职责删除这次调用。所有其他改动保持已展示的范围。

本次相关已跟踪路径的 git diff --stat 快照为9文件、+408/-265行。这是相对HEAD的合并工作区结果，包含之前身份实现与并行改动，不能当作此次caller独占行数；其余已存在但未跟踪的身份文件也作了调整。新caller4文件共243行，新test_caller.py为172行。staged为空，未将已有或新文件加入暂存；TRD仍在原gitignore目录。

### caller 实际修改树

```text
Seekino/
├── server/
│   ├── caller/
│   │   ├── [A] __init__.py  ← 外部依赖层入口
│   │   │   影响：不触发全局客户端初始化
│   │   ├── [A] mysql.py  ← Engine、Session装配、环境保护、UTC及关闭
│   │   │   影响：正式连接和本地管理连接共用正确连接参数
│   │   ├── [A] elasticsearch.py  ← Client构造、超时、上下文与关闭
│   │   │   影响：Server和工具共享构造与失败释放逻辑
│   │   └── [A] redis.py  ← Client构造、可用性检查与关闭
│   │       影响：local不连接Redis，cloud检查失败释放客户端
│   ├── [M] bootstrap.py  ← 注册失败清理、注入Redis及停机释放
│   │   影响：使用者先停止，ES、Redis、MySQL后关闭
│   ├── [M] database.py  ← 实际Engine构造交caller，保留薄委托
│   │   影响：原导入有效，Schema校验位置保持
│   ├── [M] cli.py  ← 使用caller数据库和ES上下文
│   │   影响：命令正常或异常退出都释放资源
│   ├── common/
│   │   └── [M] storage.py  ← 移走数据库与ES装配
│   │       影响：只保留本地Artifact Store构造
│   ├── service/
│   │   └── [M] auth.py  ← 要求注入Redis，移除自建及close
│   │       影响：认证业务不管理共享客户端生命周期
│   ├── design/
│   │   └── [M] user-tenant-trd.md  ← 补齐caller与Bootstrap职责
│   │       影响：设计与当前代码一致
│   └── report_docs/
│       └── [M] user-tenant-handoff-20261010.md  ← 本补充说明
│           影响：记录caller实际范围和验证边界
├── scripts/
│   ├── [M] local_services.py  ← 创建与关闭统一caller
│   │   影响：保留本地未知库管理分支与ES超时
│   └── [M] migrate_tenant_index.py  ← main使用caller上下文
│       影响：ES复制行为保持，客户端统一释放
├── tests/
│   └── server/
│       ├── [A] test_caller.py  ← MySQL、ES、Redis资源与保护回归
│       │   影响：17个用例覆盖参数传递和初始化失败释放
│       ├── [M] test_bootstrap.py  ← 新patch位置、失败阶段与停机顺序
│       │   影响：覆盖Redis/身份/ES失败、关闭失败继续清理
│       ├── [M] test_auth.py  ← 注入要求与真实caller测试fixture
│       │   影响：35个认证用例包含生产Redis创建与关闭
│       ├── [M] test_cloud_access.py  ← 去掉fixture对AuthService.close的调用
│       │   影响：借用资源由外部owner管理
│       ├── [M] test_cli_elasticsearch.py  ← patch真实caller模块
│       │   影响：既有CLI合同继续验证
│       └── [M] test_local_services.py  ← 管理连接及失败关闭回归
│           影响：验证建库成功、admin/schema失败都释放Engine
└── [M] README.md  ← 同步caller目录职责
    影响：部署说明与代码结构一致
```

### caller 验证与边界

- 最终相关回归313 passed、零skip，覆盖caller、Bootstrap、认证、真实Redis请求额度、cloud权限、数据库Schema、CLI、本地启动脚本、索引迁移预检。
- 随后将已有真实验证码fixture直接接到生产caller，再定向验证test_auth.py：35 passed、零skip。没有重新构建前端，因本次未改前端或HTTP行为。
- MySQL/ES初始化与关闭测试使用隔离SQLite或SDK替身；本次没有连接现有MySQL/ES，也没有执行数据迁移。
- 独立只读Review未发现循环导入、双重关闭或未登记的已创建客户端；SQL/ES业务职责保持原层。
- 相关现有文件git diff --check通过。新增caller/测试文件另行检查无尾随空格；新模块导入与迁移工具--help检查通过。
- 临时Redis仅16379端口，用测试随机HMAC前缀隔离，不使用FLUSHDB；验证后SHUTDOWN NOSAVE关闭，未操作已有服务。
- 本次未新增配置项、未改变数据库Schema/API/前端合同，未commit、push、部署或重启已有服务。

最终回归命令：
```sh
env SEEKINO_TEST_AUTH_REDIS_URL=redis://127.0.0.1:16379/15 .venv/bin/pytest tests/server/test_caller.py tests/server/test_bootstrap.py tests/server/test_auth.py tests/server/test_query_limits.py tests/server/test_cloud_access.py tests/server/test_database.py tests/server/test_cli_elasticsearch.py tests/server/test_local_services.py tests/server/test_tenant_index_migration.py -q --tb=short
```

## 2026-10-10 补充：进程总装配与模块子装配

按用户本次要求，新增 `server/main.py` 作为进程总装配入口。此段更新上一段的装配归属：Bootstrap 现在只装配业务；Caller 增加资源聚合类；进程总生命周期转由 Main 管理。上一段保留为前一轮交付记录。

```text
main.main()                              配置、日志、HTTP 进程启动
└── main.create_http_application()        HTTP 生命周期绑定
    ├── main.init_application()          配置检查与总装配
    │   ├── caller.init()                MySQL → Redis → ES
    │   │   └── CallerResources          共享客户端及失败清理
    │   └── bootstrap.build_application_services()
    │       └── ApplicationServices      Repository、Service、Agent、Consumer
    ├── api.app.build_app()              HTTP 中间件、异常处理、路由
    └── ApplicationRuntime               启动业务；先关闭业务，再关闭 Caller
```

Caller 是基础设施子装配，Bootstrap 是业务子装配；两者都不反向依赖 Main。配置检查发生在客户端创建前，业务构造失败先释放已登记业务对象，随后释放客户端；HTTP 绑定失败和 lifespan 启动失败也回收 Runtime。正常关闭顺序为 Consumer、Agent、Realtime、模型 Factory、Tracing、Usage，再 ES、Redis、MySQL；关闭失败仍继续清理，重复关闭不会重复调用。

CLI 是另一个一次性进程入口。其资源上下文明确指 `caller.mysql.open_database_storage()` 和 `caller.elasticsearch.open_elasticsearch_storage()`：`init-db` 仅 MySQL，`init-es-index` 仅 ES，`search` 使用 MySQL、ES 和查询编码器，退出 `with` 即释放。CLI 没有启动 HTTP、后台 Consumer 或登录 Redis；本轮 CLI 仅补充模块职责说明，具体上下文已在上一轮实现。

### 总装配实际修改树

```text
Seekino/
├── server/
│   ├── [A] main.py  ← 进程总装配与 ApplicationRuntime
│   │   影响：统一配置检查、启动、构造失败和停机资源回收
│   ├── [M] bootstrap.py  ← 业务子装配，消费 CallerResources
│   │   影响：只创建及关闭业务对象，保留现有 Agent／Realtime 行为
│   ├── [M] cli.py  ← 说明一次性命令按需装配职责
│   │   影响：命令及资源上下文行为保持
│   ├── caller/
│   │   ├── [A] base.py  ← caller.init 与 CallerResources
│   │   │   影响：MySQL、Redis、ES 子装配及失败清理
│   │   └── [M] __init__.py  ← 导出 init 和资源类型
│   │       影响：导入不创建全局客户端
│   ├── api/
│   │   ├── [M] app.py  ← build_app 负责 HTTP 绑定，保留 create_app
│   │   │   影响：外部注入 services 时不接管生命周期
│   │   └── [M] __main__.py  ← 委托 main
│   │       影响：原 python -m server.api 参数保持
│   ├── design/
│   │   └── [M] user-tenant-trd.md  ← 更新装配和资源归属
│   │       影响：保持设计与实现一致；该目录仍被原 gitignore 忽略
│   └── report_docs/
│       └── [M] user-tenant-handoff-20261010.md  ← 本补充记录
│           影响：记录实际范围、验证及已存在失败
├── tests/
│   ├── manual/
│   │   └── [M] server_mq_once.py  ← 使用 Main 运行时
│   │       影响：一次诊断退出释放业务和客户端
│   └── server/
│       ├── [A] test_main.py  ← 总装配及 HTTP 生命周期回归
│       │   影响：10 个用例保护失败清理、配置传递和启动参数
│       ├── [M] test_bootstrap.py  ← 适配 Main 并检查模块依赖隔离
│       │   影响：保留业务装配、停机顺序和注入所有权验证
│       └── [M] test_caller.py  ← 基础设施聚合及失败清理回归
│           影响：新增 5 个用例，caller 共 22 个用例
└── [M] README.md  ← 装配调用链和 CLI 资源说明
    影响：说明入口和模块分工
```

实际范围14份文件：生产7、测试4、文档3；新增3、修改11，与预计修改树一致。工作区已有身份、Agent、解析及前端并行修改；本轮统计以独立文件快照比较，不将相对HEAD的累计diff当作本轮改动。快照在caller并行写入时捕获，统计已校正base.py原不存在、__init__.py原仅一行模块说明的事实。

本轮改动统计：14文件，+702/-186行（git diff --no-index --numstat逐文件快照比较，含文档）。生产代码+273/-148行；测试+326/-36行。

相关已跟踪路径的git diff --stat快照为7文件、+346/-234行，包含此前身份实现及并行修改，不能视为本轮独占统计。main.py、caller目录、test_main.py、test_caller.py和report_docs仍未跟踪，TRD仍被忽略；git diff --cached --name-only为空。本轮逐文件快照numstat已计入这些本次改动。

### 总装配验证与边界

最终定向回归：**338 passed，零 skip**。覆盖 Main、Caller、Bootstrap、CLI、V2 Worker、邮箱认证、真实 Redis 额度、cloud 权限、业务接口鉴权、公开课程、cloud Agent Runtime、SSE、本地服务脚本。认证与额度使用16379端口的独立Redis，MySQL使用隔离SQLite或SDK替身，ES、邮件、模型使用替身；没有连接现有MySQL／ES或真实SMTP。

```sh
env SEEKINO_TEST_AUTH_REDIS_URL=redis://127.0.0.1:16379/15 .venv/bin/pytest tests/server/test_main.py tests/server/test_caller.py tests/server/test_bootstrap.py tests/server/test_cli_elasticsearch.py tests/server/test_video_parse_worker.py tests/server/test_auth.py tests/server/test_query_limits.py tests/server/test_cloud_access.py tests/server/test_business_api_auth.py tests/server/test_cloud_library_access.py tests/server/test_cloud_agent_runtime.py tests/server/test_sse_events.py tests/server/test_local_services.py -q --tb=short
```

另一次包含test_api.py的四组回归为 **166 passed、12 failed**。12项失败属于该测试的旧Agent Stub：仍调用get_draft_catelog、read_trieve_item、update_draft_item、update_video_item，与当前并行开发的Canvas工具契约不一致。用本轮基线旧HTTP工厂重跑test_agent_conversation_send_and_list_messages，仍返回502并发生KeyError('get_draft_catelog')，证明该失败已存在且与总装配改动无关。未修改职责外的Agent代码或旧Stub。此项TODO归Agent Canvas／API测试维护方，需要更新替身后重跑这12项。

Main/API/CLI 的 `--help` 均通过，原启动命令、端口、日志参数和create_app工厂保留；新增main模块也可直接运行。相关已跟踪文件git diff --check及新增文件空白检查通过，独立Review未发现本次目标内的实质性问题。

低风险观察：Bootstrap在Video Runtime／Supervisor创建后、Agent接管前若配置读取或Agent构造失败，尚未单独关闭这两个Runtime。已检查实际模型适配器构造时没有独立网络Session，共享Executor由Factory清理，当前不造成外部资源泄漏；后续扩展Runtime构造资源时需补所有权转交及失败清理。本次没有扩展该业务契约。

本轮没有引入配置项、API／Schema／前端变更、DI框架或全局客户端。未执行commit、push、现有数据库迁移、部署或已有服务重启；临时Redis验证后已关闭，未使用FLUSHDB。暂存区为空，保留原有未跟踪文件，未修改gitignore。

## 2026-10-10 补充：隔离提交与推送验证

用户本轮明确授权提交并推送自己的实现。此前各节保留为执行时的工作区快照；本次发布候选以2548ee13d152afe1fda411aa6f535538dc4ef14b为父提交，在独立检出中整理游客与邮箱验证码登录、User/Tenant归属、权限与额度、前端身份流程、caller生命周期和main总装配。

共享文件按职责选择内容：Agent、engine/rag和gateway/tracing只提交HEAD原文上的Tenant合同更名；Bootstrap、AgentChat、Upload和VideoParse相关共享文件保留身份与装配逻辑，剥离并行实时识别、Canvas及模型供应商改动。发布时间的Bootstrap没有尚未提交的Realtime装配；原工作区保留实时识别集成。此前用户要求的ORM时间字段Mixin清理一并纳入，避免共享模型留下失效依赖。

前端保留身份与Tenant改动，剥离并行Token上限调整及7项相关测试；未将验证环境、缓存、构建输出、真实环境文件或被忽略的TRD加入提交。原工作区并行文件保持原内容，提交通过精确候选patch暂存。

验证针对实际发布候选，而不是包含其他Agent修改的工作区：

- Python完整回归：2573 passed、65 skipped、零失败；使用隔离SQLite／SDK替身和16379端口独立Redis。跳过项为未配置外部集成条件。
- Python首轮定向回归：421 passed、1 skipped，包含HTTP、Main、Caller、Bootstrap、认证、权限、Schema、Repository、Agent身份隔离和真实Redis额度。
- 前端独立HEAD加41份身份候选：19文件、222项单元测试通过；TypeScript与Vite生产构建通过。
- 候选中的test_api.py全部通过。此前12项旧Agent工具替身失败来自未提交的并行Canvas工作，不存在于这份独立提交。
- 暂存diff空白检查通过；原python -m server.api启动入口和参数可用。没有执行现有数据库迁移、部署或已有服务重启。

候选完整Python回归命令：
```sh
env SEEKINO_TEST_AUTH_REDIS_URL=redis://127.0.0.1:16379/15 .venv/bin/pytest tests -q --tb=short
```

发布范围统计：206 files changed, 8934 insertions(+), 2131 deletions(-)。精确提交清单及基线证据保存在/private/tmp/seekino-identity-publish；分支和提交号以本次Git提交及推送回执为准。

推送准备期间另一并行任务已发布2d5ce92（ContextCanvas）。本次提交已接到该父提交，保留已发布Canvas流程，合并Tenant字段并适配API模型替身；合并后必要回归test_api/test_main/test_caller/test_bootstrap为142 passed、零失败。上述2573项全量结果对应合并前隔离候选；未在新父提交上重复全量。未纳入未提交Realtime或其他provider修改。
