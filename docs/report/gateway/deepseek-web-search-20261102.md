# DeepSeek 网页搜索接入验证（2026-11-02）

[PR #28](https://github.com/owariband/Seekino/pull/28) 直接面向 `dev`，主设计为 [TRD](../../design/03-product-reassessment/deepseek-web-search-trd.md)。只替换 `search_web` 的供应商，不包含 PR #18 的 ASR 或 PR #25 的正文流式实现。

## 接入与离线回归

`Supervisor → WebSearcher → POST https://api.deepseek.com/v1/tools/search` 已在真实页面执行。Server 注入独立配置，使用已有 DeepSeek key；移除 DDGS 及其仅由该包引入的 lxml/primp 锁定项。网页查询限 128 字符，视频查询仍为 500。保留 URL 校验、去重、摘要预算与既有引用规则；配置/额度错误不可立即重试，408/429/5xx 和传输错误标为可重试。

独立 `dev@56e79777` 基线的适配器、配置、Supervisor、Bootstrap、本地启动器回归 **432 passed，26.33 秒**，1 条既有 Starlette/AnyIO 弃用警告。首轮有两个既有完整响应测试桩未显式关闭 stream；补齐 `stream=False` 后通过，未因此引入 #25 代码。工具说明补充官方站点过滤后，Supervisor **137 passed**；该次缓存目录写入被本机权限拒绝，不影响断言。

```bash
PYTHONDONTWRITEBYTECODE=1 /Users/hypnos/workspace/agent/Seekino/.venv/bin/python -m pytest \
  tests/gateway/test_web_search.py tests/gateway/test_config.py \
  tests/agent/chat/test_supervisor.py tests/server/test_bootstrap.py \
  tests/server/test_local_services.py -m 'not live_agent' -q --tb=short
```

## 真实页面样本

独立实例：API `19626`、Web `15176`，专用空库 `seekino_deepseek_search_20261102_test` 及同前缀 ES 索引。主工作区配置和其他实例保持原状。模型为 `deepseek-flash / low`，实际使用 7 次生成调用；测试代码由 `900ad97c` 加[源码 SHA](evidence/deepseek-web-search-20261102/verification.json) 标识。

固定问题：“请先简短说明你的查询计划，然后联网搜索 Python 官方文档，用三点解释 asyncio.sleep 与 time.sleep 的区别，给出官方来源链接。”每组一个新会话；第二组只给工具说明增加 `site:` / `lang:` 用法。供应商缓存与检索结果变化不可控，这不是稳定性能对照。

| 项目 | 首轮（未提示站点过滤） | 补充过滤说明后 |
| --- | --- | --- |
| DeepSeek Search 成功次数 | 4/4 | 5/5 |
| 单次 Search 耗时（ms） | 463.9、551.7、302.6、355.6 | 418.2、342.8、838.8、380.6、379.2 |
| 每次有效结果数 | 5、5、5、5 | 3、4、3、5、5 |
| 模型调用数 | 3 | 4 |
| Server 整轮耗时（s） | 12.859 | 13.765 |
| 查询是否限定官方域名 | 否 | 五次均使用 `site:docs.python.org` |
| 最终引用 | 2 处未核实链接，无直接官方文档链接 | 1 个官方文档 URL 正常显示，仍有 3 处未核实链接 |
| 回答质量 | 未通过 | 未通过 |

另有一次直接 Search 探针：HTTP 200，约 400 ms，5 个结果。两轮页面加探针共 10 次非空成功 Search，按[官方费率](https://api-docs.deepseek.com/guides/search/#billing)估算 **$0.0075**，不含模型生成、不是账单。刷新后回答及来源保留，浏览器错误日志为空。7 次模型调用合计 input **42,456**、output **2,911** tokens；缓存输入 31,104、reasoning 902 分别是其子集。没有重跑 DDGS 作 A/B，也没有在 #28 计量 FTT。

## 失败案例与结论边界

首轮使用普通关键词，命中旧邮件列表与第三方资料；回答把 asyncio 的运行方式限定为必须在 async def 内 await，并混用了旧版 time.sleep 的信号行为。官方文档列出多种[协程执行方式](https://docs.python.org/3/library/asyncio-task.html#coroutines)，而 [time.sleep](https://docs.python.org/3/library/time.html#time.sleep) 在信号处理器不抛异常时会继续等待。第二轮找到了官方页，仍把 time.sleep(0) 泛称为空等；该说法忽略 Windows 的让出时间片行为。两个失败样本均保留，未靠挑选成功回答验收。

仅有摘要时，模型仍说过“核对原文”，引用了不在允许结果中的 URL。现有 URL 严格匹配和中文标点提取问题也未在本次更改。**供应商替换与配置接入已验证，回答和引用质量没有验收通过**；[C24](../../dev/problems.md#deepseek-web-search-c24) 保持 TODO，PR 保持 Draft。下一步应逐条对照工具返回 URL、模型引用及过滤结果，修复证据使用问题后再验收；不以搜索 API 的成功率替代问答质量。

脱敏记录：[请求与计时](evidence/deepseek-web-search-20261102/provider.log)、[源码/查询/用量/样本](evidence/deepseek-web-search-20261102/verification.json)、[JUnit](evidence/deepseek-web-search-20261102/backend.xml)。完整页面回答和截图保留在独立 worktree 的 `outputs/verification/deepseek-web-search-20261102/`。
