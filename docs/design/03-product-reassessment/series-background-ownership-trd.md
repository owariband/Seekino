# 1. 问题与现状

**系列背景资料归属 TRD。状态：初稿（2026-11-05）。** 分支 `refactor/series-background-ownership`，PR 待创建。关联：[series-background-api.md](../01-mvp/series-background-api.md)（原背景接口合同）；[series-overview-trd.md](../02-agent-harness/series-overview-trd.md)（`feat/series-overview`，Chat 系列概览读取这些字段）。无合并顺序依赖。

## 背景与触发场景

用户在上传解析前为系列填写简介和背景资料，用于帮助解析、检索与问答。2026-11-05 讨论系列概览时发现：用户本人无法说清 `description`、`important_extra_data`、`character_bible` 三者的区别，也不确定系列简介由谁更新。用户不清楚的字段，界面同样无法讲清楚。

## 实际与期望行为

期望：一个系列只有一份由用户维护的背景资料，外加一句简介；每个字段在界面上有唯一的编辑入口；Agent 读到的就是用户看到并能修改的内容；系统生成的内容与用户填写的内容可以区分。

实际（2026-11-05 只读调查，引用均已核对）：

| 字段 | 写入方 | 编辑入口 | 读取方 | 问题 |
| --- | --- | --- | --- | --- |
| `name` | 用户，仅创建时 | 创建表单 | Chat `series_context`、解析 | 不可改名；Agent 只看到它 |
| `display_name` | 用户，仅更新时 | 背景编辑器 | 前端、MCP 检索 | 解析与 Chat 看不到用户可见的名称 |
| `description` | 用户，创建与更新 | 创建表单、背景编辑器 | 解析、Summarize、Chat、证据 | 从不自动生成；与各集 Summarize 生成的 `display.description` 同名不同义 |
| `important_extra_data` | 用户，创建与更新 | 创建表单“背景资料”、背景编辑器 | 解析（含 `all_speakers`）、Summarize、Chat、证据 | 创建接受任意 JSON，更新强制 `{format, content}`，校验不对称 |
| `character_bible` | 仅创建接口 | 无（界面只读展示） | 解析、Chat、证据 | 更新接口拒绝该字段，界面创建表单不发送；实际人物设定写在 `important_extra_data` 里 |

证据：

- 更新白名单不含 `character_bible`：`server/service/library.py:95-102`。
- 界面创建表单只发送名称、简介、语言与背景资料：`web/src/features/library/LibraryPage.tsx:257-261`；背景编辑器只读展示人物资料：`web/src/features/library/SeriesBackgroundEditor.tsx:240`。
- 原合同已把 `characterBible` 列为“旧数据兼容”：`docs/design/01-mvp/series-background-api.md:53`；同文第 34 行称只有三个字段可更新，代码另允许 `display_name`、`visibility`。
- 真实数据：MyGO 两个系列 `characterBible=[]`，完整人物设定 12581 字符写在 `importantExtraData.content`：`docs/dev/problems.md:2073`。
- 背景合并：`engine/common/background.py:23-66` 把 `important_extra_data`、旧 `seriesBible` 文本与 `character_bible` 拼成一份背景文字。
- 解析与 Summarize 不回写系列字段：`server/service/video_parse_task.py:62-67`、`server/service/video_parse_summarize.py:212-225`。

## 负面影响

- 用户不知道人物设定该填在哪里，也无法修改已填的人物资料。
- Agent 用 `name` 指称系列，用户看到的是 `display_name`，回答中的系列名称与界面不一致。
- 系列 `description` 与各集生成的简介同名，系列概览（[series-overview-trd.md](../02-agent-harness/series-overview-trd.md)）若不区分来源，会把用户写的介绍当成视频内容。

## 本次边界

解决：明确并在界面与接口上统一“简介 + 一份背景资料”的归属；`character_bible` 停止作为独立可写字段，旧数据继续可读；Agent 使用用户可见名称；创建与更新的背景校验一致；更新 [series-background-api.md](../01-mvp/series-background-api.md)。

