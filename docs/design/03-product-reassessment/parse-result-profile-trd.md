# 1. 问题与现状

编辑要在同一个素材库里找表演和手法。基线 `dev@2139cbaf` 的上传路由能判 script、course、clip，三者都提交普通时间线。内容搜索在选中 COMPLETE 任务后按同一个 Schema 读 Chunk，直接接入参考片会让不相关的素材进入内容搜索，甚至导致整次查询失败。

分支 `refactor/parse-profile` 面向 dev，承接[双召回 #50 §3.3、§3.8](https://github.com/owariband/Seekino/pull/50) 和已合入的[上传路由 #82](https://github.com/owariband/Seekino/pull/82)。参考片的生成、索引和编辑端回看由下游实现。

实施 [PR #83](https://github.com/owariband/Seekino/pull/83)，参考片生产接入见 [#84](https://github.com/owariband/Seekino/pull/84)。

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

先跑配置、上传、任务与 MCP 范围测试，随后在下游独立测试实例选取单支参考片。初稿只约定档案选择和读取隔离。效果取证以实际入库及切点回放为准。

# 6. 结果验收与闭环

2026-11-10 在独立 MySQL、ES 和媒体目录复验。两类素材同库时，默认正片搜索从 5 个已发布视频返回事件，另 7 个暂停任务不妨碍检索。手法查询读参考片专用投影。两路的协议回执见[编辑 MCP #85](https://github.com/owariband/Seekino/pull/85)。
