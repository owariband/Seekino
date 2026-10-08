# 模型 Token 用量

AniVAG 使用现有 MySQL 的 `model_usage` 记录 Gateway 最终调用事件，模型消耗页面查询这些本地记录。
统计范围包括 V2 视频解析/index 和整个助手问答；失败调用若有 provider 已确认用量，也会保留。
本批只统计 Token，没有费用、货币或汇率合同。

## 配置与归属

Server 复用现有 `ANIVAG_DATABASE_URL`，无需新的存储服务或 Langfuse 凭据。
新表已登记在现有 init-db 模型集合；真实建表、服务启动/重启和模型运行应由运行负责人核实具体操作授权。
仓库 `config.yaml` 的 `tracing.enabled=false`；bootstrap 停止装配 Langfuse writer/reader，保留可选模块和配置字段。

- `video_parse`：V2 解析及 index/embedding，`runId` 是真实 `video_parse_task_id`。
- `chat_retrieval`：助手 direct、Web 和视频检索，`runId` 是真实 `assistant_message_id`。

Chat scope 在执行模型的线程绑定，内部 query embedding/rerank 继承助手消息，不创建独立 retrieval run。
独立检索没有受支持 owner 时不写入本表。默认解析模式为 V2，需要原有 ES 和模型配置；显式 V1 opt-in 保留，
但旧 `job_id` 不冒充 V2 任务 ID，V1 processing 不注入新统计。

## 追加记录与 Token 口径

每个 Gateway UUID `call_id` 只追加一行，`usage` 与 `metadata` 分别保存固定 JSON 对象。
重复 call_id 仅在 workspace/taskType/run 全部相同时幂等返回，不覆盖原 JSON 或写入时间；归属冲突拒绝。
写入在调用结束后使用短事务，不在模型执行期间持有数据库事务。
metadata 仅投影实际模型、provider、操作、状态、用量来源/范围和身份/阶段白名单；src 无明确来源时为 null。
不存 prompt、回答、工具参数、原始 response、SDK cost_usd 或凭据。

Token 使用 Gateway 的 inclusive input/output/total 合同。缓存读写、文字/图片/音频是 input 的子集，
reasoning 是 output 的子集，不再次加到总量。可空 subset 独立传播未知，不显示为零，
也不因为缓存或推理未知而否定已确认的核心总量。

SDK 提供实际模型明细时，用明细替代父总数及配置 alias。整体调用数按不同 call_id 计一次；
每个实际模型分组也按不同 call_id 计数。SDK 未提供明细时只使用主用量，不猜隐藏的 provider 请求。
任一参与项核心用量未知时，受影响整体/模型/run 的完整 input/output/total 为 null，
`knownTokens` 保留可确认项小计，`missingUsageCount` 按不同 call_id 计一次。
Claude `main_loop` 数字只证明主循环小计，相应完整核心总量为 null，已知数字仍保留。

## 查询接口

```http
GET /api/usage/overview?workspaceId=local-default
```

可选 `fromStartTime`、`toStartTime` 为 Unix 毫秒，默认最近 30 天、最大 31 天，默认截止时间对齐下一整分钟。
SQL 使用 `started_at >= from AND started_at < to` 的 UTC 半开窗口；先精确筛选 workspace、run 和系列，
系列匹配 metadata.context.series_id 或 series_ids 数组成员，避免 series-A 错配 series-AA。
没有新增 taskType 请求筛选。workspace 仍是数据范围，登录与成员授权尚未实现。

按 started_at ASC、call_id ASC 排序，读取 20,000 + 1 行；多出的一行只用于截断标记，不计小计。
`truncated=true` 时整体/模型/run 的完整 Token 合计为 null，已读调用数和 knownTokens 保留；
不凭未读记录增加 missingUsageCount，不能把已读小计解释为窗口全部消耗。

| 字段 | 含义 |
| --- | --- |
| source | 固定 `local` |
| status / message | `available` 或 `unavailable`；读取失败的用户提示 |
| fromStartTime / toStartTime / fetchedAt | 查询窗口和读取时刻，Unix 毫秒 |
| truncated | 匹配记录超过 20,000 条，当前数据只是已读部分 |
| totals | 已记录调用数、输入/输出、缓存读写、推理、总量、已知小计和缺失数 |
| models | 按实际 provider/model/category/operation 分组 |
| runs | 真实 runId、两类 taskType、可确定的系列/视频身份及 Token 汇总 |
| totalTokens / knownTokens | 可确认的完整已记录合计或 null，以及已知 Token 小计 |
| missingUsageCount | 缺少可确认完整用量的不同调用数，包含 main_loop |

页面一次查询并复用按模型、任务/Chat 两个视图，保留系列筛选和刷新。
数据库读取失败返回 unavailable 和未知调用数，不返回 available 的全零结果。
空窗口表示没有已记录调用，无法证明历史消费为零。

## 历史与验收限制

旧 `/api/jobs/usage/get` 已退出公开路由。旧统计表、价格与金额历史保留，不读取、回填或混入本地统计。
新表没有业务对象外键，删除任务/视频/消息不级联删除已记录用量。
`ANIVAG_MODEL_PRICING_CONFIG` 不影响当前模型消耗页。
V2 预算、并发、heartbeat、lease、取消和 reserve/release 仍由原 manager 控制。

没有持久化队列、outbox 或采集完成标记；写入失败和进程终止可能丢记录，业务完成状态不能证明全部消耗已采集。
写入失败保留模型原结果并记录不含原始载荷的 warning；available 仅表示本次记录查询成功。
离线 SQLite、MySQL dialect 和页面测试分别提供局部证据，不能代替真实 TEST MySQL 或模型语义验收。
真实 U06 至少执行一条 V2 解析/index 和一次 Chat，对实际触发调用的 run/call_id、SQL、API 和页面逐层对账。
源码与离线回归不等于真实环境验收；真实模型、MySQL 和页面仍需按上述步骤对账。
