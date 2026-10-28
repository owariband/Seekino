# Server 模型 Token 用量统计设计

更新日期：2026-10-09。阶段结果：**U01–U06 的本地 Token 统计已完成；本轮双 chunk 统计、系统通路及整 chunk 播放验收通过，逐事件时间精度未通过**。
八列表 Schema、两个业务归属类型与 `model_usage` 表名保持；`usage` / `metadata` 合同未重设计。
本轮初始源码是 `c8c5d2cd9d28485dea0e5990de8813ef39a71a6f` 加开始时全部 dirty 的 **538 tracked 冻结文件**，已包含 common 迁移；运行中叠加五生产 / 五测试的十个必要补丁。同期 ROOT 的 V1/Qdrant 清理未纳入真实服务，已有并行改动全部保留。
默认 90132 ms 媒体按原 60000 ms 配置拆成两段，task `559ce12a-ea72-4d6d-867e-e7261e21be60` 的 parent、两 child 与 Interval 均 COMPLETE，ES 两文档。Parse 于 05:57:33 UTC 在本轮补修前靠既有默认重试与 Seed 恢复完成；该解析没有在十补丁定版后重跑。
随后修复 Provider schema 适配、V2 媒体读取与 Supervisor 原始意图传递，经同 namespace 冷重启，保留原解析数据执行三轮默认 Chat；最终 `a9f70ef3-5151-43f8-8cd9-c04de4e6f0bb` 命中两个当前 chunk 并生成两段视频，范围为 0–60000 / 60000–90132 ms。模型、理解与 fps=1 未替换或下调，`profileOverrides=false`。
全 workspace **99 calls / 已知小计 1,204,853 Token / missing 2**；公开 input/output/total 均为 **null**，页面显示已知小计及输入/输出“—”。两个真实 DeepSeek schema 400 没有返回 usage，保持未知；七条 failed 的已知 175,035 Token 与前两轮 Chat 的部分/失败结果全部留账。
独立 `audit-multichunk-final.json` 为 passed / `fullFlowPassed=true`；此通过限定 owner/JSON、SQL/API/UI、预算释放、两 receipt/ES/provenance、固定 key 重放，以及整段播放的结构和全局毫秒边界。**第二段当前 version2 的 Interval updates 在 60004–60030 ms 的 26 ms 内承载多句对白，逐事件定位精度未通过**；不能把合法 bounds 等同逐句视频时间准确。Chat/DTO 保存原值，旧 version1 已被覆盖且 ASR 原始证据未持久化，无法追认混入阶段；本轮不猜测乘 1000 或改历史 SQL。
本轮源码/测试仅十文件，生产 +106/-10、测试 +264/-11，维护既有五产物；没有改八列 Schema 或模型配置，没有新增 commit/push。Provider schema 的真实 Responses probe 单独 1 call / 2,740 Token，无 owner、不写主 SQL，与 99 次主账分开。
初始冻版 23 测试文件 894 passed / 1 skipped，唯一 MySQL timing 后在独立库 1 passed / 0 skipped；schema 35 passed、冻结媒体 158 passed、Supervisor ROOT154 / 冻结152 passed 分开记录。最新 ROOT 六文件 195 passed 仅离线兼容，不把并行清理写作真实全链覆盖。
前轮 532 文件 + 四补丁的 49 calls / 357,825 Token、endpoint 的 686 passed / 三 callback 10,403 Token，以及更早失败均保留为各自历史，不混入本轮账。
当前原始证据在 `/private/tmp/seekino-usage-multichunk-20261009/`，各阶段、限制和历史结果见[证据索引](../../server/report_docs/model-token-usage-stages-evidence.md)；本设计继续作为唯一 Schema 与行为合同。

<a id="scope"></a>
## 1. 目标、职责与当前证据

用 Server 自有 SQL 表记录 Gateway 已采集的 Token 用量，替换模型消耗页面的 Langfuse 数据来源。
当前目标仅统计 Token，不建价格引擎，不记录 USD/CNY，不换算费用，不建设完整 trace。
统计范围是实际落入新表的模型调用；无记录的历史消耗无法还原，也不能解释为零。

