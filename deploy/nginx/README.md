# 原生 Nginx 本地 HTTPS

在 macOS 上直接运行 Nginx 和 Seekino，不需要 Docker。Nginx 是独立进程，返回
`web/dist` 静态文件，并将 `/api`、`/healthz` 转给本机 Python API。
这里使用真正的 TLS 和本地开发 CA；只有信任该 CA 的设备才会接受测试证书。

根 CA 的内部名称为 **Seekino Local Development CA**（`O=Seekino`、
`OU=Local Development`），服务器叶证书为 **Seekino Local HTTPS**。
这些名称用于识别证书，不限制 CA 的签发范围；当前 CA 没有域名范围约束，
信任它并不表示“它只能影响 Seekino”。Mac 仅对 SSL 设置始终信任也不限制域名。

```text
浏览器 -- HTTPS :8443 --> Nginx -- HTTP 127.0.0.1:9621 --> Seekino API
                           └── web/dist（构建后的前端）
```

## 本机启动

1. 安装原生依赖：

   ```bash
   brew install nginx mkcert
   ```

   OpenSSL 命令、Python 3 和 Node/npm 复用开发环境。使用 8443 无需管理员权限。
   不需要 `brew services start nginx`；本项目使用自己的配置和 PID，避免接管全局实例。

2. 在仓库根目录构建同源前端：

   ```bash
   (cd web && VITE_API_BASE_URL= VITE_REMOTE_API_BASE_URL= VITE_REMOTE_API_ORIGIN= npm run build)
   ```

   本入口不依赖 Vite。修改前端后需要重新构建；原有 5173 开发服务可继续运行。
   Vite 专用的 `/remote` 开发代理没有包含在此配置中。

3. 生成项目独立的 Seekino CA、服务器证书和供手机安装的 `.cer`：

   ```bash
   ./deploy/nginx/certificates.sh
   ```

   脚本先用 OpenSSL 生成自定义名称的 CA，再通过自定义 CSR 和 mkcert 签发叶证书。
   CA 已存在时复用，名称或文件不完整会报错，不覆盖现场；叶证书会重新签发。
   默认 SAN 包含 `localhost`、`127.0.0.1` 和 `::1`，可在命令后增加实际域名/IP。
   此步骤只生成文件，不安装系统信任、不切换运行中的 Nginx。

   | 文件 | 用途 |
   |---|---|
   | `outputs/local-services/nginx/seekino-ca/rootCA.pem` | PEM 格式的根 CA 公共证书 |
   | `outputs/local-services/nginx/seekino-ca/rootCA-key.pem` | CA 私钥，只保留本机 |
   | `outputs/local-services/nginx/seekino-certs/site.pem` | Nginx 使用的服务器叶证书 |
   | `outputs/local-services/nginx/seekino-certs/site-key.pem` | 服务器私钥，只保留本机 |
   | `outputs/local-services/nginx/Seekino-Local-Dev-CA.cer` | 根 CA 的 DER 副本，用于 Mac/iPhone 导入 |

4. 按原来的方式启动 API 和已有原生 MySQL、Elasticsearch 等依赖；API 保持监听
   `127.0.0.1:9621`。再启动独立的 HTTPS 入口：

   ```bash
   ./deploy/nginx/start.sh
   ```

   默认只监听 `127.0.0.1:8443`，地址为 `https://localhost:8443`。
   脚本不会启动、停止或切换 Python API 的认证模式，也不会读取 `.env.local`。
   API 端口不同时显式传入 `SEEKINO_HTTP_PORT`；HTTPS 端口使用 `SEEKINO_HTTPS_PORT`。
   `start` 也会调用证书生成脚本，始终使用 `seekino-ca` 和 `seekino-certs`，
   不复用旧 `ca/`。已有 Nginx 进程须先 stop 再 start 才会切换到新证书。

