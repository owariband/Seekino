# 模型 Token 用量与 API 费用估算

Seekino 使用现有 MySQL 的 `model_usage` 记录 Gateway 最终调用事件，模型消耗页面查询这些本地记录。
统计范围包括 V2 视频解析/index 和整个助手问答；失败调用若有 provider 已确认用量，也会保留。
页面展示 Token 和按配置估算的 API 费用；Codex 订阅调用保留 Token，排除金额估算。

## 配置与归属

Server 复用现有 `SEEKINO_DATABASE_URL`，无需新的存储服务或 Langfuse 凭据。
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
metadata 仅投影实际模型、provider、操作、状态、用量来源/范围和身份/阶段白名单。
新调用在 Gateway 的实际模型绑定处记录 `src`，例如 `codex_local`、`ark_default`；
无法确定真实来源的 shared tool fallback 和历史缺失记录仍为 null，不根据当前配置补写历史。
不存 prompt、回答、工具参数、原始 response、SDK cost_usd 或凭据。

Token 使用 Gateway 的 inclusive input/output/total 合同。缓存读写、文字/图片/音频是 input 的子集，
reasoning 是 output 的子集，不再次加到总量。可空 subset 独立传播未知，不显示为零，
也不因为缓存或推理未知而否定已确认的核心总量。

SDK 提供实际模型明细时，用明细替代父总数及配置 alias。整体调用数按不同 call_id 计一次；
每个实际模型分组也按不同 call_id 计数。SDK 未提供明细时只使用主用量，不猜隐藏的 provider 请求。
任一参与项核心用量未知时，受影响整体/模型/run 的完整 input/output/total 为 null，
`knownTokens` 保留可确认项小计，`missingUsageCount` 按不同 call_id 计一次。
Claude `main_loop` 数字只证明主循环小计，相应完整核心总量为 null，已知数字仍保留。

## 费用估算与 Codex 口径

服务启动时读取 `SEEKINO_MODEL_PRICING_CONFIG` 指向的配置，默认 `config/model-pricing.yaml`，查询使用已加载的配置。
按每次实际模型调用的开始时刻、输入量和适用阶梯选择单价，再使用 Decimal 计算人民币金额，
以十进制字符串返回；不能先汇总输入 Token 再套单次请求阶梯。
SDK 实际模型明细仍替代父总数，不将两者重复计费。

生成模型将输入分成普通输入、缓存输入、音频输入和缓存音频输入，按相应费率估算；
embedding 使用已确认的文字和图片输入子集。缓存与多模态子集从 inclusive input 中扣分，
reasoning 不另加输出费用。缺少适用价格或影响费率的必要 Token 子集时记为未计价，
不会猜测全部输入都是文字。当前配置没有独立缓存写入费率，明确发生缓存写入时也记为未计价。

金额使用当前服务加载的价格配置，不是账单实扣，也没有持久化每次调用的价格快照。
当前仓库费率的核对日期为 2026-09-15；更新配置并重新加载服务后估算可能改变，已记录 Token 不变。
失败调用有已确认用量时仍参与估算。只有部分用量的 `main_loop` 保留已计价小计，完整金额为 null。

