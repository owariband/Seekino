# 云端样例库首发

首发开放已解析、已发布的样例，用户可观看、检索和问答，管理员负责上传与资料管理。
复制 [环境模板](first-release.env.example) 到服务器的私有配置文件，填入生产连接和凭据，
并将示例域名替换为实际 HTTPS 地址。

`ANIVAG_PUBLIC_LIBRARY_ONLY=true` 只作用于 cloud 身份：普通邮箱用户仍可登录和保存自己的
对话，但身份不授予个人资料范围或上传、管理权限；游客继续体验公共资料。
管理员仍可管理自己的资料和平台公共资料，前端公共资料源开放其管理入口。
后端上传、任务管理和资料修改继续校验身份范围，隐藏按钮不能绕过这些校验。
该开关默认 false；关闭后恢复普通用户的个人资料范围，本机 local 模式继续使用本机身份。

新管理员须在首次邮箱验证前设置 `ANIVAG_AUTH_ADMIN_EMAILS`。已有账号的角色仍以数据库
为准，修改邮箱配置不会自动提权。上传样例时由管理员选择平台公共资料源；只有媒体、解析和
索引就绪的系列才发布，普通用户不会看到尚未发布的内容。

服务器使用独立的 API 模型 YAML，通过 `ANIVAG_MODEL_CONFIG` 指定。
将 [API 模型模板](models.example.yaml) 配置为服务器的 `/etc/seekino/models.yaml`，
核对供应商账户实际可用的模型或 endpoint；模板沿用项目已有 API 模型绑定，未做真实调用验收。
cloud 要求 `supervisor_harness: deepagents`，不得绑定本机 Codex/Claude runtime；模型来源与
样例索引的 embedding 模型、维度须匹配。当前根配置包含本机模型，不可直接用于云端。
Deep Agents checkpoint 与 MySQL schema 的初始化、已有数据库升级按
[模型配置](../../docs/model-config.md#deep-agents-checkpoint-持久化)核对。

Web 使用 `npm --prefix web run build` 的 `web/dist`，由公网 HTTPS 反向代理同源提供；
API 用 `python -m server.main`，MySQL、Elasticsearch 和 Redis 保持私有连接。
现有 [Compose](../../compose.yaml) 用于本机开发，不能直接照搬其空密码数据库配置。
HTTPS、媒体、SSE 及请求来源校验参照 [Nginx 入口](../nginx/README.md)。

发布验收至少覆盖游客和普通邮箱用户的观看、检索、问答，普通用户直接调用上传/任务/资料修改
接口的拒绝，以及管理员的上传、发布和撤回。真实 SMTP、模型调用、服务器重启后的对话恢复
与公开入口仍须在实际服务器上验证；配置模板不代表云端部署已完成。