5. 让 Mac 浏览器信任新 CA：打开“钥匙串访问”，选择“系统”（System）钥匙串，
   导入 `outputs/local-services/nginx/Seekino-Local-Dev-CA.cer`，按提示进行管理员确认。
   双击 **Seekino Local Development CA**，核对指纹，再展开“信任”：

   - 仅将“安全套接字层（SSL）”设置为“始终信任”。
   - 不将顶部“使用此证书时”菜单设为“始终信任”；修改 SSL 后，顶部自动显示
     “使用自定义设置”属于正常现象。
   - 其余项目，包括基本 X.509、代码签名和邮件，保持未指定或系统默认。

   关闭证书窗口并按提示确认保存，再重新打开浏览器标签页。本方案不使用 mkcert
   的自动安装信任功能，避免写入更宽的信任策略。钥匙串设置方式见
   [Apple 官方说明](https://support.apple.com/guide/keychain-access/change-the-trust-settings-of-a-certificate-kyca11871/mac)。

   不安装系统信任也可以显式验证 CA 和 HTTPS：

   ```bash
   openssl x509 -in outputs/local-services/nginx/seekino-ca/rootCA.pem -noout -fingerprint -sha256
   curl --noproxy '*' --cacert outputs/local-services/nginx/seekino-ca/rootCA.pem \
     https://localhost:8443/healthz
   ```

## 手机内网测试

### 原生 cloud 登录环境

安装原生依赖 `brew install redis mailpit`，不需要 Docker 或 `brew services`。
原 `.env.local` 和 `5173 → 9621` 保持 local 管理员模式。手机使用独立的
`outputs/local-services/cloud-auth/phone.env`（权限 0600，不入 Git），不要将 cloud
覆盖到 `.env.local`。独立配置保留所需模型配置和连接凭证，使用至少 32 字节随机签名秘密：

```dotenv
SEEKINO_AUTH_MODE=cloud
SEEKINO_AUTH_SECRET_KEY=<本机随机签名秘密>
SEEKINO_AUTH_REDIS_URL=redis://127.0.0.1:16379/1
SEEKINO_AUTH_COOKIE_NAME=seekino_phone_auth
SEEKINO_REDIS_PORT=16379
SEEKINO_AUTH_COOKIE_SECURE=true
SEEKINO_AUTH_QUERY_LEASE_SECONDS=1800
SEEKINO_SMTP_HOST=127.0.0.1
SEEKINO_SMTP_PORT=1025
SEEKINO_SMTP_USERNAME=
SEEKINO_SMTP_PASSWORD=
SEEKINO_SMTP_SENDER=noreply@seekino.test
SEEKINO_SMTP_USE_SSL=false
SEEKINO_SMTP_START_TLS=false
SEEKINO_MAILPIT_SMTP_PORT=1025
SEEKINO_MAILPIT_WEB_PORT=8025
SEEKINO_HTTP_HOST=127.0.0.1
SEEKINO_HTTP_PORT=9622
SEEKINO_ENVIRONMENT=development
SEEKINO_DATABASE_URL=mysql+pymysql://<本机数据库凭证>@127.0.0.1:3306/seekino_phone_dev?charset=utf8mb4
SEEKINO_ELASTICSEARCH_INDEX=seekino_phone_chunks_dev
SEEKINO_ELASTICSEARCH_EVENT_INDEX=seekino_phone_events_dev
SEEKINO_ARTIFACT_ROOT=<仓库绝对路径>/outputs/phone-test/artifacts
SEEKINO_LOG_DIRECTORY=<仓库绝对路径>/outputs/local-services/cloud-auth/logs
FORWARDED_ALLOW_IPS=127.0.0.1,::1
```

API 的 `SEEKINO_ALLOWED_ORIGINS` 加入实际的 `https://<电脑内网IP>:8443`，保留已有
开发 origin。1800 秒租约大于当前 1200 秒总执行超时；调整模型执行配置时一起核对。

```bash
uv run --no-sync --env-file outputs/local-services/cloud-auth/phone.env python deploy/cloud-auth/run.py init-data
uv run --no-sync --env-file outputs/local-services/cloud-auth/phone.env python deploy/cloud-auth/run.py start
uv run --no-sync --env-file outputs/local-services/cloud-auth/phone.env python deploy/cloud-auth/run.py restart-api
uv run --no-sync --env-file outputs/local-services/cloud-auth/phone.env python deploy/cloud-auth/run.py status
```

`init-data` 初始化独立测试库和 Chunk/Event 两个索引，不迁移原库。
底层 MySQL/Elasticsearch 进程复用，数据库、索引、视频目录、日志、Cookie 和 API
进程记录分开；cloud 消费者不会领取原 local 数据库中的任务。
脚本对这套 phone 资源名称和 9622 端口做固定校验，拒绝接管 local。

`start` 可重复执行；`restart-api` 先验证 Redis PING、SMTP、应用配置和测试库中的
活跃解析任务，只重启 `phone-api`，不会停止原 `api` 或 Vite。重启前关闭连接 cloud
的页面，避免 SSE 拖住退出；超时不会强制终止，新实例失败不会自动恢复旧配置。
`./start.sh` 继续用于原 local 工作台；它管理共享的 MySQL，停 MySQL 也会影响 cloud。

电脑打开 `http://127.0.0.1:8025` 查看 Mailpit 收件箱。手机填邮箱申请验证码后，
在电脑收件箱按收件人找到最新邮件，将六位验证码填回手机同一 Safari 会话。
邮件只进入本机 Mailpit，不会投递到填写的真实邮箱。

Redis、SMTP 和收件箱均仅监听 `127.0.0.1`，手机只访问 Nginx 的 8443。
状态和邮件保留在已忽略的 `outputs/local-services/`，手机视频在 `outputs/phone-test/`，
不设置开机自启。结束手机测试时，先停止 Nginx，再停止独立 cloud API 和测试依赖：

```bash
./deploy/nginx/start.sh stop
uv run --no-sync --env-file outputs/local-services/cloud-auth/phone.env python deploy/cloud-auth/run.py stop
```

原 local 资料仍由本地管理员访问。独立 cloud 库初始没有视频，游客可以进入正常页面，
只能浏览其中已发布的公共资料；登录后的个人空间也初始为空，不自动复制 local 视频。

### 手机 HTTPS 入口

先确认独立 9622 API 已按 cloud 模式配置；local 模式会信任操作者为本地管理员，不能用于
验证游客与账号隔离。HTTPS 入口继续使用 `SEEKINO_AUTH_COOKIE_SECURE=true`。
同机 Nginx 是直接入口时，可显式限定 API 的 `FORWARDED_ALLOW_IPS=127.0.0.1,::1`。
修改 API 环境配置后，需要按原有方式重启 API 才会生效。

1. 确定电脑的内网 IP（系统设置 → 网络）。假设为 `10.254.1.2`，先停项目 Nginx，
   再使用电脑的实际地址启动：

   ```bash
   ./deploy/nginx/start.sh stop
   SEEKINO_HTTPS_BIND=10.254.1.2 SEEKINO_HTTP_PORT=9622 ./deploy/nginx/start.sh
   ```

   脚本将实际绑定地址加入证书 SAN；不是填写 `0.0.0.0`。切换网络、IP 改变时需
   重新启动并签发包含新 IP 的证书。手机访问 `https://10.254.1.2:8443`。
   默认的 localhost 启动方式不向内网开放。内网启动前会检查目标 API 无凭证的 `/me`
   必须返回 401，拒绝将无需登录的 local 管理员 API 暴露出去。

2. 只将 `outputs/local-services/nginx/Seekino-Local-Dev-CA.cer` 通过 AirDrop 或其他
   受控方式复制到自己的测试 iPhone，打开该文件并允许下载描述文件：

   - 打开“设置 → 已下载描述文件”；没有该入口时，到“通用 → VPN 与设备管理”。
   - 安装 **Seekino Local Development CA** 描述文件，核对证书名称和指纹，按提示
     输入设备密码。安装描述文件后，SSL/TLS 还没有自动获得信任。
   - 再进入“设置 → 通用 → 关于本机 → 证书信任设置”，只为这个 Seekino 根证书
     开启完整信任。不要改动公司的证书或管理描述文件。

   手动描述文件与 SSL/TLS 信任是两个步骤，见 [Apple 官方说明](https://support.apple.com/en-us/102390)。
   Android 需在系统安全设置中安装该 CA 证书，入口名称因系统和浏览器而异。
   Mac 上安装信任不会自动让手机信任。公司网络还需要允许设备互访及 macOS 入站连接。

3. 从同一个 HTTPS 地址申请邮箱验证码并提交；可在电脑的 Mailpit 收件箱查看邮件。
   从 HTTP、localhost 切换到 LAN IP 时，浏览器不会自动沿用之前站点的身份。
   认证所需的 Redis、SMTP、签名秘密、租约仍由 API 配置，Nginx 不提供这些依赖。

CA 和服务端私钥仅保存在已忽略的 `outputs/`。手机只接收导出的 `.cer` 公共证书；
不要分发 `rootCA-key.pem`、`site-key.pem` 或整个状态目录。这些开发证书不用于公开网站。

## 更换旧测试 CA

旧 `outputs/local-services/nginx/ca/` 与 `certs/` 只保留用于指纹核对和必要回退，普通启动不会使用。
不要因名称含 mkcert 就批量删除证书；撤销信任前，先与该目录的 `rootCA.pem` 核对
指纹，只处理这个项目之前安装的测试 CA，不修改公司根证书、钥匙串或管理描述文件。

Mac 上在对应钥匙串删除已确认的旧测试证书，或只撤销其显式信任；iPhone 在
“VPN 与设备管理”删除已确认的旧测试 CA 描述文件。删除本机证书文件不会自动
撤销系统信任，关闭 Nginx 也不会撤销 CA 信任；反过来，撤旧 CA 不会自动信任新 CA。
旧文件保留不代表应该继续信任旧 CA。

## 配置与检查

```bash
./deploy/nginx/start.sh check  # 用临时配置校验参数；不覆盖运行配置、不签发证书
./deploy/nginx/start.sh stop   # 仅优雅停止本项目的 Nginx
```

使用自定义绑定或端口时，`check` 需传入相同的环境参数。
模板位于 `deploy/nginx/nginx.conf.template`；生成配置、PID、错误日志和证书都位于
`outputs/local-services/nginx/`。不修改 Homebrew 全局 `nginx.conf`，不配置开机自启。
模板修改后按 stop/start 应用；stop 等待最多 5 秒，已有 SSE 等连接未结束时返回提示，
稍后再启动，不强制终止。停止 Nginx 不会停止 API、数据库或 Vite。

API 保留原始 `/api` 前缀、包含端口的 Host、HTTPS scheme 和实际客户端 IP。
API 不缓存、不缓冲响应，保留 SSE 流式输出及后端鉴权后的 Range/206 响应。
单请求上限为 32 MiB，覆盖当前默认 8 MiB 的视频分片；总视频大小仍由应用限制。
如果调整分片或附件大小，需要一起检查这个入口上限。静态根目录只有 `web/dist`，
原片和私有媒体继续经过 API 权限检查。

验收时依次检查首页、深链接刷新、游客 Cookie、验证码登录、视频分片上传、
拖动播放和 SSE 首个事件；HTTPS 可连通不等于整套 cloud 业务已通过测试。

2026-10-18 本机验证结果：

- 新 **Seekino Local Development CA** 已导入系统钥匙串，仅 SSL 始终信任，其他用途
  保持默认。Nginx 1.31.7 已 stop/start，实际提供的证书与 `seekino-certs/site.pem` 一致。
  入口已绑定 `10.254.47.100:8443`，叶证书 SAN 包含该 IP。系统 curl 不带
  `--cacert` 访问此 HTTPS `/healthz` 返回 200、证书校验结果为 0。
- 原 `.env.local` 已逐字节恢复，本机 `localhost/127.0.0.1:5173` 均免凭证返回 local
  管理员，原有 2 组视频系列可读；Vite 代理显式保留 Host，避免 cloud 下 Origin 比对失败。
- 9622 `phone-api` 使用独立数据库、Chunk/Event 索引、目录和 Cookie，Secure 保持开启，
  Nginx 仅代理该实例。浏览器已确认 local 原页面恢复，cloud 游客能进入正常助手页面。
  真实 HTTPS 共通过 31 项检查，覆盖注册、退出后同账号再次登录、SMTP 捕获、
  Redis 300 秒验证码摘要、错误验证码拒绝、成功消费挑战、个人资料权限、旧凭证失效；
  视频上传与模型调用未纳入本轮验收。
- 旧项目 mkcert CA 已恢复系统默认信任后删除 System 钥匙串记录；旧叶携旧根验证仍返回
  `NOT_TRUSTED`，新叶验证成功。项目相关管理员信任记录仅保留新 CA，用户级没有额外
  信任设置，公司的证书未修改。
  旧 `ca/`、`certs/` 文件保留核对和回退，启动器不使用。
- 前端构建、Nginx 配置与基础页面已验证。更名前使用临时身份/Redis 替身通过的
  13 项入口检查覆盖 Cookie、代理头、上传转发、Range/206 和 SSE，没有接真实账号库、SMTP 或模型。
- 导出的 iPhone `.cer` 内部名称已为 Seekino；用户已反馈完成安装，手机实际跨设备
  访问与交互仍待手机验收。电脑访问内网 IP 成功不能证明公司 Wi-Fi 允许设备互访。

## 正式云服务器

继续使用同样的“HTTPS 入口 → API”结构。配置实际域名和公信 CA 的证书/私钥，
安排自动续期，通常监听 443；不向访问者分发本地 CA。API 保持受限访问并仅信任
真正的代理地址。此脚本只负责本地开发，不安装 Linux 系统服务或配置公网证书续期。

参考：[Nginx HTTPS](https://nginx.org/en/docs/http/configuring_https_servers.html)、
[Homebrew Nginx](https://formulae.brew.sh/formula/nginx)、
[mkcert 与手机信任](https://github.com/FiloSottile/mkcert#mobile-devices)。
