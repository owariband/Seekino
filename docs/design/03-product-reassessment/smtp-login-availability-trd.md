# 1. 问题与现状

需求：SMTP 留空时，登录入口明确显示尚未开放。日期：2026-11-07。
分支：`feat/email-login-availability`；[PR #65](https://github.com/owariband/Seekino/pull/65)。
上游：[无 SMTP 游客启动 #62](https://github.com/owariband/Seekino/pull/62)，
[原设计](cloud-guest-without-smtp-trd.md)；发布依赖 [main PR #63](https://github.com/owariband/Seekino/pull/63)。
当前任务状态统一见 [管理工作台](../../dev/workbench.html) DEPLOY-04。

CPU 候选服务已能在 SMTP 留空时提供游客身份，但前端 LoginDialog 仍展示邮箱、验证码和
发送按钮；游客完成填写后才收到 EMAIL_UNAVAILABLE。AccountControls 显示“未登录”，
上传提示“登录后上传远端视频”，使用户误以为当前能够注册或登录。AuthIdentityView 尚未
包含邮件能力，前端无法在打开表单前判断。依据为 server/api/auth.py、server/service/auth.py、
web/src/features/auth/LoginDialog.tsx 和 web/src/app/AppShell.tsx。

本次由现有 SMTP 装配状态决定提示，不新增独立开关，不配置或试发邮件，不变更游客、
已登录账号或本地资料权限。邮件服务的网络故障仍沿用现有发送失败提示。

# 2. 影响因素、假设与评价指标

假设：直接从 AuthService 的发送器装配状态提供能力，能够消除前端与后端配置判断分叉。
生产发送器仅在 SMTP host 非空时装配；测试可注入发送器。账号服务器可能独立于当前资料
来源，因此前端必须消费 accountIdentity，不能使用当前本地资料的 identity。

| 指标 | 基线 | 目标 | 测量与证据 |
| --- | --- | --- | --- |
| SMTP 空配置下登录入口 | 始终展示表单，发送后 503 | 展示“登录与注册暂未开放”、游客说明和“继续体验” | 身份 HTTP 与 AuthProvider/App 交互用例，待测 |
| 未开放状态邮件请求数 | 用户可触发无效请求 | 打开及关闭提示过程为 0 | 前端 API mock 计数，待测 |
| SMTP 配置后的原行为 | 邮箱验证码登录 | 表单、冷却与登录保持 | 既有登录回归，待测 |
| 资料权限与来源 | 由服务端身份决定 | 本地上传不受远端邮件能力影响，云游客保持只读 | 混合来源及权限回归，待测 |
| 键盘操作与移动端 | 既有焦点和关闭交互 | 提示可聚焦、关闭、恢复焦点；移动端入口一致 | 交互测试与构建，待测 |

# 3. 改造方案与建模

AuthService 暴露只读 email_login_enabled，复用发送器是否存在的条件。身份 HTTP 响应
统一增加 emailLoginEnabled 布尔值，覆盖 me、guest、verify 和 logout；不返回 SMTP
地址、凭据或发送者配置。此值表示配置已启用，不承诺邮件连通性。

AuthProvider 透传账号能力；LoginDialog 在明确 false 时显示说明而不渲染表单。
AccountControls 和设置页显示“游客模式”，账号入口提示“登录 / 注册（暂未开放）”；
无上传权限的游客入口改为“个人上传暂未开放”，点击仍打开说明。已登录用户及本地
上传不受影响。旧服务响应缺少字段时保留原交互，不将网络断开当作未开放。

SMTP 配置随现有运行配置在启动时加载；配置并重启后，重新取得身份的页面恢复表单。
本次不引入轮询或配置热加载。沿用现有弹窗、焦点管理、国际化与身份响应通道。

# 4. 对照实验与结果分析

先用无 SMTP 身份和对应界面用例确认旧行为，再增加能力字段和条件显示。
使用隔离 SQLite/StubRedis 与邮件替身验证 HTTP；前端复用现有 Vitest 用例，验证
空配置提示、零邮件请求、配置后登录及本地/远端混合来源。执行相关权限回归、类型检查和
生产构建；原始输出保存到 outputs/ops/cloud-runtime/email-login-availability/，结果待回填。

# 5. 试点验证与方案取舍

在现有 CPU 空库候选环境构建前后端镜像，检查真实身份响应、游客权限及网页提示。
只创建必要的测试游客，不调用模型、不发送邮件、不迁移资料。失败可切回已有候选镜像，
保留数据卷和 private env。正式 main 发布仍通过 #63，候选验证与正式发布分别记录。
配置后的真实 SMTP 发信不在本轮验收范围；自签证书和 GPU 接入沿用既有部署限制。

# 6. 结果验收与闭环

待实施与验证。目标是让 SMTP 空配置的游客在填写邮箱之前知道登录注册尚未开放，
且未来配置邮件后恢复原行为。最终回填代码/测试/候选部署证据，不以测试通过代替 main
发布，不以 emailLoginEnabled=true 代替真实邮件可达。当前状态只维护在工作台。
