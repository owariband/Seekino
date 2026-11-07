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
首次安装时，用 [CPU 主机模型模板](models.example.yaml) 生成服务器的 `/etc/seekino/models.yaml`，
填写实际的三个 GPU HTTPS 地址。已有私有文件按下面的角色逐项修改，保留本部署的视觉、对话及检索合同。
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

## 服务器拓扑

| 服务器 | 运行内容 |
| --- | --- |
| CPU 服务器 | 根 [Compose](../../compose.yaml) 叠加 [compose.prod.yaml](compose.prod.yaml) 运行 Web、API、MySQL、Elasticsearch 和 Redis。Web 复用 [Nginx 模板](../nginx/nginx.conf.template)，80 将请求重定向到公开域名的 HTTPS 地址，443 提供页面与 API。API 仅发布在 loopback，数据库与索引只使用内部网络。 |
| CUDA GPU 服务器 | WhisperX 转写、Qwen3-Embedding-0.6B Q8 GGUF 向量和 StartLux Decision 查询路由。 |

云端入站使用 TCP 80 和 443。设置 `SEEKINO_PUBLIC_HOST` 为公开域名，80 对收到的请求
返回 308 并保留路径和查询参数。可用 `curl -I 'http://你的域名/settings?lang=zh'`
检查 `Location`，再用浏览器的默认证书信任设置访问对应 HTTPS 地址。

| 角色 | 私有模型 YAML 填写方式 | 服务合同 |
|---|---|---|
| WhisperX | 转写来源的 `base_url` 填服务前缀，包含 `/v1`，通过 `WHISPER_API_KEY` 认证 | GPU 提供 `POST /v1/audio/transcriptions` 和带时间的 segments，模型别名对应 GPU 实际加载的权重 |
| Qwen 向量 | `llama_cpp_embedding.base_url` 填 HTTPS 服务根地址，适配器自行拼接 `/v1/embeddings` | 样例库使用 `llama_embedding_local`、`seekino-qwen3-embedding-0.6b`、文本模式、1,024 维和模板中的查询指令。该适配器使用可从 CPU 主机访问的受控服务入口 |
| StartLux | `query_hook.router_provider: startlux`，`router_endpoint` 填完整的 HTTPS `/v1/systemone`；模板通过 `STARTLUX_API_KEY` 向 GPU 入口传 Bearer。受控私网无令牌的入口可设 `router_api_key_env: null` | GPU Decision 服务加载 `StartLux-Decision-v1.1-4B-AutoThink`；Seekino 发送 `think_mode: never` 并读取 `answers.route.choice`。路由仍使用既有时限与失败回退 |

视觉理解、对话和查询改写使用私有 YAML 中已有的远端提供方。CPU 模板只绑定远端转写，未启用独立的本地 pyannote 说话人分离。已有索引的来源名、编码器、维度和查询指令参与索引合同，部署时保留原库的绑定；使用其他编码器的库需要独立的索引迁移。模型 YAML 中的示例主机名需要直接替换，Compose 不展开挂载文件的内容。[StartLux 服务协议](https://github.com/StartLuxLabs/StartLux-Decision)

## 4 核、7.25 GiB CPU 主机

| 生产服务 | CPU 上限（核） | 内存上限（MiB） |
|---|---:|---:|
| API，含 Consumer 与 FFmpeg | 1.50 | 2,304 |
| Elasticsearch | 1.00 | 2,048，其中 JVM 堆 1,024 |
| MySQL | 0.75 | 1,152 |
| Redis | 0.25 | 192，其中数据预算 64，保留 AOF 和 noeviction |
| Web / Nginx | 0.25 | 128 |
| **常驻合计** | **3.75** | **5,824** |
| initialize，一次性 | 0.50 | 512 |

按实机 7,429 MiB 计算，常驻容器全部到顶仍留 1,605 MiB 给宿主；额外计入一次初始化后仍留 1,093 MiB。各容器的 `memswap_limit` 与 `mem_limit` 相等，防止批量解析把压力转到宿主 swap。CPU 配额是可用计算时间的上限。构建镜像的 BuildKit 不在服务预算内，发布可使用预构建的镜像。[Docker 资源设置](https://docs.docker.com/reference/compose-file/services/#memswap_limit) · [ES 8.19 堆设置](https://www.elastic.co/guide/en/elasticsearch/reference/8.19/advanced-configuration.html)

生产叠加固定视频准入为 1，默认理解、Refine、索引和 ASR 并发各为 1，关闭解析原始模型跟踪。三集可同时提交，尚未准入的视频在 MySQL 排队。`SEEKINO_MQ_MAX_RUNNING_CHILDREN=2` 另外限制同库所有理解、Refine、索引在途子租约之和，实时领取也经过同一入口。它是对已有任务快照与各阶段限制的额外约束。切换时已有租约可正常完成，等待空位后才发下一份。整片准备、摘要与 Chat 各自使用原执行路径，均受 API 容器 CPU/内存配额约束。部署到同一任务库的所有消费者应使用同一版 API 镜像和同一配置。

发布前在仓库根目录渲染配置。现场另有镜像或入口叠加文件时，沿用该部署原有的 `-f` 顺序，核对最终使用包含本次变更的 API 镜像。

```bash
docker compose --env-file deploy/cloud/.env.cloud -f compose.yaml -f deploy/cloud/compose.prod.yaml config --quiet
```

按既有发布流程重建容器后，用 `docker inspect` 核对 `HostConfig.NanoCpus`、`Memory`、`MemorySwap`，用 `docker stats --no-stream` 观察实际用量。首次整集验收先跑一集，再同时提交三集并发起检索，记录 cgroup v2 的 `memory.peak`、`memory.events`、容器重启数和每集完成耗时。查看任务完成以及命中能否回到源片；健康探针只验证服务就绪。需要回退时使用上个 API 镜像及其配套 Compose、私有模型配置，保留数据库和媒体卷。[预算设计与实测](../../docs/design/03-product-reassessment/cloud-cpu-budget-trd.md)


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
