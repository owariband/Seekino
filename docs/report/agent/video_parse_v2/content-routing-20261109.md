# 普通上传自动判型验收 · 2026-11-09

[实现 PR #82](https://github.com/owariband/Seekino/pull/82) 和[设计](../../../design/03-product-reassessment/parse-pre-hook-router-trd.md)。输入取自 MyGO 第 1、4 集、两支 Karpathy 课程、三支 Persona OP 和七支爱音剪辑。每条给相同的三选一问题、原片名、时长和三张最高 360p 的原帧。测试时系列名、背景和对白留空。预期类型仅用于评分。

| 样本 | 官方 DeepSeek Flash 正确 / 样本数 | 本机 StartLux 4B Instant 正确 / 样本数 |
| --- | ---: | ---: |
| 剧情正片 | 2 / 2 | 2 / 2 |
| 课程 | 2 / 2 | 2 / 2 |
| 剪辑成片 | 9 / 10 | 1 / 10 |

官方路径共发起 14 次，13 次获得结果，全部符合预期。《For Anon Tokyo》的连接在返回前中断，保留为无结果。另用三组自制图片走实际 Gateway 请求，三类各正确一次。本机对照模型把九支剪辑判成剧情，显示更换模型需要重新验收。14 支片不能代表所有上传。

普通上传服务、SQL 任务、Prepare 与抽帧入口的联合回归覆盖了三种决策、超时后重试、失效租约、重复执行、显式配置和旧配置。此项使用临时本机数据库和可控分类返回值。真实模型判断另按上表统计。EP04 的整集 Understanding、Refine、检索与中点回看沿用 [#77 的显式策略基线](anime-material-recall-20261108.json)。此次未执行生产库写入或 MyGO 全系列重解析。

本机原始记录在 `outputs/verification/parse-pre-hook-router-20261109/`。样本清单在 `cases.json`，问题在 `question.json`，逐次结果与断线日志在 `official-corpus-results.jsonl`、`official-corpus.log` 和 `offline-results.jsonl`。媒体保留在本机。
