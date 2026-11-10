# 1. 问题与现状

编辑要在同一个素材库里找表演和手法。基线 `dev@2139cbaf` 的上传路由能判 script、course、clip，三者都提交普通时间线。内容搜索在选中 COMPLETE 任务后按同一个 Schema 读 Chunk，直接接入参考片会让不相关的素材进入内容搜索，甚至导致整次查询失败。

解析用途承接[双召回 #50 §3.3、§3.8](https://github.com/owariband/Seekino/pull/50) 和已合入的[上传路由 #82](https://github.com/owariband/Seekino/pull/82)。原开发记录见 [#83](https://github.com/owariband/Seekino/pull/83)，生成、索引和编辑回看的统一交付在 `feat/mcp-edit-references` → `dev` 的 [#85](https://github.com/owariband/Seekino/pull/85)。

# 2. 影响因素、假设与评价指标

| 条件 | 基线 | 验收 |
| --- | --- | --- |
| 旧上传、解析快照和幂等键 | 没有 profile 字段 | 旧快照仍得到 content，默认序列化与原值一致 |
| 重解析后视频用途改变 | 读取最新任务 | 内容读路径只选择最新、可见、属于 content 的版本，不能回退到旧版 |
| 显式参考档案 | 没有入口 | 任务可冻结单个全片窗口及视觉策略；超过 63 个 Chunk 提前拒绝 |

# 3. 改造方案与建模

上传配置到冻结快照沿用 `UploadService` 现有转换。将用途写入 `profile`，`content_kind` 继续服务普通解析。普通内容保留旧序列化，参考片按专门的任务策略执行。读路径先解析归属与当前版本，再按任务用途选择。复用 MySQL 已有 JSON 和事务，按任务版本逐个生效。

# 4. 对照实验与结果分析

旧配置按 content 读取并保持原有序列化。上传可显式选择 edit_reference，冻结时检查全片窗口能否容纳所有 Chunk。普通检索按视频最新任务筛选用途。

# 5. 试点验证与方案取舍

档案选择及读取隔离随 #85 整体验收。用同一个系列的两支上传验证普通内容能按动作检索、参考片能按手法检索，两者的引用均能下载对应原片。

# 6. 结果验收与闭环

2026-11-10 在独立 MySQL、ES 和媒体目录复验。两类素材同库时，默认正片搜索从 5 个已发布视频返回事件，另 7 个暂停任务不妨碍检索。手法查询读参考片专用投影。两路的协议回执见[编辑 MCP #85](https://github.com/owariband/Seekino/pull/85)。