Codex provider 的调用排除金额统计，模型行显示“不适用（Codex 订阅）”，Token 照常展示。
混合任务的 API 费用只包含可计价 API 调用，并单独报告排除的 Codex 调用数。
Codex-only 或空窗口的 API 金额为 null，不显示成消费零元。
没有 Token 到“本任务消耗每周额度百分比”的换算。
[官方定价说明](https://learn.chatgpt.com/docs/pricing)区分 API 价格与订阅用量，并说明模型、上下文、
推理、工具和缓存会影响额度消耗；账户额度快照也无法直接归属于同时执行的某一个任务。

## 查询接口

```http
GET /api/usage/overview?tenantId=local-default
```

可选 `fromStartTime`、`toStartTime` 为 Unix 毫秒，默认最近 30 天、最大 31 天，默认截止时间对齐下一整分钟。
SQL 使用 `started_at >= from AND started_at < to` 的 UTC 半开窗口；先精确筛选 tenant、run 和系列，
系列匹配 metadata.context.series_id 或 series_ids 数组成员，避免 series-A 错配 series-AA。
没有新增 taskType 请求筛选。接口沿用现有 tenant 读取授权及用户过滤。

按 started_at ASC、call_id ASC 排序，读取 20,000 + 1 行；多出的一行只用于截断标记，不计小计。
`truncated=true` 时整体/模型/run 的完整 Token 和完整 API 费用合计为 null，已读调用数、knownTokens 和已计价金额小计保留；
不凭未读记录增加 missingUsageCount，不能把已读小计解释为窗口全部消耗。

| 字段 | 含义 |
| --- | --- |
| source | 固定 `local`，表示本地用量账本，区别于模型绑定 `src` |
| status / message | `available` 或 `unavailable`；读取失败的用户提示 |
| fromStartTime / toStartTime / fetchedAt | 查询窗口和读取时刻，Unix 毫秒 |
| truncated | 匹配记录超过 20,000 条，当前数据只是已读部分 |
| totals | 已记录调用数、输入/输出、缓存读写、推理、总量、已知小计和缺失数 |
| models | 按实际 provider/model/category/operation/src 分组；历史 src 缺失仍为 null |
| runs | 真实 runId、两类 taskType、可确定的系列/视频身份及 Token 汇总 |
| totalTokens / knownTokens | 可确认的完整已记录合计或 null，以及已知 Token 小计 |
| missingUsageCount | 缺少可确认完整用量的不同调用数，包含 main_loop |
| totals.cost / models[].cost / runs[].cost | API 费用与计价覆盖范围；旧响应可缺失 |
| cost.currency | 固定 `CNY` |
| cost.estimatedAmount | 完整已记录 API 估算金额，缺价、部分用量或截断时为 null；排除 Codex |
| cost.knownAmount | 可确认的已计价 API 金额小计，无可计价记录时为 null |
| cost.pricedCallCount / unpricedCallCount / excludedCallCount | 按不同 call_id 计的已计价、未计价和排除金额的调用数 |

`/usage` 一次查询并复用按模型、任务/Chat 两个视图，展示 API 费用和计价覆盖，保留系列筛选和刷新。
PPE/debug Tasks 页在展开解析用量明细后，以真实 `videoParseTaskId` 查询同一接口，展示模型、src、Token 和 API 费用。
解析与索引执行中保持原有轮询，进入完成或失败状态后刷新；release 页不展示此调试入口。
数据库读取失败返回 unavailable 和未知调用数，不返回 available 的全零结果。
空窗口表示没有已记录调用，无法证明历史消费为零。

## 历史与验收限制

旧 `/api/jobs/usage/get` 已退出公开路由。旧统计表、价格与金额历史保留，不读取、回填或混入本地统计。
新表没有业务对象外键，删除任务/视频/消息不级联删除已记录用量。
本次估算使用现有 `model_usage` 的 Token，不读取旧金额历史，不新增数据库字段或迁移。
V2 预算、并发、heartbeat、lease、取消和 reserve/release 仍由原 manager 控制。

没有持久化队列、outbox 或采集完成标记；写入失败和进程终止可能丢记录，业务完成状态不能证明全部消耗已采集。
写入失败保留模型原结果并记录不含原始载荷的 warning；available 仅表示本次记录查询成功。
离线 SQLite、MySQL dialect 和页面测试分别提供局部证据，不能代替真实 TEST MySQL 或模型语义验收。
真实 U06 至少执行一条 V2 解析/index 和一次 Chat，对实际触发调用的 run/call_id、SQL、API 和页面逐层对账。
源码与离线回归不等于真实环境验收；真实模型、MySQL 和页面仍需按上述步骤对账。
