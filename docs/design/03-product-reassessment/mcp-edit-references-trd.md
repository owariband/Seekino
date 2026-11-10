# 1. 问题与现状

编辑只给剧本，需要在同一个 Seekino 素材库找到可剪的表演、可借鉴的手法，并回到两者的原片。一个 MCP 服务在 `/mcp` 同时提供这六个工具。编辑可以用同一个客户端走完搜索、看依据、自选入出点和下载。

主设计、实现与整体验收统一挂在 [PR #50](https://github.com/owariband/Seekino/pull/50)，分支 `docs/editing-skill-style-reference` 直接面向 `dev`。原先的取片、用途识别、参考片解析和整合工作分别记录在 #81、#83、#84、#85，相关代码均已并入本 PR。

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

从 #50 检出后运行下列测试。端口指向本机专用 Elasticsearch 8.x 测试实例，命令需要 FFmpeg 和 ffprobe。测试通过正常 API 启动应用，在临时数据库中上传两支各两秒的合成片，执行 Parse V2 和索引发布，再用同一 MCP 客户端读取新生成的带引证风格卡、下载自选表演与参考原片。它使用固定模型响应，验证 HTTP、SQL、ES 和 FFmpeg 的衔接。索引名每次随机生成，结束时释放。默认使用临时 SQLite；设置 `SEEKINO_EDITING_TEST_DATABASE_URL` 可在本机空的 MySQL `*_test` 数据库执行同一用例。

```bash
SEEKINO_TEST_ELASTICSEARCH_URL=http://127.0.0.1:19451 uv run --locked python -m pytest -q tests/server/test_editing_mcp_flow.py
```

本机编辑库里已有的浅版卡标作 `legacy_technique_groups`，保留实际已发布的观察和版本。正式 Refine 现写入 `scope`、`signals` 及指到具体毫秒的 `examples`。回归在独立库重放 #50 的八段观察和片主盲选 B 组用过的原卡。当前提示词未新增付费推理。后续由片主发起 Claude 验收，编辑只拿剧本、MCP 和本机 `motion-showreel` 工具，验收者另持已知答案。成片里的动作连续性、发缘、眼镜和手指的质量、实际借用及返工由该次验收记录。