| 层 | 本次职责 | 继续沿用的机制 |
|---|---|---|
| Gateway | 发出已有 `ModelCallUsage` | provider adapter、Token 归一、调用 ID、context snapshot、observer fanout |
| Server host/service | 确定业务归属，绑定一个稳定的 SQL observer | V2 lease/admission/budget，Chat runtime、线程内 context |
| Server repository/model | 追加记录、幂等校验、SQL 范围过滤 | session factory、短事务、UTC 时间类型、MySQL/SQLite 分支 |
| Server API/VO | 查询已记录用量并投影 Token 统计 | 现有 `/api/usage/overview` 和窗口参数 |
| Web | 按模型、业务任务显示 Token 与未知/截断状态 | 当前页面、一次 overview 请求、刷新与两种视图 |

本轮已完成本地统计及保留默认 Parse → ES → 修复后媒体/检索 Chat 的真实系统通路验收，SQL、API 与页面精确对账。common 已包含于538文件初始冻版；同期 V1/Qdrant 清理仅另做 ROOT 离线验证。逐事件时间精度缺口不被统计通过结论掩盖：

- [Gateway 用量合同](../../gateway/usage.py#L19)已有 `TokenUsage`、`SDKModelUsage` 和 `ModelCallUsage`。
- [Gateway 组合 observer](../../gateway/usage.py#L177)按 callable 身份去重，`combine=True` 才保留父 observer。
- [Bootstrap](../../server/bootstrap.py)装配共享 `ModelUsageRepository` / `ParseUsageService`，tracing 为 None。
- [Usage service](../../server/service/parse_usage.py)绑定真实 owner，显式投影调用 JSON 并短事务追加，overview 从新表聚合。
- [现有 API](../../server/api/parse_usage.py#L13)及[路由](../../server/router/jobs.py#L11)提供 `GET /api/usage/overview`。
- [当前页面](../../web/src/features/usage/UsagePage.tsx)展示已记录 Token、未知与小计；typecheck 与直接回归通过。

Server 负责本设计的持久化、归属与聚合；Gateway adapter 已有采集。当前 endpoint 修复只集中解析请求路由与编码身份，实际响应模型与 Token 采集合同保持。
Langfuse 的目标处理是停用装配与消费，保留现有模块和依赖，不开展无关清理。
V1 保留显式 opt-in 入口和旧表；新统计不读取、回填或改写 V1 历史。
后续实时识别 sub-agent 有[现有 OKR](../dev/OKR.md#L13)，本批不预建未来业务类型。

<a id="schema"></a>
## 2. 已认可的八列表结构

表名：`model_usage`。一条 Gateway `call_id` 只对应一行追加记录。
新 ORM 名为 `ModelUsageRecord`，按既定修改树放在 `server/model/parse_usage.py`；仓储文件为 `server/repository/model_usage.py`。
Gateway 的 `ModelCallUsage` 是已有调用事件合同，保持名称及接口不变。

| 列 | SQL 类型 | 约束/来源 | 用途 |
|---|---|---|---|
| `call_id` | `VARCHAR(36)` | PK、非空；Gateway UUID | 唯一调用记录与幂等键 |
| `workspace_id` | `VARCHAR(64)` | 非空；Server owner scope | workspace 隔离和查询 |
| `task_type` | `VARCHAR(16)` | 非空；CHECK 两个已确认值 | 业务归属 |
| `run_id` | `VARCHAR(256)` | 非空；真实任务/助手消息 ID | 单次业务执行筛选，沿用公开 `runId` |
| `started_at` | `TIMESTAMP(3)` | 非空、UTC；Gateway Unix 毫秒转换 | 统计窗口与稳定排序 |
| `usage` | `JSON` | 非空、固定对象形状 | Token 主用量与 SDK 按模型明细 |
| `metadata` | `JSON` | 非空、固定对象形状 | 模型信息、调用状态与白名单 context |
| `created_at` | `TIMESTAMP(3)` | 非空、UTC；Server 写入时刻 | 落库时间 |

两个业务值的定义已经确认：

- `video_parse`：V2 视频解析及其 index/embedding；`run_id = video_parse_task_id`。
- `chat_retrieval`：当前整个助手问答业务，包括 Supervisor 直接回答、Web 分支和视频检索；`run_id = assistant_message_id`。

`chat_retrieval` 不承诺每条模型调用都执行了检索。
Chat 内部 query embedding、rerank 沿用助手消息归属，不创建独立 retrieval 类型或新 run。
V2 chunk/interval/prefix 子任务仍归同一视频解析任务，子任务身份留在 context。
Server 定义这两个业务值；Gateway 只承载 opaque context，不认识业务 enum。

以下为已确认的 MySQL DDL；2026-10-09 已在独立测试库实际建表并核对，生产数据库不在本次联调范围：

```sql
CREATE TABLE model_usage (
    call_id VARCHAR(36) NOT NULL,
    workspace_id VARCHAR(64) NOT NULL,
    task_type VARCHAR(16) NOT NULL,
    run_id VARCHAR(256) NOT NULL,
    started_at TIMESTAMP(3) NOT NULL,
    usage JSON NOT NULL,
    metadata JSON NOT NULL,
    created_at TIMESTAMP(3) NOT NULL,
    PRIMARY KEY (call_id),
    CONSTRAINT ck_model_usage_task_type
        CHECK (task_type IN ('video_parse', 'chat_retrieval')),
    INDEX ix_model_usage_workspace_started
        (workspace_id, started_at, call_id),
    INDEX ix_model_usage_workspace_run_started
        (workspace_id, run_id, started_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;
```

Repository 必须写入 `created_at`；不依赖数据库连接的本地时间或隐式默认值。
复用 [UTC_TIMESTAMP](../../server/model/video.py#L40)：ORM 使用 aware UTC，MySQL 落为毫秒精度。
[Engine](../../server/database.py#L217)已有连接级 UTC 设置；新表加入绝对时间 Schema 检查。
本表不继承当前使用 BIGINT 双时间列的 `TimestampedRecord`。
采用 `utf8mb4_bin`，使 workspace/run/call 字符串按精确身份比较，避免默认排序规则忽略大小写。
不加 `updated_at`：这是追加型记录，不维护可变累计数。
不加外键：两个业务类型指向不同已有对象，且删除业务对象不应级联删除已记录用量。
空字符串、ID 长度、UUID 形状、JSON 类型及归属一致性由 DTO/repository 校验。
业务 ID 写入与查询均拒绝前后空白，不做静默 trim，避免 MySQL PAD SPACE 比较把不同输入当成同一身份。
系列筛选暂使用 JSON；本批不增加系列列、生成列或额外索引。

### 2.1 `usage` 的固定形状

```json
{
  "tokens": {
    "input_tokens": 100,
    "output_tokens": 20,
    "total_tokens": 120,
    "cached_input_tokens": 30,
    "reasoning_tokens": null,
    "text_input_tokens": null,
    "image_input_tokens": null,
    "audio_input_tokens": null,
    "cached_audio_input_tokens": null,
    "cache_write_input_tokens": null,
    "cache_write_5m_input_tokens": null,
    "cache_write_1h_input_tokens": null
  },
  "sdk_model_usage": []
}
```

`tokens` 是现有 `TokenUsage` 的固定字段映射；没有有效主用量时整个 `tokens = null`。
`sdk_model_usage` 始终为数组；元素固定为 `{"model_name": "实际模型", "tokens": null或同形Token对象}`。
SDK 元素的 `tokens = null` 表示该模型用量未知，不能写成全零；命名与主 tokens、metadata.model_name 一致。
从 [SDKModelUsage](../../gateway/usage.py#L63)投影时明确省略 `cost_usd`。
禁止直接序列化整个 `ModelCallUsage`：它含 MappingProxyType context，SDK 明细也包含本批不收的费用。

### 2.2 `metadata` 的固定形状

```json
{
  "model_name": "provider实际返回或adapter持有的模型名",
  "provider": "实际provider",
  "src": null,
  "operation": "text_embedding",
  "status": "completed",
  "request_id": null,
  "ended_at": 1791400000123,
  "granularity": "provider_request",
  "usage_source": "provider_usage",
  "usage_scope": null,
  "context": {"series_id": "series-A", "stage": "index"}
}
```

`model_name/provider/operation/status/request_id/ended_at` 来自实际 Gateway 记录；结束时间为可空 Unix 毫秒。
`src` 是可空的调用来源标记，仅保存已有 host 明确给出的来源或真实 adapter 来源。
目前 `ModelCallUsage` 没有独立 `src` 字段；没有明确来源就写 `null`，不根据 model alias 虚构。
不把 `src` 推导成业务类型、provider 名或 Token 来源；这些信息已有独立承载。

| 信息 | 定义 | 当前 adapter 值 |
|---|---|---|
| `granularity` | 一条调用记录的粒度 | 普通 provider 为 `provider_request`；Codex/Claude Code 为 `sdk_turn_run` |
| `usage_source` | 主 Token 用量的出处 | `provider_usage`、`codex_thread_total`、`claude_result_usage`；主用量未知时 `null` |
| `usage_scope` | 用量覆盖的执行范围 | Claude Code 为 `main_loop`；其余当前为 `null` |

来源见[默认 tracker](../../gateway/usage.py#L250)、[最终 emit](../../gateway/usage.py#L393)、[Codex](../../gateway/codex/agent.py#L356)、[Claude](../../gateway/claude_code/agent.py#L699)。
这三个字段描述不同信息，保留既有可空合同；不新增 SQL 列或来源 enum。
旧 Langfuse 子 observation 的 `claude_model_usage/sdk_model` 不属于 adapter 的主记录值。

`context` 只投影已有身份和阶段的白名单，不复制任意 host payload：

- 范围/关联：`series_id`、`series_ids`、`video_id`、`video_ids`、`session_id`、`conversation_id`、`user_message_id`。
- V2 子任务/检索范围：`video_parse_task_ids`、`chunk_parse_task_id`、`interval_parse_task_id`、`video_chunk_id`。
- 阶段/版本：`execution_kind`、`execution_task_id`、`category`、`stage`、`sequence_no`、`lease_version`、`attempt_count`、`retry_count`、`index_generation`。

主归属以独立列为准，context 不重复保存 `assistant_message_id/video_parse_task_id`；投影前拒绝与 owner 冲突的身份。
缺失白名单值不补造；数组规范化为 JSON 数组，身份字符串保留精确值。
不存 prompt、回答正文、工具参数、原始 response、连接串或凭据。

<a id="recording"></a>
## 3. 采集与幂等写入

目标主路径：Server 绑定 owner scope → Gateway 发出最终用量 → 稳定 SQL observer 投影 → repository 短事务追加。
不在模型执行期间持有数据库事务，不把新统计接入 V2 预算扣减或 lease 状态机。

1. V2 沿用 [capture_video](../../server/service/parse_usage.py#L60)，在 host scope 明确 `video_parse` 与真实任务 ID。
2. Chat 在[实际执行模型的线程](../../server/service/agent_chat.py#L749)绑定 `chat_retrieval` 与助手消息 ID，并安装 SQL observer。
3. 嵌套检索沿用父 observer 与 owner；其[局部检索 context](../../server/service/video_retrieval.py#L188)仅补系列、视频、阶段。
4. Observer 用 `combine=True` 与已有订阅组合，且始终复用同一 callable 实例。
5. 最终事件到达后校验 scope，显式构造两个 JSON 对象并转换 UTC 时间，再追加一行。

[model_call_context](../../gateway/usage.py#L125)合并父 context 并冻结 snapshot，适合继承 Chat owner。
[V2 manager.capture](../../server/service/video_parse_model_calls.py#L120)仅在可选 usage_observer 非空时另装订阅。
[生产构造](../../server/service/video_parse.py#L895)没有传该参数，当前路径仅安装 control，不覆盖外层 SQL observer。
U03 保持 manager、reserve/release、heartbeat 与 budget 行为，验证它们和新外层采集同时生效。
稳定 callable 配合 Gateway 身份去重，可避免同一 observer 被父子 scope 重复注册。
没有受支持 owner 的独立检索不写新表，不虚构任务 ID。
V1 `capture` 中的旧 `job_id/run_id` 不视为 V2 owner，不写入新表。

Repository 采用既有[短事务追加范本](../../server/repository/parse_usage.py#L74)，并补齐 scope 冲突检查：

- 首次 `call_id`：插入完整八列；一次 SDK 多模型运行也只插入一行。
- 重复 `call_id`：只有 `workspace_id/task_type/run_id` 全部一致，才作为幂等重放返回已有行。
- 任一归属不同：明确拒绝并记录冲突；不得返回另一个 workspace 的行，不覆盖已有记录。
- 并发 PK 冲突：回滚后重新读取，并执行同样的归属校验；其他完整性错误不能吞成成功。
- 同 owner 重放不更新原 JSON 或 `created_at`，不把重试记录重新计算成新增调用。

数据库写失败记录 warning，保留模型调用的原业务结果；Gateway 已有[observer 隔离](../../gateway/usage.py#L136)。
日志仅留 call/scope/异常类型等排障信息，不打印两个 JSON 的原始内容。
没有持久化队列、事务 outbox 或采集完成标记，因此进程终止和写失败可能丢记录。
解析子队列失败、Chat COMPLETED/FAILED 状态均不能证明该业务的全部消耗已采集。

<a id="query"></a>
## 4. SQL 查询与聚合口径

Repository 查询必须先构造完整 SQL WHERE，再排序和限量：

- `workspace_id` 必填，精确匹配；所有读取均不能跨 workspace。
- 使用 `started_at >= from AND started_at < to` 的 UTC 半开窗口。
- `run_id` 可选、精确匹配；本批不新增请求 `taskType` 筛选参数。
- `series_id` 可选：精确等于 `metadata.context.series_id`，或精确属于 `series_ids` JSON 数组。
- JSON 过滤沿用[现有 repository](../../server/repository/agent.py#L214)的 MySQL `JSON_CONTAINS` / SQLite `json_each` 惯例。
- 按 `started_at ASC, call_id ASC` 稳定排序，最多读取 `20,000 + 1` 行。
- 多出的第 20,001 行只用于 `truncated=true`，不计入当次已知小计。

禁止先读取 workspace 全量/20k 行，再用 Python 过滤 run 或系列；那会遗漏本应匹配的记录。
禁止对 JSON 文本做 contains/LIKE，避免 `series-A` 错配 `series-AA`。
新表 PK 已保证每 call 一行，聚合仍以 `call_id` 作为调用计数单位。
只读取新表；旧 parse_model_call 与 Langfuse observation 不拼接或回填。

### 4.1 Token 的求和规则

1. 普通调用使用主 `tokens`；`input_tokens` 已包含缓存和模态输入。
2. cached、cache write、text/image/audio 是 input 的子集；reasoning 是 output 的子集，均不再次加入 total。
3. 若有 SDK 按模型明细，按明细替代父主用量参与总和和模型分组，不做父子相加。
4. SDK 多模型共同分享一个 `call_id`：整体调用数为 1，每个模型分组独立按不同 call_id 计数。
5. 任一参与项的核心 input/output/total 用量未知时，受影响的完整合计为 `null`；`knownTokens` 只报告可确认项的小计。
6. 可空 subset 独立传播未知：未知缓存、推理使对应 subset 合计为 null，不能显示为 0，也不能单凭它们未知而否定已确认的 input/output/total。
7. Claude `main_loop` 记录的已知数字只证明主循环小计；受影响的整体、模型、run 行的 `inputTokens/outputTokens/totalTokens` 为 null，`knownTokens` 保留已确认小计，`missingUsageCount` 按 call_id 计一次。不新增 coverage 字段。

`missingUsageCount` 按缺少可确认完整用量的不同 call_id 计数，不按模型明细数量计数。
`truncated=true` 时，整体、模型、run 的完整 Token 合计为 null，`knownTokens` 保留已读取记录小计；`callCount` 为已读的不同 call_id 数量。
截断不凭未读记录增加 `missingUsageCount`，由既有 truncated 字段单独提示；不能把已读小计当成统计窗口全部调用或全部 Token。
失败调用仍可包含 provider 已确认用量；状态失败不自动丢弃或清零 Token。
数据库读取失败时报告不可用与 message，不能返回 available 的全零结果。
空窗口可以报告 0 条已记录调用；措辞必须说明这是记录范围，不是历史消费为零。

<a id="view"></a>
## 5. API 与页面目标

继续使用现有 overview 路由，workspace/time/series/run 参数及 Unix 毫秒时间窗沿用。
默认最近 30 天、最大 31 天；默认截止时间按下一整分钟对齐，沿用[当前窗口校验](../../server/service/parse_usage.py#L80)。
U04 已将源码数据源切为 SQL 并同步 DTO、VO、API 投影；U05 已同步 Web 合同与展示。
Token 合同保留调用数、输入/输出、缓存读/写、推理、总量、已知小计、缺失数与截断状态。
模型行按实际 provider/model/operation 统计，任务行保留 `runId`，用 `taskType=video_parse|chat_retrieval` 表达归属。
去掉费用、货币、计价缺失字段和 Langfuse 配置状态；数据库失败沿用 status/message 表达。
响应固定 `source='local'`，`status='available'|'unavailable'`；读取失败用已有 message，移除 LF 专有状态。
页面保留按模型/按任务视图与刷新，展示“已记录调用”“已知 Token 小计”“结果截断”。
不展示 SDK 未提供的模型明细，不把父配置 alias 标成 SDK 实际子模型。
不把本地数据库状态、trace 配置或实现细节塞入正常用户操作流程。

<a id="stages"></a>
## 6. 阶段计划、下一批树与授权

| 阶段 | 交付与责任 | 验收 | 当前状态 |
|---|---|---|---|
| U01 设计/check-out | Server 设计文档、证据索引、plan-map 与职责/授权核对 | 八列、归属、未知规则和阶段树可审阅 | 已确认；本轮执行指令授权 U02–U05 |
| U02 持久化 | Server ORM、DTO、repository 与数据库用例 | 离线证据及真实 TEST MySQL 存储验收 | 六文件已实施；离线通过；2026-10-09 MySQL 8.4.11 独立测试库 139 passed / 2 skipped，真实存储验收通过 |
| U03 运行采集 | Server V2/Chat 接线、组合 observer、停用 LF 消费 | 嵌套继承、一次落库、失败保业务与 V1 隔离 | 本轮99条唯一SQL：Parse17、三Chat82；owner/JSON/预算通过，七failed已知175035与两unknown保留；49/429/409等为独立历史 |
| U04 SQL overview/API | Server 聚合、DTO/VO/API 合同、错误映射与文档同步 | SDK/缓存/未知/截断/API 结果正确 | 源码与 SQLite → API 验收完成；补齐部分 SDK 用量、同名 run 分型及 HTTP 错误合同，41 passed |
| U05 统计页面 | Web 合同、页面、直接回归用例 | 两视图仅展示 Token，未知与空窗口可辨 | 源码、typecheck与42个Web回归通过；本轮实际两视图/series/刷新为99/known1204853/missing2，公开input/output/total均null，与API一致，页面脚本/HTTP错误均空 |
| U06 真实联调验收 | Server/Web 实际流程与用量核对 | 至少真实 V2 与一次 Chat 的已触发调用可追到表和页面 | ✓ 538冻结文件+十补丁范围统计/系统通路/整chunk播放通过；保留补修前Parse→修复后Media/Chat，99/known1204853/missing2。逐事件精度未通过；common已纳入初始冻版，ROOT并行清理仅离线 |

依赖：`U01 → U02`；U02 后 U03 与 U04 可并行；`U04 → U05`；`U03 + U05 → U06`。
真实存储环境缺失会限制 U02 的 MySQL 验收与 U06；不阻塞文档、代码开发或离线单测。
2026-10-09 用户已授权联调及任务内修复；本轮使用专用 `seekino_usage_multichunk_20261009_052927_test`，默认双chunk、SQL/ES、API/Web与最新检索Chat的统计/通路已验证。逐事件精度失败不被通路通过覆盖；上一轮532+4、毫秒库及独立embedding I/O各保留历史范围，不混用数据。
本轮各阶段已按实际树check-out；源码实施、隔离环境及任务内最小修复均有明确授权。U01–U06统计完成，地图无正在执行的统计阶段；逐事件精度后续验证与同期ROOT V1/Qdrant清理未因此获得真实验收状态。

U02 已执行修改树（沿用原 proposal）：

```text
Seekino/
├── server/
│   ├── model/
│   │   ├── [M] parse_usage.py                 ← 新增 ModelUsageRecord，保留旧表模型
│   │   └── dto/
│   │       └── [M] parseUsageDTO.py           ← 新增八列 State 与 ModelCallUsageBatch
│   ├── [M] database.py                       ← 注册新表绝对 UTC 时间检查
│   └── repository/
│       └── [A] model_usage.py                ← 通用追加、scope 幂等校验、SQL 范围读取
└── tests/
    └── server/
        ├── [M] test_database.py              ← DDL/约束/索引/UTC 回归
        └── [A] test_model_usage_repository.py ← 幂等冲突、精确范围、JSON 与截断回归
```

范围：4 个生产文件、2 个测试文件；新增 2、修改 4，无删除、无重命名。
DTO 改动有必要：旧 ParseModelCallState 强制 V1/V2 lease 字段，LF batch 也不是新 SQL 合同。
新 State 只映射已认可八列；batch 只表达 records 与 truncated，不把 ORM 对象泄漏给 service。
U02 预计生产代码新增/删除合计约 220–360 行，测试约 180–300 行，实施前按实际 checkout 收敛。
U02 源码批次不装配 observer、不切 API、不改页面；注册模型沿用 init-db，2026-10-09 已获授权于隔离测试库实际建表。
U03 已执行树（原 11M，加用户本次授权补修 1M，共 12M）：

```text
Seekino/
├── [M] config.yaml                           ← 将现有 tracing.enabled 设为 false
├── server/
│   ├── [M] bootstrap.py                      ← 装配 SQL repo/observer，停止 LF 装配
│   └── service/
│       ├── [M] parse_usage.py                ← 固定 owner、stable callable、combine=True
│       └── [M] agent_chat.py                 ← 在实际执行线程绑定助手消息 owner
└── tests/
    ├── gateway/
    │   ├── [M] test_config.py                ← 默认 trace 停用与配置兼容
    │   └── [M] test_model_config_v2.py        ← 模型配置兼容
    └── server/
        ├── [M] test_bootstrap.py             ← SQL 装配、LF 未启用
        ├── [M] test_parse_usage.py           ← scope、继承、写失败隔离
        ├── [M] test_agent_chat_service.py    ← 整体问答/嵌套检索归属
        ├── [M] test_video_parse_worker.py    ← 外层采集与预算/lease 并存
        ├── [M] test_video_parse_model_calls.py ← 本次追加授权；补 provider model 响应替身
        └── [M] test_video_retrieval_service.py ← 父 owner 与局部系列/阶段合并
```

V2 capture_video 与 retrieval model_call_context 已有；保留可选 tracing 参数为 null，不改对应 runtime 或 manager 源码。
U04 同步现有六处文档的相关段落：`README.md`、`server/README.md`、`.env.example`、
`docs/parse-model-usage.md`、`docs/api-ui-views.md`、`docs/design/frontend-api.md`，避免读者仍按 LF 配置。
这些文档已随 API 源码切换同步；保留 LF 模块/依赖，不泛化修改 agent/provider adapter。
U05 已执行树如下：

```text
Seekino/
└── web/
    └── src/
        ├── api/
        │   ├── [M] contracts.ts              ← 对齐 Token-only overview 合同
        │   └── [M] client.test.ts            ← 对齐响应 fixture 与运输合同验证
        └── features/
            ├── usage/
            │   ├── [M] UsagePage.tsx         ← Token 小计、两视图、未知/截断提示
            │   └── [M] UsagePage.test.tsx    ← 直接页面回归
            └── tasks/
                ├── [M] ParseUsageDetails.tsx      ← 现有未挂载组件的合同/编译对齐
                └── [M] ParseUsageDetails.test.tsx ← 对应 fixture 与行为对齐
```

`client.ts` 只是既有参数运输，不改；闲置 money helper 保留，不顺手清理。
ParseUsageDetails 目前未挂载，不预建 TaskDetails 功能，不修改 TasksPage 或额外迁移 props/status。

<a id="acceptance"></a>
## 7. 验证与交付判据

- U01：检查七个显式锚点、内部源码链接、DDL 与已认可八列一致，以及阶段授权和状态无混写。
- U02：离线 SQLite 验证追加/重放/冲突、workspace/run/系列精确筛选、同毫秒排序和 20k 截断。
- U02：MySQL dialect 编译核对 TIMESTAMP(3)、JSON、CHECK、两索引及 utf8mb4_bin，只作为离线证据。
- U02：真实 TEST MySQL 验证八列、UTC/JSON 往返、并发幂等/冲突、精确 SQL 先筛选再限量；SQLite/DDL 编译不能替代。
- U02：2026-10-09 已获联调授权，在 MySQL 8.4.11 独立测试库完成 139 passed / 2 skipped；两项跳过仅为 SQLite 参数的 MySQL 并发专项。
- U03：离线回归确认父 scope 继承、fanout 不重复、预算/control 正常、SQL 失败保留业务结果。
- U03：显式 V1 opt-in 的旧 job 不进入新表；原 V2 预算/lease 直接用例保持通过。
- U04：普通调用、cached subset、SDK 多模型、main_loop、unknown、失败与截断均有聚合/API 用例。
- U05：前端 typecheck、已有页面测试及目标场景回归；费用与 LF 配置提示不再出现。
- U06：至少一次真实 V2 解析/index 与一次真实 Chat，对实际触发的 usage 核对 call_id/归属、JSON 与页面小计。
- U06：不要求一次 Chat 同时触发 direct/Web/retrieval；各分支由独立自动化覆盖，记录真实触发范围与环境证据。

阶段完成需要代码、直接验证与证据共同支持；文件存在、历史测试或数据库空结果均不等于完成。
无 Schema 关键未决项。本轮保持原默认 V2、理解、ASR/视觉/embedding endpoint 与 fps=1；pipelineVersion 名称 `m2-seconds` 不决定执行模式或媒体单位。实际 canonical embedding 为 `doubao-embedding-vision-250615` / 1024 维，SDK route 与编码身份保持区分；两 receipt 的 semantic/vision vectors 与 ES 均核对通过。
真实范围是开始时 c8c5d2c + dirty 的 538 文件（包含 common）及十个已记录补丁，保留补修前完成的17条 Parse 调用，再验证修复后的媒体与 Chat；不是十补丁版重新执行 Parse。同期 ROOT V1/Qdrant 清理不在真实范围。
独立审计为 passed / `fullFlowPassed=true`：99 唯一 call_id，92 completed / 7 failed，已知 1,204,853 / missing 2，公开 input/output/total 均 null；原始已知 input 1,023,503、output 181,350 仅诊断小计，不伪装公开完整总量。失败已知175,035与两次schema400未知均保留。
解析预算 used_call_ids 精确等于17条 SQL调用，active为空且终态释放；两当前回执、ES文档与 Chat parseProvenance 对应。最新默认 Chat 双段命中/播放，固定key重放保持消息、答案、99个SQL ID和payload及用量不变；早期部分回答与失败评估留账。
真实HTTP页面为99次/已知小计1,204,853、输入输出“—”、缺失2，11模型分组/4run；两视图复用、series筛选和实际刷新通过，访问pageErrors/httpErrors均空，无mock或额外网页Chat。
验收覆盖结构、全局毫秒范围与整chunk播放边界，**不覆盖已确认失败的逐事件精度**：第二chunk当前version2在60004–60030ms内多句对白，仅26ms，来源为Interval模型updates；Chat/DTO无二次转换。旧内容被覆盖、ASR原证未持久化，不能追认具体混入步骤。后续应强化 understanding/refine 的毫秒与全局偏移约束并以真实 reParse 验证，不凭启发式改历史值。本轮默认V2未登记keyframes，图片覆盖为unobserved。
初始23文件894passed/1skip（27.32s），唯一MySQL timing在独立库1passed/0skip补验；schema35passed（2.72s）及真实无owner的独立probe2740Token不混主账；冻结媒体158passed，Supervisor ROOT154/冻结152分别记载。ROOT最新六文件195passed（19.96s）仅离线；不得扩展至并行清理真实运行。
本轮没有真实rerank、SDK明细、同名run跨类型或无owner主账覆盖，这些保留离线边界。三轮Chat35/301660、19/202021、28/298496与Parse17/known402676共同构成99主账；schemaprobe、前轮49/357825、endpoint三callback10403和更早账本均分开。
最终 `acceptance-final.json` 七项统计/通路检查均true，eventPrecisionCertified=false、cleanupCompleted=true。临时API7827于07:00:09.322656UTC以SIGTERM正常关闭，Web59930于07:07:53.234UTC以SIGINT/exit0关闭；07:08:24.967914UTC的cleanup确认9622/5174不监听，3306/6333/9200复用依赖继续运行，测试数据和证据保留。
当前证据入口为 `/private/tmp/seekino-usage-multichunk-20261009/` 的 `environment.json`、`source-manifest.json`、`source-manifest-patched.json`、`source-patches.json`、`parse-result.json`、`intent-retry/chat-result.json`、`intent-retry/chat-replay-verification.json`、`intent-retry/semantic-answer-review.json`、`timeline-precision-proof.json`、`audit-multichunk-final.json`、`ui-verification.json`、`offline-tests.json`、`timing-mysql-tests.json`、`own-schema-tests.json`、`deepseek-schema-probe.json`、`media-tests.json`、`own-supervisor-tests.json`、`root-focused-tests.json`、`independent-code-review.json`、`acceptance-final.json`、`cleanup.json`、`api-stop-7827.json` 与 `web-stop.json`；详细生命周期见证据索引最新节。
前轮532+4与更旧结果保留于索引各历史节。旧两轮临时目录因daemon recovery已消失，原路径和结果不代表文件仍可读。本轮没有新增commit/push，原有源码、并行变化与用户改动全部保留。
