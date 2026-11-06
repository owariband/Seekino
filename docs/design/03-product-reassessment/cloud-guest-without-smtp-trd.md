# 1. 问题与现状

需求：云端缺少 SMTP 时允许游客服务启动，邮箱认证明确不可用。日期：2026-11-06。
分支：`fix/cloud-guest-without-smtp`；[修复 PR #62](https://github.com/owariband/Seekino/pull/62)；[main 发布 #63](https://github.com/owariband/Seekino/pull/63)。依赖 [云部署 TRD](cloud-docker-deploy-trd.md)、
云端运行配置续作 #61；当前任务状态以 [管理工作台](../../dev/workbench.html) 为准。

CPU main `0282fcb0` 的 MySQL、Elasticsearch、Redis 已健康，初始化退出码 0，
但 API 在 `AuthService.__init__` 因 SMTP 为空抛出 ValueError 并重启。
`deploy/cloud/env.cloud.example` 明确说明 SMTP 留空可只开游客，实际构造器与该合同冲突。
本地 `.env.local` 的 SMTP 和管理员邮箱均为空，不能通过复制配置满足当前强制门槛。

本次只修正身份服务对可选邮件能力的装配与调用门槛。保留 cloud 模式、签名密钥、Redis、
CSRF、游客限流和权限校验；不改为 local 身份，不设置虚假 SMTP，不发送验证码邮件。

# 2. 影响因素、假设与评价指标

| 因素 | 现状与假设 | 验收 |
| --- | --- | --- |
| SMTP 被视为整个认证服务必需依赖 | 构造器在创建游客前就抛错 | 无 SMTP 的 cloud 服务可启动、创建并认证游客 |
| 邮箱验证码依赖邮件能力 | 不能把无法发信当作成功发送 | 无 SMTP 且无注入发送器时，发送/验证均明确 503 EMAIL_UNAVAILABLE |
| 游客能力仍依赖签名、Redis 和权限 | 只放松邮件依赖，不放松身份合同 | 密钥/Redis 必需、CSRF/权限回归通过，禁用邮件不写验证码或登录用户 |
| 有 SMTP 或注入发送器的原路径 | 原配置必须继续工作 | 既有邮件、会话及隔离 SQLite 回归通过 |

# 3. 改造方案与建模

AuthService 只在有 SMTP host 或显式注入发送器时持有邮件发送器；没有邮件能力不阻断
bootstrap/create_guest/authenticate。发送与验证邮箱代码在校验游客身份后检查邮件能力，
缺失时返回已有 EMAIL_UNAVAILABLE 错误码与明确文案，在访问验证码状态前失败。
配置 SMTP 时继续使用现有 SMTPEmailSender，保留原发送失败清理及竞争处理。

先补直接覆盖真实启动故障和 HTTP 边界的测试，再运行相关认证/权限/初始化回归，
以独立候选镜像在 CPU 验证；通过发布 PR 合入 main 后部署正式镜像，不在服务器补丁源码。

# 4. 对照实验与结果分析

基线：CPU main 的初始化成功，API 在 AuthService 构造时失败，Web 被健康依赖阻止启动。
计划比较无 SMTP 服务创建与 HTTP 游客会话、无邮件能力的发送/验证、既有配置邮件路径。
使用隔离 SQLite 与 StubRedis，避免访问开发数据或发送真实邮件；真实 CPU 验证只创建测试游客。
测试结果、候选镜像、发布 main 和运行结果待回填，不以单测替代部署证据。

# 5. 试点验证与方案取舍

CPU 新生产空库试运行：健康检查、游客 cookie/CSRF、公开资料只读、管理拒绝以及邮箱明确 503。
不导入素材，不迁移现有库，不调用未接通的 GPU 模型。若失败保留数据库卷，停止候选 API，
修复后重建；不能恢复到绕过 cloud 权限的配置。自签 HTTPS 与真实邮箱/GPU/public 验收分开记录。

# 6. 结果验收与闭环

待验证：直接回归、相关权限回归、CPU 候选运行、main 发布和最终健康状态。
完成标准是无 SMTP 的游客应用可用且邮箱接口明确拒绝；不代表真实邮件可用、模型链路完成、
样例数据已迁移或公网证书可信。后续结果回填本节，进度统一在管理工作台维护。

2026-11-07 实测：新增 HTTP 用例在旧构造器复现 SMTP ValueError；修复后通过，验证游客
Secure/HttpOnly cookie、稳定身份、CSRF 拒绝、邮件 send/verify 的 503、没有验证码 Redis 写入
且没有邮箱用户创建。组合认证/访问/资料权限/初始化检查 99 passed、7 skipped（真实隔离 Redis
未配置）；另外 8 项 cloud Agent 用例因测试夹具缺少 WebSearcher.max_query_chars 失败，
在未修改的基线同样 8 failed，列为既有测试缺口，不宣称全套通过。

候选 `f269f1ecd4ea2e2ae957a22c0abf50b8908a240b` 在 CPU 构建与起栈均退出 0，
API/Web/MySQL/ES/Redis 五个服务 healthy。HTTPS 首页、/healthz、parseConsumer running、
游客会话、公开空资料库通过；带 tenantId 的管理读取 403，缺 CSRF 的邮箱请求 403，
正确 CSRF 的邮件 send/verify 均 503 EMAIL_UNAVAILABLE，跨站创建游客 403。
首次实机脚本漏传管理接口必需 tenantId 得到 422，补齐参数后验证权限，未修改接口。
证据：本机 `outputs/ops/cloud-runtime/candidate-runtime-verification.json`，服务器
`~/seekino-cloud-runtime/runtime-verification.json`。未发送邮件、未调用模型；#63 合并与
正式 main 镜像切换待执行，不能把候选镜像当成最终 main。
