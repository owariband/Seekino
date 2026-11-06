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
生产构建；原始输出保存到 outputs/ops/cloud-runtime/email-login-availability/。

2026-11-07，候选源码 `f811903f` 的本地验证：

| 检查 | 基线 | 修改后 | 范围 |
| --- | --- | --- | --- |
| 身份能力 HTTP 用例 | 两项因响应缺字段失败 | SMTP 留空 false，配置 SMTP 或注入发送器 true | 隔离 SQLite/StubRedis，不发送真实邮件 |
| 未开放提示用例 | 仍显示邮箱表单，用例失败 | 无输入框，零 send/verify 调用，关闭恢复焦点 | AuthProvider 交互 |
| 身份与权限回归 | 既有行为保留 | 94 passed、7 skipped | 跳过需独立 Redis 的用例，无生产数据库连接 |
| 前端相关回归 | 原验证码登录流程 | 181 passed | AuthProvider、App、API client、SeriesPage、VideoPlayer |
| 类型检查与生产构建 | 已有构建入口 | 均通过 | Node 本地依赖，无依赖版本改动 |
| Chrome 浏览器 | 新的未开放场景 | 390px/1280px 两项通过 | 菜单、设置、弹窗、上传提示、焦点与零邮件请求 |

Playwright 默认浏览器二进制未安装，首次运行两项未执行；改用已安装 Chrome 后通过。
手机截图发现标题最后一行仅一个字，给现有弹窗标题增加均衡换行后重建并复验两项通过。
本次不测吞吐或模型 token；变更不调用模型，也不触发 SMTP 探测。

# 5. 试点验证与方案取舍

在现有 CPU 空库候选环境构建前后端镜像，检查真实身份响应、游客权限及网页提示。
只创建必要的测试游客，不调用模型、不发送邮件、不迁移资料。失败可切回已有候选镜像，
保留数据卷和 private env。正式 main 发布仍通过 #63，候选验证与正式发布分别记录。
配置后的真实 SMTP 发信不在本轮验收范围；自签证书和 GPU 接入沿用既有部署限制。

# 6. 结果验收与闭环

本地实现与验证支持采用：SMTP 空配置的游客在填写邮箱之前看到未开放说明，配置邮件后
恢复原表单。CPU 候选与正式 main 的部署结果按下面记录分别判断，不以测试通过代替 main
发布，也不以 emailLoginEnabled=true 代替真实邮件可达。当前状态只维护在工作台。

2026-11-07 候选部署记录：旧下载转发两次超时；独立转发完整 wheel 下载 200 后，原
Dockerfile 构建成功。首次启动因候选脚本的 umask 077 同时影响源码解压，目录/文件成为
700/600，镜像的非 root 用户读取失败；立即恢复旧候选，API/Web 和三项基础服务均健康。
修正为仅源码解压使用 umask 022，日志和私有配置仍保持原保护，并在切换前追加镜像运行
用户的离线 import 和配置读取预检。这是候选操作脚本修正，不改变仓库源码或依赖合同。

修正后同一源码候选 f811903f 构建和离线预检均退出 0，API/Web 起栈退出 0，五服务健康。
真实 HTTPS 首页和 /healthz 均 200，guest/me 均返回 emailLoginEnabled=false；游客权限
全 false，管理/CSRF/跨站仍为 403，禁用邮件接口为 503，公开库仍为空。公网 Chrome
1280px/390px 均验证菜单、设置、无输入框、继续体验和上传说明；零浏览器邮件请求、
零 pageerror。证据为上述目录下 cpu-runtime-verification.json、cpu-browser-verification.json
与 cpu-*.png。测试显式接受既有自签证书，未发送邮件、未调用模型或导入素材。
CPU 正式源码仍为 main 0282fcb0，当前运行的是本候选镜像；正式 main 切换待 #63。
