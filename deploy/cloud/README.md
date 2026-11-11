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
将 [API 模型模板](models.example.yaml) 配置为服务器的 `/etc/seekino/models.yaml`，
核对供应商账户实际可用的模型或 endpoint；模板沿用项目已有 API 模型绑定，未做真实调用验收。
cloud 要求 `supervisor_harness: deepagents`，不得绑定本机 Codex/Claude runtime；模型来源与
样例索引的 embedding 模型、维度须匹配。当前根配置包含本机模型，不可直接用于云端。
每条 Chat 回答的模型和工具预算默认各 100 次，默认可选上限也各为 100。生产全局 Agent 设置独立保存在 `/data/config/cloud/agent-runtime.yaml`。升级时首次读取会继承其他运行参数，管理员全局设置页可继续调整。模型连接和 API Key 使用原模型文件，按 IP 的每日查询额度使用鉴权环境变量。[配置说明](../../docs/model-config.md)

Deep Agents checkpoint 与 MySQL schema 的初始化、已有数据库升级按
[模型配置](../../docs/model-config.md#deep-agents-checkpoint-持久化)核对。

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
| GPU 服务器 | 解析索引可优先使用与 CPU 同权重的 Qwen embedding；WhisperX 转写使用独立来源。 |

云端入站使用 TCP 80 和 443。设置 `SEEKINO_PUBLIC_HOST` 为公开域名，80 对收到的请求
返回 308 并保留路径和查询参数。可用 `curl -I 'http://你的域名/settings?lang=zh'`
检查 `Location`，再用浏览器的默认证书信任设置访问对应 HTTPS 地址。

服务器的模型 YAML（`SEEKINO_MODEL_CONFIG`）指向的 `base_url`：

- Cloud 的查询 embedding 使用 API 的本机 loopback。文档按 `document_base_urls` 顺序尝试 GPU 转发和本机。下面是这次部署的设置，其他部署可选自己的副本。
- WhisperX 与对话模型分别使用自己的来源。非 loopback 模型地址须提供 HTTPS。

```yaml
model_sources:
  llama_embedding_local:
    adapter: llama_cpp_embedding
    provider: llama_cpp
    base_url: http://127.0.0.1:18845
    document_base_urls:
      - http://127.0.0.1:18843
      - http://127.0.0.1:18845
```

已经使用管理设置的 Cloud 实例可把生效模型文件保存在数据卷 `/data/config/cloud/models.yaml`。修改 API 容器的 `SEEKINO_MODEL_CONFIG` 所指文件，并核对通过该配置构造的查询和文档调用。长文档回退时，示例云机将 `models.embedding.request_timeout_seconds` 配为 360 秒。

[Cloud Compose 示例](compose.embedding.example.yaml) 使用 llama.cpp b12283，与 API 共享 loopback。CPU 副本限两核、1600 MiB，使用同一份 Qwen3-Embedding-0.6B-Q8_0.gguf（SHA-256 `06507c7b42688469c4e7298b0a1e16deff06caf291cf0a5b278c308249c3e439`）。设置 `SEEKINO_EMBEDDING_GGUF` 为宿主机权重路径，镜像须提供 llama-server 入口和 Python 健康检查。替换 API 时用同一条 Compose 命令更新 `api embedding`。GPU 启动后，由原有转发提供 API 内的 18843；只有这个端点实际可达才会接手文档。


## 从本机私有配置启动

从 `.env.local` 派生的 `.env.cloud` 只能经私有通道传到服务器；文件权限使用 600，
不要提交真实配置。`.env.*` 已覆盖该文件，仓库另有显式 `.env.cloud` 忽略项。
复制时核对 production 数据库与 ES 索引、容器内路径、独立会话密钥和数据库密码；
移除只在本机可用的 HTTP_PROXY/HTTPS_PROXY 及 Mac 路径。模型 YAML 独立挂载，保持已有
embedding 模型、维度和编码合同；运行环境可校验通过不代表模型端点已经连通。

未配置 SMTP 时可以启动游客功能，邮箱验证码发送和验证明确返回 503 EMAIL_UNAVAILABLE，
不会放宽 CSRF 或管理权限。该启动缺陷与验证见
[游客启动 TRD](../../docs/design/03-product-reassessment/cloud-guest-without-smtp-trd.md)。
私有配置、可信 TLS、管理员/邮箱、模型接口与已有样例数据需要分别验收。
