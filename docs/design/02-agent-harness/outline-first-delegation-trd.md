# 1. 问题与现状

**大纲优先、减少 query_video 委派 TRD。状态：初稿（2026-11-05）。** 分支 `fix/chat-outline-first-delegation`，PR 待创建。关联：[video-outline-trd.md](video-outline-trd.md)（#41，已合并，大纲与按“系列名 + 集数”寻址）；[outline-chunk-citation-trd.md](outline-chunk-citation-trd.md)（#46，大纲 Chunk 读后可引用）；[query-rewriter-model-trd.md](query-rewriter-model-trd.md)（#47，改写器）。无代码依赖，可独立合并；验收在 `dev` + #46 + #47 本地集成构建上进行。本需求属于 2026-11-05 发布验收发现的体验问题。

## 背景与触发场景

课程库演示（LOOP-08）中，学习路线与“在哪一段”类问题（LP01、LP02、SE02）需要跨章节定位。#41 之后 Supervisor 可以先读大纲，再用 `search_video_events` 精读并提交片段，正常耗时 40–200 秒。

## 实际与期望行为

期望：有大纲的视频上，路线、对比类问题由 Supervisor 直接用大纲与 Event 检索完成，不出现数分钟的等待。

实际：Supervisor Prompt 写有“复杂问题可直接把可独立调查的目标交给 query_video”（`agent/chat/supervisor.py:1142`，`dev` `978be570`）。路线问题被模型视为复杂问题，部分回合在读完大纲后委派 `query_video`；子任务内部连续调用 80 次以上工具，直到 180 秒模型超时（`VIDEO_MODEL_TIMEOUT`），Supervisor 再自己检索一遍。

## 负面影响

被委派的回合总耗时 311–431 秒，约为未委派回合的 3–10 倍；回答中还出现“子任务超时了，我改用直接的事件检索”这类工具过程叙述。

## 现状证据

2026-11-05 演示环境 19641（`dev` + #46 + #47 集成构建 `c9680e08`，v11 课程库），`outputs/verification/course-loop-dev-20261104/gates/loop08-{2,3,4}.json`：

| 回合 | 是否委派 query_video | 工具调用数 | 耗时 |
| --- | --- | --- | --- |
| loop08-2 LP01 | 是 | 91 | 431.0 秒 |
| loop08-2 LP02 | 是（超时） | 182 | 391.1 秒 |
| loop08-3 SE02 | 是 | 88 | 311.7 秒 |
| 其余 6 个 LP/SE 回合 | 否 | 9–12 | 39–214 秒 |

`dev` 构建 11-05 上午的 loop08-1 中 LP/SE 均未委派（9–26 次工具，22–113 秒）；api.log 中 Supervisor 委派 `query_video` 共 4 次（11-04 两次，11-05 两次），有完成记录的 3 次中 2 次耗时 180 秒。

## 本次边界

解决：Prompt 中 `query_video` 的委派条件——视频有大纲时先用大纲与 `search_video_events` 直接核验，不委派。

不解决：`query_video` 子任务自身的工具循环与超时设置；模型生成速度；`OUTLINE_TOO_LARGE` 后的重试行为。

必须保持：没有大纲的视频上 `query_video` 仍可用于多个独立目标的并行调查；工具注册不变。

# 2. 影响因素、假设与评价指标

| 因素 | 证据 | 假设 | 验证方法 | 影响的决策 |
| --- | --- | --- | --- | --- |
| Prompt 鼓励委派 | `supervisor.py:1142` | H1：限定“有大纲时不委派”后，LP/SE 回合委派率接近 0 | LP01、LP02、SE02 各重复 3 次统计 | 是否需要从工具层禁用 |
| 子任务成本 | 委派回合 80+ 次工具调用、180 秒超时 | H2：不委派时耗时回到 40–200 秒区间，回答质量不下降 | 同上，人工核对路线与时间码 | 是否采用 |

**指标：**

| 指标 | 定义 | 基线 | 目标 / 护栏 | 测量方法 | 证据入口 |
| --- | --- | --- | --- | --- | --- |
| 委派率（结果） | LP/SE 回合中 Supervisor 调用 `query_video` 的比例 | 3/9（集成构建） | 0/9 | api.log `tool=query_video` | `gates/` |
| 回合耗时（结果） | 提交到回答完成 | 委派回合 311–431 秒；最长 431 秒 | 全部 ≤ 240 秒 | `gates.py` | 同上 |
| 回答质量（护栏） | 路线覆盖相关章节、时间码可回看、无内部术语 | loop08-2 LP02 人工通过 | 不下降 | 人工核对 | 同上 |

# 3. 改造方案与建模

只改一句 Prompt：把“复杂问题可直接把可独立调查的目标交给 query_video”改为先看大纲——有大纲时路线、对比等跨章节问题由 Supervisor 用大纲与 `search_video_events` 直接核验，不委派；只有没有大纲且存在多个可独立调查的目标时才委派。

涉及：`agent/chat/supervisor.py`（Prompt 一句）、对应 Prompt 测试。不涉及工具实现、配置与数据。

# 4. 对照实验与结果分析

计划：同一演示环境与模型配置，集成构建加入本改动后，LP01、LP02、SE02 各运行 3 次，对比第 1 节基线。

# 5. 试点验证与方案取舍

演示环境即试点。若委派率仍大于 0，改为在有大纲的范围内不向 Supervisor 暴露 `query_video`；回退方式为恢复原句。

# 6. 结果验收与闭环

待实验完成后填写。
