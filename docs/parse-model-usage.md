# 模型用量与 USD 预估成本

模型用量和预估成本由 Langfuse 保存与计价，AniVAG 的模型消耗页面通过 Server
查询这些已同步记录。覆盖解析、向量化、检索查询编码与聊天；记录来自实际 Gateway
调用完成边界，包含已返回用量的失败调用。页面显示 USD，后端没有人民币价格引擎或汇率转换。

## 配置

仓库 `config.yaml` 已开启现有记录通道：

```yaml
tracing:
  enabled: true
  environment: development
```

在 `.env.local` 配置项目连接信息，不把密钥放进前端：

```dotenv
LANGFUSE_BASE_URL=https://your-langfuse-host
LANGFUSE_PUBLIC_KEY=your-project-public-key
LANGFUSE_SECRET_KEY=your-project-secret-key
```

可以使用 Langfuse Cloud 或自托管 v4+。显式 `tracing.base_url` 优先于环境地址；
`public_key_env`、`secret_key_env` 可沿用已有配置指定变量名。没有连接配置时，
业务模型仍可执行，消耗页面显示“尚未配置”，不会把未记录用量显示为零。
配置生效需要由运行负责人重启进程；本次代码修改不自动重启服务。

本批仅复用现有模型调用记录，不采集 prompt/response 正文或工具完整轨迹。
默认解析执行模式改为 V2，需配置 ES；显式 `ANIVAG_PARSE_EXECUTION_MODE=v1`
仍能运行旧链，新统计入口同样使用 Langfuse。

## 用量与价格

Token 总量保留 Provider/SDK 返回的 inclusive 输入与输出合同。写入 Langfuse 的
计价桶拆成普通输入、缓存读取、缓存写入和输出，避免缓存被重复计入普通输入。
只有返回了完整 TTL 细分时才分别记录缓存写入5分钟/1小时；推理量仍属于输出子集。
已知且可拆开的模态使用互斥文字/图片/音频桶，缓存与模态交集未知时不猜交集。

当前字段键为 `input`、`input_cached_tokens`、`input_cache_creation_tokens`、
`input_cache_creation_5m_tokens`、`input_cache_creation_1h_tokens`、
`input_text_tokens`、`input_image_tokens`、`input_audio_tokens`、`output` 和 `total`。
缓存写入总桶与 TTL 子桶二选一；普通输入只保留未被缓存或已知模态桶覆盖的剩余量，
与这些细分桶互不重叠。
模型价格必须与实际写入的用量键对应；在 Langfuse **Project Settings > Models**
配置 USD 单价与模型匹配，不在 AniVAG 维护第二套价格配置。
[Langfuse 用量与成本合同](https://langfuse.com/docs/observability/features/token-and-cost-tracking)

Claude SDK 提供实际模型汇总时，按模型记录该轮 SDK 用量和 USD 预估成本，
外层 run 不重复记录总量和成本。它们是 SDK 聚合，不能解释为每个内部 Provider 请求。
仅有主循环 usage 时保留这个粒度，不拿范围不同的子 Agent 总费用冒充同一模型成本。
调用数按 Gateway call ID 去重，Token/成本按独立 Langfuse observation ID 汇总。

没有 SDK 成本时使用 Langfuse 推算；缺价格、细分不完整或价格仅覆盖部分用量时，
完整成本为 null，保留可证明的已知小计。费用是模型预估成本，不是供应商账单金额。

## 查询接口

```http
GET /api/usage/overview?workspaceId=local-default
```

可选 `fromStartTime`、`toStartTime` 为 Unix 毫秒，默认最近30天；时间窗必须有界，
也可用 `seriesId`、`runId` 筛选。Server 查询固定 Langfuse 环境和 workspace，
同时精确核验返回的 workspace，系列多选按 metadata 中的真实 ID 匹配。
目前 workspace 仍是数据范围，登录与成员授权尚未实现。

查询使用 `/api/public/v2/observations`，选择 usage/model/metadata 字段并分页读取。
单次最多20页、每页1000条，总读取预算15秒；未读完时 `truncated=true`，
完整 Token/成本合计保持未知。缓存以 workspace 和时间窗隔离，最长30秒、最多32个条目，
用于合并页面查询；没有新增 SQL 聚合表。
[Langfuse Public API](https://langfuse.com/docs/api-and-data-platform/features/public-api)

| 字段 | 含义 |
| --- | --- |
| source / currency | 固定 `langfuse` / `USD` |
| status | `available`、`disabled`、`not_configured`、`unavailable` |
| fromStartTime / toStartTime / fetchedAt | 查询时间窗与本次结果返回时间，Unix 毫秒 |
| truncated | 是否受有界分页限制；不能把截断小计当完整合计 |
| totals | 已同步调用、输入/输出/缓存/推理/总量与预估成本汇总 |
| models | 按服务商、实际模型、分类与操作分组 |
| runs | 按解析任务、聊天运行或其他运行分组 |
| totalTokens / estimatedCostUsd | 完整已同步记录合计；缺失或截断时 null |
| knownTokens / knownCostUsd | 仅已知记录的小计；金额为十进制字符串 |
| missingUsageCount / unpricedCallCount | 缺少用量或完整计价的调用数量 |

页面一次查询并使用服务端汇总，前端仅格式化 USD 字符串，不重复计算价格。
页面明确说明“已同步记录”；写入队列、网络失败和 Langfuse 处理延迟仍可能使最新
调用暂未出现在查询中，`available` 不代表所有业务调用都已成功上传。

## 旧统计退出与验收边界

旧 `/api/jobs/usage/get` 已退出公开路由，运行时不再装配 ParseUsageRepository 或
加载旧价格表。`capture`/`capture_video` 保留签名与身份上下文，仅订阅 Langfuse。
V2 的调用次数预算、租约、取消和模型控制仍使用原执行管理器，与统计来源分离。
历史 SQL 表、金额和价格快照保留，不换币种、不混入新统计、不回填历史。
旧 `ANIVAG_MODEL_PRICING_CONFIG` 不再影响模型消耗页面。

离线测试验证字段映射、范围过滤、分页、未知值、资源关闭和前端查询。
真实验收需连接项目，执行一次真实解析或聊天，在 Langfuse 和 AniVAG 页面
核对相同模型、用量、USD 及运行身份；没有项目凭据时不能宣称该在线验收完成。
