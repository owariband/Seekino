# DeepSeek 网页搜索替换调研

核对日期：2026-11-02。范围是替换 Seekino `search_web` 的搜索供应商；本次只读官方文档和当前源码，未发出真实 Search / Fetch 调用。

**可以保留 DeepSeek Flash low 与当前 Agent 工具循环，把宿主 `search_web` 接到官方独立 Search REST。** 它使用已有 DeepSeek API key，不需要再调用一次生成模型。[Search / Fetch 指南](https://api-docs.deepseek.com/guides/search/)

## 官方接口合同

| 项目 | 已确认内容 |
| --- | --- |
| 搜索地址 | `POST https://api.deepseek.com/v1/tools/search` |
| 鉴权 | `Authorization: Bearer <DeepSeek API Key>`；JSON 请求。文档未要求专用 Search key 或另行开通；本机现有 key 能否成功调用仍须实测 |
| 最小请求 | `{"query":"asyncio.sleep site:docs.python.org","count":5,"need_content":false}` |
| 查询限制 | `query` 为 1–128 个 Unicode 字符，不得全空白；`count` 为 1–50，默认 10 |
| 可选输入 | `ori_question` 同样限 1–128 字符；`localization` 是可选 IP；无需本轮接入 |
| 分页 | 公开请求 schema 没有 page、offset 或 cursor，不能假设支持翻页 |

地址与鉴权见[指南](https://api-docs.deepseek.com/guides/search/)；请求字段与限制见 [Search API Reference](https://api-docs.deepseek.com/api/search/)。公开语法支持 `site:`、`-site:`、引号精确短语、排除词、`lang:`、`after:` 和 `before:`；未知请求字段会被忽略。[查询语法](https://api-docs.deepseek.com/guides/search/#query-operators)

成功响应为 `{"results":[...]}`，无匹配为 HTTP 200 与空数组。每项提供 `title`、`url`、`snippet`、`summary`、`content`，另有排序分、站点和日期信息。`snippet` 是面向展示的短摘要；`summary` 是供模型阅读的较长摘要。`need_content=false` 时 `content` 为空；即使请求正文，正文不可获得时仍可为空。摘要不能当作已读取的逐字原文。[响应 schema](https://api-docs.deepseek.com/api/search/#responses)

## 错误、费用与执行预算

Search / Fetch 错误体使用 `error.message/type/code`，`param` 为 null。400 是请求不合法，401 是 key 无效，402 是余额不足；408、429 和 5xx 属于官方建议等待后重试的类别。504 表示下游超时。Fetch 另有安全策略 403、无法取得可用内容的 404。文档未给出 Search 固定完成时限或延迟 SLA。[错误合同](https://api-docs.deepseek.com/guides/search/#errors)

两种接口均按每 1000 次成功请求 **$0.75** 计价；`count` 不改变每次价格。只有 HTTP 200 且 Search 有非空结果 / Fetch 有内容才计费，失败、空结果及超时不收费。账户 Search 限 100 QPS，Fetch 限 25 QPS，彼此独立。[计费与限流](https://api-docs.deepseek.com/guides/search/#billing)；[价格页](https://api-docs.deepseek.com/quick_start/pricing/#search--fetch-pricing)

价格页写明，通过 Harness / Claude Code / Codex 的服务端搜索也按上述 API 执行，并自北京时间 2026-11-01 开始按请求收费。因此本次不能沿用“免费搜索”的假设。[价格说明](https://api-docs.deepseek.com/quick_start/pricing/#search--fetch-pricing)

## 与内置工具、Fetch 的边界

Responses API 的 `tools:[{"type":"web_search"}]` 只对 `deepseek-v4-pro` 生效，**对 `deepseek-flash` 会被忽略**。这与宿主自行调用独立 REST 是两条接入方式；保留当前 Flash low 时应选后者，无需迁移模型协议或整个 Harness。[Responses 工具兼容表](https://api-docs.deepseek.com/guides/responses_api/#tools)

Fetch 是独立的 `POST /v1/tools/fetch`：输入 `url`，返回 `title` 与 Markdown `content`；URL 最长 8192 字符，须为有 host 的 HTTP(S)，不允许包含用户名密码。替换搜索供应商并不要求增加逐页 Fetch；有全文核对需求时再单独设计调用与预算。[Fetch API Reference](https://api-docs.deepseek.com/api/fetch/)

## 对 Seekino 的最小改动建议

当前 `gateway/web_search.py` 直接调用 `DDGS(...).text()`，没有指定 backend；查询上限 500、结果上限 20、摘要上限 600 字符。Supervisor 消费 `WebSearchHit(title,url,snippet)` 并保留工具循环、工具结果预算及引用过滤。[当前 Gateway](../../gateway/web_search.py)、[Supervisor](../../agent/chat/supervisor.py)

建议在 Gateway 换 HTTP 适配，保留 `WebSearchHit` 和现有 URL 验证/去重；将查询输入合同收紧到 128，让超长输入明确失败而不是截断搜索操作符；`max_results≤20` 可直接映射 `count`；模型所读摘要优先取 `summary`，再套既有长度预算。保留总执行预算，不因增加重试突破超时。鉴权/余额等错误不应一律声称可立即重试，也不应静默回退 DDGS。这些是基于上述合同的接入建议，尚非已实现行为。

先验证请求路径与 header、128 字符边界、空结果、非法 JSON/schema、401/402/429/504、URL 过滤和超时；再以当前 Python 官方文档问题做一次真实 UI 验收，核对实际供应商、返回链接、工具耗时与最终引用。真实 key 可用性、延迟、搜索质量及引用效果均待测；更换供应商本身不等于修好现有引用格式或模型事实错误。

## 补充：原 DDGS 与用户选择

本项目原锁定 `ddgs==9.16.0`，来自 [deedy5/ddgs](https://github.com/deedy5/ddgs/tree/v9.16.0)。这是无需供应商 API key 的元搜索库，公开文档列出多种后端；实际可用集合还受版本禁用标志影响。原调用未传 backend，使用 auto，库按可用引擎并发搜索、合并排序。DuckDuckGo 适配器直接访问 HTML 搜索入口并提取 title/href/body，并非其官方付费 API。[版本文档](https://pypi.org/project/ddgs/9.16.0/)、[DuckDuckGo 适配器](https://github.com/deedy5/ddgs/blob/v9.16.0/ddgs/engines/duckduckgo.py)、[后端调度源码](https://github.com/deedy5/ddgs/blob/v9.16.0/ddgs/ddgs.py)。

此前建议直接移除 DDGS 过度绑定了本机选择。当前方案保留默认 DDGS，显式配置可选 DeepSeek；主模型与搜索方式独立，不根据 key 是否存在切换。以上旧调查与首版建议按形成时点阅读，实施及后续实测以 TRD 和报告为准。
