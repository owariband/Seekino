# 云端样例库首发

首发开放已解析、已发布的样例，用户可观看、检索和问答，管理员负责上传与资料管理。
复制 [环境模板](env.cloud.example) 到服务器的私有配置文件，填入生产连接和凭据，
并将示例域名替换为实际 HTTPS 地址。

`SEEKINO_PUBLIC_LIBRARY_ONLY=true` 只作用于 cloud 身份：普通邮箱用户仍可登录和保存自己的
对话，但身份不授予个人资料范围或上传、管理权限；游客继续体验公共资料。
管理员仍可管理自己的资料和平台公共资料，前端公共资料源开放其管理入口。
后端上传、任务管理和资料修改继续校验身份范围，隐藏按钮不能绕过这些校验。
该开关默认 false；关闭后恢复普通用户的个人资料范围，本机 local 模式继续使用本机身份。

新管理员须在首次邮箱验证前设置 `SEEKINO_AUTH_ADMIN_EMAILS`。已有账号的角色仍以数据库
为准，修改邮箱配置不会自动提权。上传样例时由管理员选择平台公共资料源；只有媒体、解析和
索引就绪的系列才发布，普通用户不会看到尚未发布的内容。

服务器使用独立的 API 模型 YAML，通过 `SEEKINO_MODEL_CONFIG` 指定。
Chat hook 默认只由 Jev 分类，`query_hook.rewriter_enabled: false`，无需 `models.query_rewriter`。已有部署的显式配置不会被镜像升级覆盖，须在实际挂载的 YAML 中关闭改写；`models.query_hook` 会选择合并决策并绕过 Jev，Jev-only 部署不能保留这个 Chat 绑定。修改后重启 API，并用真实 route 日志和调用记录核对。
将 [API 模型模板](models.example.yaml) 配置为服务器的 `/etc/seekino/models.yaml`，
核对供应商账户实际可用的模型或 endpoint；模板沿用项目已有 API 模型绑定，未做真实调用验收。
cloud 要求 `supervisor_harness: deepagents`，不得绑定本机 Codex/Claude runtime；模型来源与
样例索引的 embedding 模型、维度须匹配。当前根配置包含本机模型，不可直接用于云端。
每条 Chat 回答的模型和工具预算默认各 100 次，默认可选上限也各为 100。生产全局 Agent 设置独立保存在 `/data/config/cloud/agent-runtime.yaml`。升级时首次读取会继承其他运行参数，管理员全局设置页可继续调整。模型连接和 API Key 使用原模型文件，按 IP 的每日查询额度使用鉴权环境变量。[配置说明](../local-setup.md#3-模型仍用现有配置方式)

Deep Agents checkpoint 与 MySQL schema 的初始化、已有数据库升级按
[模型配置](../../gateway/README.md#云端-deep-agents-与-mysql-checkpoint)核对。

Web 使用 `npm --prefix web run build` 的 `web/dist`，由公网 HTTPS 反向代理同源提供；
API 用 `python -m server.main`，MySQL、Elasticsearch 和 Redis 保持私有连接。
现有 [Compose](../../compose.yaml) 用于本机开发，不能直接照搬其空密码数据库配置。
HTTPS、媒体、SSE 及请求来源校验参照 [Nginx 入口](../nginx/README.md)。

发布验收至少覆盖游客和普通邮箱用户的观看、检索、问答，普通用户直接调用上传/任务/资料修改
接口的拒绝，以及管理员的上传、发布和撤回。真实 SMTP、模型调用、服务器重启后的对话恢复
与公开入口仍须在实际服务器上验证；配置模板不代表云端部署已完成。

## 服务器拓扑

| 服务器 | 运行内容 |
| --- | --- |
| CPU 服务器 | 根 [Compose](../../compose.yaml) 叠加 [compose.prod.yaml](compose.prod.yaml) 运行 Web、API、MySQL、Elasticsearch 和 Redis。Web 复用 [Nginx 模板](../nginx/nginx.conf.template)，80 将请求重定向到公开域名的 HTTPS 地址，443 提供页面与 API。API 仅发布在 loopback，数据库与索引只使用内部网络。 |
| GPU 服务器 | 自托管模型：查询 embedding（与样例索引相同的 Qwen3-Embedding-0.6B Q8 GGUF，llama.cpp，1024 维）、WhisperX 转写（OpenAI `/v1/audio/transcriptions` 兼容服务）、替换 Jev 的路由模型。 |

云端入站使用 TCP 80 和 443。设置 `SEEKINO_PUBLIC_HOST` 为公开域名，80 对收到的请求
返回 308 并保留路径和查询参数。可用 `curl -I 'http://你的域名/settings?lang=zh'`
检查 `Location`，再用浏览器的默认证书信任设置访问对应 HTTPS 地址。

服务器的模型 YAML（`SEEKINO_MODEL_CONFIG`）指向的 `base_url`：

- `llama_cpp_embedding` 来源：`https://<GPU 主机>`；`openai_audio` 转写来源：`https://<GPU 主机>/v1`。
  非 loopback 地址必须是 HTTPS（`gateway/config.py` 的 `_https_base_url` / `_audio_base_url`），
  因此 GPU 服务器需要提供 TLS 入口。
- DeepSeek、Kimi 等对话与重排模型仍使用各自的公网 HTTPS API。
- 查询路由当前仍为 OpenRouter 上的 Jev；改为 GPU 上的路由模型需要单独修改 `gateway/query_hook.py`。


## 从本机私有配置启动

从 `.env.local` 派生的 `.env.cloud` 只能经私有通道传到服务器；文件权限使用 600，
不要提交真实配置。`.env.*` 已覆盖该文件，仓库另有显式 `.env.cloud` 忽略项。
复制时核对 production 数据库与 ES 索引、容器内路径、独立会话密钥和数据库密码；
移除只在本机可用的 HTTP_PROXY/HTTPS_PROXY 及 Mac 路径。模型 YAML 独立挂载，保持已有
embedding 模型、维度和编码合同；运行环境可校验通过不代表模型端点已经连通。

未配置 SMTP 时可以启动游客功能，邮箱验证码发送和验证明确返回 503 EMAIL_UNAVAILABLE，
不会放宽 CSRF 或管理权限。
私有配置、可信 TLS、管理员/邮箱、模型接口与已有样例数据需要分别验收。
