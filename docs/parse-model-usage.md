# Parse 模型用量与预估费用

视频建库任务按 **视频、模型、职责** 统计模型消耗，费用统一为人民币 `CNY`，页面显示 `¥`。Token 取自 Provider 返回的 `usage`；预估费用按实际用量和调用开始时适用的单价计算，不包含免费额度、赠金、资源包、商务折扣或账单核对。

## 统计范围

| 分类 | operation | 职责 |
| --- | --- | --- |
| parse | candidate_transcript | 独立 ASRAgent 阶段的转写；仅在启用 ASR 时产生 |
| parse | video_timeline | VideoUnderstandingAgent 视频语义解析 |
| parse | neutral_visual_evidence | fallback 分组视觉识别，每个实际请求单独计数 |
| parse | fallback_video_timeline | fallback 证据整理 |
| embedding | text_embedding | 建库文本向量 |
| embedding | video_embedding | 建库视频向量 |

`candidate_transcript` 是现有账本 operation 名，不代表 ASR 与理解 Agent 互调。内容审核
fallback 只复用 Parse Graph 已传入的 Transcript，不会自行发起
`candidate_transcript` 或其他 ASR 调用；因此 ASR 调用数和费用只来自启用后的独立
ASRAgent 阶段。

Query、Evaluator、Chat 和检索问题的 Embedding 不计入 Parse 账本。FFmpeg、切片、抽帧、数据库和 Qdrant 操作没有模型 Token。缓存复用没有发出模型请求时，不新增调用或费用记录；历史已经发生的调用仍保留。

## 模型价格

默认配置为 [config/model-pricing.yaml](../config/model-pricing.yaml)，自定义路径通过 `ANIVAG_MODEL_PRICING_CONFIG` 指定。`server/config/usage.py` 对配置做类型和范围校验，价格使用带引号的十进制字符串；计算使用 `Decimal`，不通过浮点数计价。

每个价格条目记录 Provider、完整模型 ID、分类、币种、每百万 Token 的单位、输入长度档位、输入／输出／缓存／音频或向量细分单价、价格版本生效时间及核对来源。DeepSeek 另有北京时间峰谷时段。新增或替换模型时，显式配置对应完整 ID；没有匹配条目时，费用保持未知。

2026-09-15 核对的默认价格如下，单位为 **元／百万 Token**：

| 模型 | 输入长度（千 Token）／时段 | 非音频输入 | 音频输入 | 非音频缓存命中 | 音频缓存命中 | 输出 |
| --- | --- | --- | --- | --- | --- | --- |
| doubao-seed-2-0-lite-260428 | [0, 32] | 0.6 | 9 | 0.12 | 1.8 | 3.6 |
| 同上 | (32, 128] | 0.9 | 13.5 | 0.18 | 2.7 | 5.4 |
| 同上 | (128, 256] | 1.8 | 27 | 0.36 | 5.4 | 10.8 |
| doubao-seed-2-1-pro-260628 | [0, 256] | 6 | — | 1.2 | — | 30 |
| deepseek-flash | 谷价 | 1 | — | 0.02 | — | 4 |
| deepseek-flash | 峰价 | 2 | — | 0.04 | — | 8 |