不解决：删除 `character_bible` 列或迁移旧数据（需用户另行确认）；自动生成系列简介；`name` 改名能力。

必须保持：已有系列的背景内容按原文读取，解析与检索使用的背景文字不变；`important_extra_data` JSON 中的 `all_speakers` 等自定义字段原样保留。

# 2. 影响因素、假设与评价指标

| 因素 | 证据 | 假设 | 验证方法 | 影响的决策 |
| --- | --- | --- | --- | --- |
| 两个背景容器 | 见上表 | H1：所有实际人物设定都已在 `important_extra_data`；非空 `character_bible` 只存在于早期数据 | 统计 v11 与开发库中 `character_bible` 非空的系列数 | 是否需要迁移脚本 |
| 名称不一致 | Chat 用 `name` | H2：Agent 使用 `display_name`（缺省回落 `name`）即可与界面一致 | 单元测试 + Chat 回答抽查 | 寻址键仍用唯一的 `name` |
| 校验不对称 | 创建与更新规则不同 | H3：创建按更新规则校验不会拒绝现有前端请求 | 前端创建表单请求形状核对、接口测试 | 是否保留创建时的旧格式兼容 |

**指标：**

| 指标 | 定义 | 基线 | 目标 / 护栏 | 测量方法 | 证据入口 |
| --- | --- | --- | --- | --- | --- |
| 可写背景字段数（结果） | 用户可写的背景容器个数 | 2（其中 1 个界面不可写） | 1 | 接口白名单与界面核对 | 代码与测试 |
| 背景文字不变（护栏） | 已有系列 `prepare_background_text` 输出 | 当前值 | 逐字节一致 | 对 v11 副本所有系列前后比对 | 待测 |
| Agent 系列名称（结果） | Chat 上下文中的系列显示名 | `name` | `display_name` 优先 | 单元测试 | 待测 |

# 3. 改造方案与建模

| 概念 | 字段 | 写入方 | 用途 |
| --- | --- | --- | --- |
| 系列名称（键） | `name` | 用户，创建时 | 唯一寻址键（Chat 按“系列名 + 集数”定位视频） |
| 显示名称 | `display_name`（缺省 `name`） | 用户 | 界面与 Agent 回答中的称呼 |
| 系列简介 | `description` | 用户 | 一句话介绍；在 Agent 上下文中标注为“用户填写” |
| 背景资料 | `important_extra_data = {format, content}` | 用户 | 人物、术语、关系等全部设定；唯一可写背景容器 |
| 旧人物资料 | `character_bible` | 不再写入 | 只读兼容，继续并入背景文字 |
| 各集简介 | `video_parse_content.display.description` | Summarize | 视频实际内容摘要，与系列简介分开标注 |

改动：

1. 创建接口不再接受 `character_bible`（或接受但拒绝非空值，按前端与测试核对结果决定），`important_extra_data` 按更新规则校验。
2. Chat `series_context` 增加 `displayName`；Prompt 用显示名称称呼系列。
3. 背景编辑器的人物资料区标注为旧数据，提示人物设定写进背景资料。
4. 更新 [series-background-api.md](../01-mvp/series-background-api.md) 的可更新字段与字段归属。

# 4. 对照实验与结果分析

计划：H1 用只读 SQL 统计各库 `character_bible` 非空的系列；护栏“背景文字不变”对 v11 副本所有系列计算改动前后 `prepare_background_text` 并逐字节比较。结果：待测。

# 5. 试点验证与方案取舍

低风险的接口与文案修改，以接口测试、前端类型检查与 v11 副本的背景文字比对代替独立试点。若 H1 不成立（存在非空 `character_bible` 的在用系列），先补只读展示与迁移方案，不删除数据。

# 6. 结果验收与闭环

待实施。
