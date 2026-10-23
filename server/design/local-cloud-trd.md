# Seekino Local / Cloud 设计

更新日期：2026-10-21。

本文按**地址、凭证、数据源**三个维度记录已确认的产品方向、当前实现与待办。品牌使用 Seekino；既有 `ANIVAG_*` 环境变量、Cookie 名及存储标识保留兼容，不在本设计中批量更名。

状态：当前先打通云网页的游客浏览与邮箱登录体验。本地工作台接入官方云是后续方向，桌面封装和数据同步延后。原 `5173 → 9621` 和 `.env.local` 保持 local 管理员，原视频资料可读；Nginx `https://10.254.47.100:8443` 连接独立 cloud API 9622，使用私有 phone 配置及独立数据库、Chunk/Event 索引和媒体目录。浏览器已确认 local 原页面恢复、cloud 游客可进入正常页面，验证码注册、再次登录、权限和退出共通过 31 项检查。用户已反馈完成 iPhone 证书安装，手机实际访问与视频业务仍待验收。公司的证书未修改。操作方式见[原生 HTTPS 部署说明](../../deploy/nginx/README.md)。

## 1. 已决定的边界

| 维度 | 回答的问题 | 与其他维度的关系 |
|---|---|---|
| 地址 | 浏览器向哪个 origin 发请求，服务监听哪些网卡和端口？ | 部署在电脑或云服务器、是否 Docker、是否能被别人访问，由实际网络配置决定 |
| 凭证 | 这个后端如何识别请求者，并允许其做什么？ | `local/cloud` 是后端信任策略；登录哪个实例，就使用那个实例签发的凭证 |
| 数据源 | 上传、浏览、检索和修改的是哪个实例、哪个 Tenant 的资料？ | 选择远端资料不等于复制到本地；登录也不等于同步 |

`ANIVAG_AUTH_MODE=local` 表示信任能够访问该后端的使用者，以固定本机 owner 身份操作。当前代码使用 `local-user/local-default`，权限等同本地管理员。这个“无账号 owner”与云端 guest 不同：前者可以管理本机资料，后者只能按服务端规则访问公共内容。

`ANIVAG_AUTH_MODE=cloud` 表示请求必须经过 guest 或邮箱账号的身份和资源权限检查。它既可运行在云服务器，也可运行在电脑上做联调；把本地 Docker 的 mode 改为 cloud，不会自动获得公网 IP、域名或开放端口。mode 与 `ANIVAG_ENVIRONMENT`、模型来源及模型运行位置独立。

服务端绑定 `127.0.0.1` 仅接受本机网络连接；绑定内网 IP 或 `0.0.0.0` 会扩大可达范围，还受 Docker 端口映射、防火墙和网络路由影响。Docker 内监听所有接口与宿主机发布端口是两个边界，宿主机映射到 `127.0.0.1:端口` 才限定为本机入口。

浏览器地址中的 `localhost` 只说明页面入口在本机，不能证明它没有请求远端 backend；反之，前端由远端提供也不能证明数据存在哪里。应用必须显式展示当前连接和资料来源，后端必须独立执行权限，不能相信前端传来的“我是 local”。

当前默认 API 为 `127.0.0.1:9621`，Vite 为 `127.0.0.1:5173`，Vite 将 `/api` 转发给 API。临时开放 local 页面给其他设备，也会把本机 owner 权限开放给这些可达设备；需要多用户隔离时使用 cloud 策略。

源码：[AuthService](../service/auth.py)、[资源权限](../service/access.py)、[AuthConfig](../config/auth.py)、[API 监听](../main.py)、[Vite 代理](../../web/vite.config.ts)。

## 2. 本地工作台与云端服务

已确认的产品方向是本地独立可用、可选登录云账号：用户可以在本机打开 Web 工作台并使用本机资料，不必先有官方账号；之后可连接官方云，浏览云端资料，未来按明确操作上传或同步数据。本地 Docker 也可以承载独立的 Web 实例，不要求只有桌面端才可本地使用；当前仓库 Compose 只运行基础依赖，API/Web 仍在宿主机运行，不把完整应用容器化写成已交付。