方舟价格以常规在线推理为口径，档位依据官方表头“千 Token”转换为整数区间；价格版本从本次核对日期起使用，不补算未记录的历史请求。[方舟模型价格](https://www.volcengine.com/docs/82379/1544106?lang=zh)、[模型列表](https://www.volcengine.com/docs/82379/1330310?lang=zh)

`doubao-embedding-vision-250615` 的文本输入为 `0.7`，图像输入为 `1.8`；视频帧消耗读取 Embedding 响应的 `image_tokens`。[方舟模型价格](https://www.volcengine.com/docs/82379/1544106?lang=zh)

DeepSeek 峰价为北京时间周一至周五 `09:00–12:00`、`14:00–18:00`，其余为谷价；此版本价格于北京时间 2026-09-10 12:00 生效，直接采用官方人民币价格。[DeepSeek 模型与价格](https://api-docs.deepseek.com/zh-cn/quick_start/pricing/)

输入中的缓存命中 Token 只计一次，推理 Token 已包含在输出 Token 中，不再累加。Lite 按音频和非音频分别计价；向量按文本和图像分别计价。缺少计算所需的细分用量时，费用返回 `null`，不使用文本单价套算全量多模态 Token。当前调用没有显式创建缓存存储，因此统计不包含独立的缓存存储时长费用。

每次调用保存实际 Token 细分及采用的完整价格快照。修改配置后只影响后续调用，历史汇总使用已保存的金额。

## 采集与持久化

Gateway 在 Ark SSE、DeepSeek Responses 和多模态 Embedding 的原始响应边界采集用量，然后解析和校验业务结果。业务 JSON 无效、Provider 失败或流不完整时，已经返回的用量仍计入。响应没有用量时，调用保留，Token 和金额标为未知。

Parse 层用上下文作用域传递片段编号和分类；ASR 并发任务各自复制上下文，避免不同任务和片段串账。进程内 ParseJobConsumer 在一次处理生命周期中绑定视频、任务、执行次数和租约代次，再由 Service 写入独立调用账本。

调用账本以 `callId` 唯一去重，保存 `jobId`、`attemptCount`、`leaseVersion`、`workspaceId`、`seriesId`、`videoId`、可空片段编号／ID、Provider、模型、职责、请求编号、开始时间、状态、Token、金额及价格快照。失败尝试和旧租约已经发生的消耗也保留；记账不修改 Job 状态。

独立的执行记录标记是否完整采集，区分缓存复用的真实零调用和历史无账本。采集写入失败不会重跑已经付费的模型调用，查询会标记账本不完整。默认模型客户端关闭 SDK 隐式重试，视频任务仍由 MySQL DB Queue 的重试机制调度。

新增表 `parse_model_call`、`parse_usage_attempt`。已有数据库使用受控的 `init-db` 创建缺失表；不会清空数据，也不修改现有表字段。

## 查询接口与页面

```http
GET /api/jobs/usage/get?workspaceId=local-default&jobId=<jobId>
```

请求先验证 Job 对当前 Workspace 的归属。返回 `ParseUsageVO`，DTO 在 `server/model/dto/parseUsageDTO.py`，公开 VO 在 `server/model/vo/parseUsageVO.py`。汇总范围是指定 Job 的全部执行尝试；调用记录、采集完成标记及当前执行次数通过同一条 SQL 读取，避免任务并发完成或重试时漏算最后一笔消耗。

| 字段 | 含义 |
| --- | --- |
| jobId、videoId、seriesId、seriesName、episodeOrder、originalFilename | 对应任务和真实视频身份 |
| currency | 固定 `CNY` |
| coverage | `not_started` 尚未执行；`recorded` 已完整记录；`partial` 部分或正在采集；`unknown` 历史无完整账本 |
| callCount、missingUsageCount、unpricedCallCount | 已记录调用数、缺用量调用数、有用量但不能计价的调用数 |
| totalTokens | 完整统计时的总 Token；不完整时为 `null` |
| estimatedCostCny | 完整统计和计价时的十进制金额字符串；未知时为 `null` |
| items | 按 Provider、模型、分类和职责汇总的行，包括调用数、输入／输出／缓存／推理／总 Token、金额和缺失数 |

任务页展开“模型消耗”后按需读取接口，按行展示模型、职责、Token 和 ¥ 预估费用。运行且展开时每 5 秒刷新，任务进入终态后立即刷新；终态账本仍为 `partial` 时每 15 秒继续读取迟到消耗，收起或记录完整后停止。未知字段显示“未记录”或“未估算”，历史无账本不会显示成零。

本功能不能恢复添加采集前的历史模型消耗。记录从新版 ParseJobConsumer 实际执行模型请求时开始，而非按上传日期判断：已有但尚未执行的排队任务也能记录；旧失败任务由新版 Consumer 重试时只记录新请求，旧执行没有账本则整项统计保持 partial；缓存复用不补算曾经产生缓存的费用。HTTP API 提供查询，写入由同一 AniVAG Server 进程内的 Consumer 完成，因此创建账本表后只需启动或重启使用新版代码的 API 进程。

真实模型费用验收需要运行启用采集后的 Parse 任务并与 Provider 用量核对。
