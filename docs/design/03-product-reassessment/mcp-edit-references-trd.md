# 1. 问题与现状

找到正片表演后，编辑仍得翻参考片才能知道某种手法是否值得借用。#84 的代表片段、风格卡与来源属于同一解析版本；现有 retrieve、browse_events、read_evidence 提供正片事件。编辑需要直接用自己的话寻找参考，并播放返回的原片区间。

分支 `feat/mcp-edit-references`，依赖[参考片解析 #84](https://github.com/owariband/Seekino/pull/84)、[档案 #83](https://github.com/owariband/Seekino/pull/83)。沿用[双召回主设计 #50](https://github.com/owariband/Seekino/pull/50)。本分支包含 [#81](https://github.com/owariband/Seekino/pull/81) 的原始区间取片提交，可从一份检出运行两条链路。

实施 [PR #85](https://github.com/owariband/Seekino/pull/85)。

# 2. 影响因素、假设与评价指标

手法说明要有可检索的语义、清楚的原片边界与可看的证据。逐级拉远很容易被误写成镜头内推拉。验收统计固定问题里的首个可用结果、逐帧可指认的切点、来源版本及媒体播放。不同租户、未发布与已替代版本的结果必须为零。

# 3. 改造方案与建模

复用本机 MCP 身份、访问限制和媒体端点。先限定当前可查参考任务，在独立 ES 中按自然语言召回，逐项回 MySQL 核实原片、任务版本、索引代次和观察指纹。搜索结果、整片卡的代表段及信号例子给出同一 `evidence_ref`。编辑沿 `read_evidence` 核对原生帧时间，用 `media.play_url` 播放精确区间，或用 `media.clip_url` 下载自选区间。

# 4. 对照实验与结果分析

2026-11-10，本机测试 API 通过握手发现并实际调用全部六个工具。5 集已发布、7 集暂停时，默认剧本查询返回了 9 条事件。自选 1200 毫秒原片解出 29 帧。登场手法原句首位命中 P5R，搜索和卡片指向同一段 1133 毫秒、34 帧的原片，浏览器完整播放后停止。篡改索引代次后下载得到 HTTP 409。[协议及回放回执](../../report/server/editing-mcp-code-loop-20261110.json)

# 5. 试点验证与方案取舍

正式工具中，`list_series`、`retrieve`、`read_evidence` 沿用基础 MCP，`browse_events` 来自 #77，两个参考片工具来自 #50 §3.5。#81 扩展已有的 `read_evidence`。这一步只交付解析所得的手法，`templates` 返回空数组。模板版本、写回以及模板库的筛选参数按 #50 §3.8 暂缓。

#50 §3.3 指定逐段采用 Weekend B 提示词。正式代码加载[逐段原文](../../../agent/video_parse_v2/edit_reference_pick.txt)和[整片 Refine 原文](../../../agent/video_parse_v2/edit_reference_refine.txt)。前者替换实测参数，后者接收结构化笔记。分帧排版、失效引用校验、区间 MP4 的 inline 响应属于实现细节。本轮优先复验提示词原文与源时刻。具体怎样呈现登场、如何选择配色和字体由编辑决定。

# 6. 结果验收与闭环

本机编辑库里已有的浅版卡标作 `legacy_technique_groups`，保留实际已发布的观察和版本。正式 Refine 现写入 `scope`、`signals` 及指到具体毫秒的 `examples`。回归在独立库重放 #50 的八段观察和片主盲选 B 组用过的原卡。当前提示词未新增付费推理。后续由片主发起 Claude 验收，编辑只拿剧本、MCP 和本机 `motion-showreel` 工具，验收者另持已知答案。成片里的动作连续性、发缘、眼镜和手指的质量、实际借用及返工由该次验收记录。