本地 backend 和云端 backend 各自拥有身份策略、数据库及媒体。接入云端时，用户连接的是已存在的远端服务，不要求每个用户额外在本机启动一个“云 server”。本地 owner 登录云账号后，本地资料也不会自动归属给该云账号。

当前前端已经具备固定 `PRIMARY_CONNECTION`、一个可选 `REMOTE_CONNECTION` 和独立账号状态，可区分 local/public/personal 资料源；设置页可以展示连接状态并重连。这是现有能力，不应描述为完全没有远端连接。

完整的任意 provider 增删、持久连接管理、官方连接可信标识、跨实例复制/迁移和同步协议尚未实现。这里的资料服务 provider 与模型 provider 是不同概念。现有 `/remote` 仅是 Vite 开发代理，限定 loopback 请求，不是生产代理或通用的手机接入路径。

后续 provider 设计遵循以下约束：

- 每个连接分别维护 origin、身份、Cookie/令牌、CSRF 状态、缓存及资料范围；切换连接不能把原实例凭证发送给另一个实例。浏览器 Cookie 不按端口隔离（[RFC 6265](https://www.rfc-editor.org/rfc/rfc6265.html#section-8.5)），不能只靠同一主机的不同端口隔离账号；后续须明确独立主机名或代理 Cookie 隔离方案。数据库、查询缓存或文件按实例分区，不应宣称为能抵御恶意实例的安全沙箱。
- 上传前明确目标实例与 Tenant；读取远端数据、上传副本、持续同步是不同操作。同步另行定义版本、冲突、删除传播和断线重试，当前不承诺全量双向同步或 CRDT。
- 后端自报 `mode=cloud`、实例名称或自托管证书不能证明“官方”。官方身份需要产品确定的官方 origin 和匹配该 origin 的可信 HTTPS；自托管作为独立信任来源展示，不能复用官方凭证。不同后端使用独立签名 secret。

源码：[连接配置与请求](../../web/src/api/client.ts)、[身份和资料源](../../web/src/features/auth/AuthProvider.tsx)、[设置页](../../web/src/features/settings/SettingsPage.tsx)。

### 2.1 Agent 运行方式的产品目的

2026-10-21 已确认：**本地由用户连接自己的 Codex / Claude Code；云端由 AniVAG 提供统一 Pi harness。** 这是两种部署场景的职责决定，不能由浏览器地址或 `auth.mode` 自动推断已经完成运行时切换。

| 场景 | 目标运行方式 | 进程与资源责任 |
|---|---|---|
| 本地 | 用户自行安装、登录并连接 Codex / Claude Code；不要求 Pi | 原生 runtime 按自身机制运行，资源由用户本机承担；AniVAG 负责接入及自身连接的生命周期 |
| 云端 | AniVAG 通过一个 Node Pi Host 提供长对话、长工具调用和复杂任务链；用户可配置自己的模型 API | AniVAG 负责进程复用、凭据与对话隔离、并发限制、缓存上限、取消和恢复 |

云端资源目标以 4 CPU / 8 GB 服务器为初始验收环境，避免为每个用户或对话启动一个完整 pi / Codex 进程。一个 Conversation 绑定独立持久上下文；一次性 subagent 执行结束释放，不建立长期业务会话。共享 Host 不等于共享用户 API Key。

Server 负责模型配置的用户归属、凭据保存与读取授权、配置版本，以及现有 Conversation / Message 的持久化与幂等。Gateway 接收已授权的配置和执行上下文，负责 Pi 连接及运行时调度。当前全局 YAML / 管理员设置不等于已经实现用户级 BYOK 配置；该配置管理及真实容量仍待开发和验收。

首版部署设计为一个 Python API 进程管理一个 Node Pi Host。未来增加 API workers 时，应独立托管共享 Host 并通过本机 Socket 连接，不能每个 worker 各自启动一份而仍宣称全机单 Host。本地原生 runtime 的内部进程优化不纳入这项云端工作。

本地接入见 [Bridge TRD](../../gateway/bridge/design/codex-bridge-trd.md)，云端目的、源码依据、Host 协议与验收见 [Pi Host TRD](../../gateway/pi/design/pi-host-trd.md)。本次仅确认并记录设计，没有切换实际运行配置或启动新服务。

## 3. 当前云网页身份流程

页面、guest 会话和邮箱账号分开处理。首次 `/me` 的 401 是身份引导分支，不是 Web 页面被 HTTP 登录框挡住；前端取得可用身份后再进入资料页面。

1. 浏览器加载页面，前端 `GET /api/auth/me`，通过 `credentials: include` 携带本站已有 Cookie。
2. 没有有效凭证时返回 401，前端再 `POST /api/auth/guest`。后端检查请求来源、创建匿名 User，签发 guest Cookie，并返回身份、权限及 CSRF token。重复引导保留有效的现有身份。
3. guest 默认没有个人 Tenant，只访问平台已发布、解析和媒体均就绪的公共系列。是否可检索及额度由服务端控制。
4. 用户填写邮箱，`POST /api/auth/email/send` 携带 guest Cookie 和 `X-CSRF-Token`；后端生成六位随机验证码，通过 SMTP 发送。
5. `POST /api/auth/email/verify` 校验同一挑战。新邮箱将当前 guest 升级为正式用户并创建个人 Tenant；已有邮箱登录现有账号，按既有规则迁移当前 guest 对话。无需单独的密码或注册接口。
6. 后端签发 email 身份凭证，以同名 Cookie 覆盖 guest Cookie。前端更新账号与资料源状态，可以选择公共资料或自己的个人资料。
7. 业务 API 验证 token 签名和有效期，再查询 User 的 ACTIVE 状态、`auth_version` 和身份等级，检查 Tenant/资源权限；写请求另校验 CSRF。前端隐藏按钮不能替代这些检查。
8. 退出登录撤销该账号旧版本凭证，签发新的 guest Cookie。当前是用户 `auth_version` 级撤销，不是单独删除某个设备的服务端 session。

当前 token 是签名凭证，服务端不持久保存每一枚 token；User/归属保存在数据库，验证码及限流状态保存在 Redis。JWT、验证码、查询并发租约分别有不同有效期：

| 参数 | 当前默认值 | 语义 |
|---|---|---|
| `ANIVAG_AUTH_TOKEN_TTL_SECONDS` | 604800 秒（7 天） | Cookie 中登录凭证的有效期 |
| `ANIVAG_AUTH_OTP_TTL_SECONDS` | 300 秒（5 分钟） | 单次验证码挑战有效期 |
| `ANIVAG_AUTH_OTP_ATTEMPT_LIMIT` | 5 次 | 当前验证码错误达到上限后删除挑战 |
| `ANIVAG_AUTH_SEND_COOLDOWN_SECONDS` | 60 秒 | 同一邮箱的重发冷却，另有邮箱/guest/IP 窗口限流 |
| `ANIVAG_AUTH_QUERY_LEASE_SECONDS` | 600 秒 | 查询并发占用的过期保护，不是登录或验证码时长 |

Redis 保存验证码的 HMAC 摘要，不保存明文验证码。成功验证原子消费挑战；重新发码覆盖该 guest/邮箱的旧码并重新计时；发送失败清理本次挑战。挑战绑定 guest ID、auth_version 和邮箱，因此须在申请验证码的同一浏览器会话、同一站点地址完成验证，可以在另一台设备查看邮件内容。

源码：[前端身份引导](../../web/src/features/auth/AuthProvider.tsx)、[Auth API](../api/auth.py)、[验证码与凭证](../service/auth.py)、[注册和登录事务](../repository/identity.py)、[业务请求身份](../api/context.py)。更详细的归属规则见[用户租户设计](user-tenant-trd.md)。

## 4. SMTP 与本地模拟邮件

当前运行路径统一使用 `SMTPEmailSender`。SMTP 的 host、port、username/password、sender、SSL 和 STARTTLS 均可配置；cloud 启动要求可用 Redis 和 SMTP 配置，不存在固定万能验证码或运行时跳过验证开关。测试代码的 `email_sender` 注入仅用于测试。

Mailpit 用 SMTP 接收验证码并提供本地收件箱，不投递到收件人的真实邮箱。它替换邮件接收端，仍经过随机生成、Redis、限流、CSRF 和登录事务，适合先验证流程；能读取模拟收件箱不证明拥有填写的真实邮箱。

仓库已有可选 `mock-email` Compose profile，SMTP/UI 仅发布到本机 1025/8025，邮件不配置持久卷。当前手机测试使用原生 Mailpit 和独立 Redis，由 `deploy/cloud-auth/run.py` 管理，均只监听 loopback；邮件和 Redis 持久状态保留在忽略的项目运行目录。该脚本只重启独立 `phone-api`，不接管原 local API；原生 MySQL/Elasticsearch 进程复用，但手机测试使用独立库、Chunk/Event 索引、媒体目录和 Redis DB 1。它与原生 Nginx 无耦合，Nginx 不负责发信。

宿主机上的 Server 连接本地 Mailpit 使用以下配置，真实 SMTP 配置默认保持不变：

```dotenv
ANIVAG_SMTP_HOST=127.0.0.1
ANIVAG_SMTP_PORT=1025
ANIVAG_SMTP_USERNAME=
ANIVAG_SMTP_PASSWORD=
ANIVAG_SMTP_SENDER=noreply@seekino.test
ANIVAG_SMTP_USE_SSL=false
ANIVAG_SMTP_START_TLS=false
```

`START_TLS` 默认是 true，模拟收件箱须显式关闭。真实 SMTP 可使用隐式 SSL 或 STARTTLS，但两者不能同时启用；浏览器到 Nginx 的 HTTPS 与 SMTP 自身的 TLS 是两条独立连接。

源码与操作：[邮件配置](../config/email.py)、[SMTP 发送](../utils/email.py)、[本地模拟邮件说明](../../deploy/README.md#本地模拟验证码邮件)。Mailpit 的接收和存储行为见[官方文档](https://mailpit.axllent.org/docs/configuration/runtime-options/)。

## 5. HTTPS 与独立 Nginx 入口

已决定由独立 Nginx 进程提供 TLS 和反向代理，不把证书处理塞进认证业务。浏览器先验证证书覆盖当前访问域名/IP 且签发链受信任，再建立加密连接；Nginx 终止 TLS，直接返回 `web/dist` 的生产构建静态文件，把 API 请求转发给 loopback 上的 Python API。应用服务、Redis 和 SMTP 可以分别运行，Nginx 不依赖 Docker。

```text
浏览器 ── HTTPS ──> Nginx
                    ├── /api/...、/healthz ── HTTP loopback ──> Python API
                    └── 页面/静态资源 ──> web/dist（生产构建）
```

本地入口默认只监听 `127.0.0.1:8443`。证书脚本先用 OpenSSL 生成独立 CA，内部主题为 `O=Seekino / OU=Local Development / CN=Seekino Local Development CA`；再以自定义 CSR 和 mkcert 签发 `CN=Seekino Local HTTPS` 的服务器叶证书。根 CA 位于 `outputs/local-services/nginx/seekino-ca`，该目录也是项目隔离的 `CAROOT`；叶证书位于 `seekino-certs`，公共根证书另外导出为 `outputs/local-services/nginx/Seekino-Local-Dev-CA.cer`。CA 私钥、服务器私钥、PID 和日志均保留在已忽略的项目状态目录，不进入 Git。

证书生成不安装系统信任，也不自动替换运行中进程的证书。Mac 推荐将 `.cer` 导入钥匙串的“系统”，核对指纹后仅对 SSL 设置“始终信任”，其余用途保持未指定或系统默认；顶部自动显示“使用自定义设置”属于正常现象，不将顶部菜单设为“始终信任”，也不使用自动安装信任功能。iPhone 先安装该 `.cer` 描述文件，再到“通用 → 关于本机 → 证书信任设置”只为该根 CA 开启完整信任；Mac 的信任不会自动同步到手机。服务器 SAN 必须覆盖实际访问的内网 IP/域名，只分发公共 `.cer`，不分发任何私钥。具体生成和导入步骤见[部署说明](../../deploy/nginx/README.md)，mkcert 对 CSR 与 CA 的支持见[官方说明](https://github.com/FiloSottile/mkcert)。

Seekino 的 O/CN 只是内部显示名称，没有为 CA 加上名称范围约束；它并非只能为 Seekino 签发证书。Mac 仅信任 SSL 缩小的是信任用途，不是域名范围。旧 `outputs/local-services/nginx/ca`、`certs` 仅保留用于核对和必要回退，普通启动不使用；撤信任时精确核对旧测试 CA，公司的根证书及管理配置不在处理范围内。

本轮 Nginx 1.31.7 已 stop/start 绑定 `10.254.47.100:8443`，只代理独立 cloud API 9622，新叶证书 SAN 包含该 IP；系统 curl 验证 HTTPS 通过。cloud 无 Cookie 的 `/me` 返回 401，游客引导后正常显示页面；真实 HTTPS OTP 注册、Redis 300 秒过期、SMTP 捕获和退出撤销均已验证。原 local API 9621 及 `.env.local` 已恢复原状态，Vite 5173 保留浏览器 Host/端口，仍免登录使用本机管理员。Nginx 内网启动拒绝代理无凭证即可访问的 API，避免误发布 local。新 CA 仅对 SSL 始终信任；旧项目测试 CA 已撤信任，公司的证书未修改。用户已反馈完成 iPhone CA 安装，手机实际访问及视频上传/播放仍待验收。

内网手机联调额外指定 `ANIVAG_HTTPS_BIND=<电脑内网IP>`，配合 cloud 策略及独立测试入口；具体脚本的限制和命令以部署说明为准。电脑与手机处于同一公司网络不保证互通，还需路由、Wi-Fi/VLAN 隔离及防火墙放行该端口。手机的 localhost 指手机自身。

正式云入口使用域名和公信 CA 签发的证书，并安排续期；本地 CA 不能作为面向陌生用户的正式证书方案。TLS 证明连接到证书对应地址并保护传输，不授予管理员权限，也不把自托管站点变成官方站点。TLS 配置原则见 [Nginx HTTPS 官方文档](https://nginx.org/en/docs/http/configuring_https_servers.html)。

Cookie 当前是 `HttpOnly; SameSite=Lax; Path=/`，`Secure` 由 `ANIVAG_AUTH_COOKIE_SECURE` 控制，默认 true。HTTPS 联调和正式部署保持 true；仅临时普通 HTTP 的内网测试可显式设 false。LAN IP 的普通 HTTP 不能依赖浏览器发送 Secure Cookie，不能将该失败误认为验证码或 Redis 故障。属性含义见 [Set-Cookie 文档](https://developer.mozilla.org/en-US/docs/Web/HTTP/Reference/Headers/Set-Cookie)。

代理必须保留以下边界，并按实际请求验收：

- **API 前缀**：外部 `/api/auth/guest` 到后端仍是 `/api/auth/guest`。`proxy_pass` 附带 URI 会改变路径替换规则，不能意外剥掉 `/api`，见 [Nginx proxy_pass](https://nginx.org/en/docs/http/ngx_http_proxy_module.html#proxy_pass)。
- **Host 与协议**：保留客户端使用的 Host，包括 8443 等非默认端口，正确传递原始 HTTPS 协议。guest 创建严格比较 Origin 与后端所见 scheme/host/port，端口丢失或 HTTPS 被误识别为 HTTP 都可能导致 403；扩大 CORS 不能替代此检查。
- **客户端 IP**：边缘 Nginx 设置可信来源的转发头；API 仅信任指定代理传来的 Proto/IP，不能无条件采信外部任意 `X-Forwarded-*`。否则 guest/IP 限流及审计归属会失真。
- **流式与上传**：同源代理覆盖媒体、上传和 SSE；超时、请求体大小及 buffering 按部署入口验证，不以首页打开代替业务全链路验收。更新 HTTPS 页面前须重新构建 `web/dist`。

## 6. 公共与私有资料的当前差距

当前 cloud 的访问范围是平台公共 Tenant 加本人个人 Tenant。平台管理员只能将平台公共 Tenant 下、媒体和解析就绪的系列设为 `is_published`；公共列表和读取会检查发布状态。普通用户可在个人 Tenant 上传和管理资料，但不能把个人系列切换为别人可访问的 public。

因此，“少量用户邮箱注册、上传并自行设置系列 public/private”仍有功能缺口，不是只等部署测试。未来最小语义是：系列保持原所有者；private 仅所有者可访问；public 可供约定的访客读取；发布不授予其他人修改权；撤回应同时关闭列表、检索和媒体读取。具体权限与索引撤回方案另行实现，不能只增加前端开关或挪到平台 Tenant 冒充完成。

算法与工程可并行，但必须共享资源 ID、所有权、可见范围和当前索引版本；检索候选回源、媒体服务、缓存和后台索引发布都遵守同一授权。同步、团队成员与共享编辑不在当前迭代内。

源码：[系列发布 API](../api/library.py)、[Tenant 与系列读取](../service/access.py)、[公共系列分页](../service/library.py)。

个人系列 public/private 共享与团队范围的最小模型、权限与迁移评估见 [Series Sharing TRD](series-sharing-trd.md)（Series sharing and team scope assessment）。

## 7. 配置约束与分阶段验收

不记录实际 secret、SMTP 授权码和运行中 Cookie。当前源码要求 cloud 签名 secret 至少 32 字节、Redis 可连接且 SMTP 已配置。类默认查询租约是 600 秒，当前 `config.yaml` 的 `execution_timeout_seconds` 是 1200 秒；启动检查要求总超时开启且 execution timeout **严格小于** query lease，不会自动 clamp。手机联调使用私有 `outputs/local-services/cloud-auth/phone.env`，设置 1800 秒租约、独立签名秘密、`seekino_phone_auth` Cookie 和本机模拟 SMTP；不覆盖原 `.env.local`，两个实例 Cookie 不靠端口隔离。

| 阶段 | 验收范围 | 当前边界 |
|---|---|---|
| 当前：云网页访客体验 | 可信 HTTPS、首次 guest、公开内容浏览/检索/播放、OTP 注册与登录、退出、个人资料上传及权限 | 真实内网 HTTPS、OTP 注册和退出已在电脑联调；手机互访与视频/模型业务仍待验收 |
| 下一步：个人系列可见性 | 所有者发布/撤回，公共检索与媒体权限一致 | 普通用户 public/private 尚未实现 |
| 后续：本地工作台连接云 | 本地独立运行、显式连接来源、各实例凭证及缓存隔离 | 固定 primary 加单个可选 remote 已有；完整 provider 管理和生产代理未完成 |
| 延后：桌面和同步 | 桌面封装、选择性上传/同步、冲突与删除规则 | 尚无同步协议或自动同步实现，不纳入当前云网页验收 |

验证时先区分“网络能到”“TLS 可信”“Cookie 留存”“OTP 可用”“业务有权限”五个步骤。Mailpit 验证流程，实际 SMTP 验证邮箱投递与所有权；本地 owner 的成功操作不能当作 cloud 权限已通过。

源码：[cloud 启动约束](../bootstrap.py)、[认证默认值](../config/auth.py)、[当前模型执行配置](../../config.yaml)。原生 Nginx 的启动、证书信任和后续验收记录维护在[原生 HTTPS 部署说明](../../deploy/nginx/README.md)，与本文的架构决定分开更新。
