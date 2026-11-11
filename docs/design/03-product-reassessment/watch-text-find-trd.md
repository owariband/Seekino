# 1. 问题与现状

**查找范围是当前播放的单个视频（`Video`），覆盖整集。** 在《MyGO!!!!!》E01 按 `⌘F / Ctrl+F` 搜“高松灯”，只匹配 E01 已发布的台词和画面文字，“上一处”和“下一处”都在 E01 内跳转。切到 E02 后，原查找栏关闭，再打开时读取 E02。所属系列（`Series`）用于判断观看权限。

需求是观看页的片内文字查找。分支 `feat/watch-text-find`，[PR #105](https://github.com/owariband/Seekino/pull/105) 面向 `dev`。基于已集成的 [观看页](../../../web/src/features/player/WatchPage.tsx) 和 [Event 时间线](../02-agent-harness/sub-chunk-trd.md)，无待合并的前置需求。

MyGO 第 1 集 05:06.4 的双语字幕出现“高松灯”，22:07.5 的演职员表写作“高松 燈”。两帧已在本地原片核过。播放器已有毫秒跳转，用户却不能在当前一集输入文字并依次跳往每处出现的位置。两帧可见不代表对应解析版本采集了两行文字。

## 2. 影响因素、假设与评价指标

对白的原文和已有译文、画面文字均有原片时间。繁简和字间空白影响字面匹配。当前混合检索的排名和数量限制不能表达“下一处”。验收要求混合写法在查询和材料两侧均能匹配，命中按时间排列，原文保留，跳转误差在播放器允许范围内。仅出现在描述、角色名中的字不算。分清解析未就绪、读取失败和零命中。

## 3. 改造方案与建模

首次打开查找栏时，读取有权观看的本集最新已发布时间线，仅交付对白文本、画面文字及毫秒范围。浏览器用同一套繁转简和空白处理规则生成文本与查询的匹配键，保留原文。一个事件的不同语言可共同命中同一时间点。打开时支持快捷键和播放器按钮，选中、上一处、下一处直接定位当前播放器。复用现有鉴权、播放和正式解析数据，无数据库迁移。假名、别名、对话改写与语义匹配留待后续需求。

### 3.1 观看页读取哪份数据

以下按 2026-11-11 的本地实现补记。打开查找栏时，`VideoPlayer` 挂载 `VideoTextFind`，后者调用 `getVideoTextTimeline`，用当前会话发起一次请求。两个参数都必填。查询词留在浏览器。[播放器入口](../../../web/src/features/player/VideoPlayer.tsx) · [API 客户端](../../../web/src/api/client.ts)

```http
GET /api/videos/text-timeline?tenantId=<tenantId>&videoId=<videoId>
```

1. `tenantId` 限定资料所属租户，`videoId` 指定本次读取的唯一视频。服务端从该视频取得 `seriesId`，用观看页已有的 `read_series` 校验所属系列的访问权限。游客访问已撤回的公开系列会收到 404。[请求处理](../../../server/api/media.py)
2. `VideoParsePersistenceService.published_video_contents` 经仓储读取 MySQL 中本集最新未删除的解析任务。任务须已完成，各分段须完成 INDEX、当前内容版本等于已索引版本，数量和序号也须完整。任一条件不满足就返回 `{"ready":false,"cues":[]}`，播放器显示“本集文字尚未就绪”。[持久化服务](../../../server/service/video_parse_persistence.py) · [分段与版本查询](../../../server/repository/video_parse_retrieval.py)
3. `MediaService.video_text_timeline` 从各分段的 `timeline` 提取 `DIALOGUE.text_variants[].text` 和 `ON_SCREEN_TEXT.text`。按 `(startMs, endMs, source)` 分组，保留组内互异的原文，按起点、终点、来源排序。发布完整但没有这两类文字时，返回 `{"ready":true,"cues":[]}`。[事件投影](../../../server/service/media.py) · [响应类型](../../../server/model/dto/mediaDTO.py)

下面是隔离接口测试的完整响应。区间左闭右开，起止值均为原片上的整数毫秒。[测试输入与断言](../../../tests/server/test_video_text_find.py)

```json
{
  "ready": true,
  "cues": [
    {"startMs": 3456, "endMs": 5678, "source": "dialogue", "texts": ["高松 燈です", "我是高松灯"]},
    {"startMs": 12345, "endMs": 13456, "source": "screen_text", "texts": ["演員表　高 松 燈"]}
  ]
}
```

### 3.2 输入“高　松 灯”后怎样跳到原片

`createVideoTextSearch` 为每条原文计算一次匹配键。它去掉空白（含全角空格），将字母转小写，再用 OpenCC 的 `TSCharacters` 单字表繁转简。用户输入“高　松 灯”时，查询键是“高松灯”。匹配器在各条原文的键内做 `includes` 匹配，返回每个位置第一条命中的原文。`VideoTextFind` 显示结果并处理切换。同一条对白的日文和中文都能命中，列表仍只占一行。上例得到两个位置。[匹配规则](../../../web/src/features/player/videoTextSearch.ts) · [查找与切换](../../../web/src/features/player/VideoTextFind.tsx)

第一次按 Enter 将 `3456` 传给播放器已有的 `seekTo`，播放器在媒体边界内把毫秒换成秒，写入 `video.currentTime`。再按 Enter 跳到 12.345 秒的演职员表，Shift+Enter 回到 3.456 秒。没有命中时在本地显示空结果。若读取失败，面板显示错误并允许重试。[播放器跳转](../../../web/src/features/player/VideoPlayer.tsx) · [真实媒体浏览器用例](../../../web/tests/video-text-find.mjs)

采用整集文字一次读取，是因为“下一处”需要按原片顺序遍历全部字面命中。已有语义检索按相关性返回有限候选。浏览器匹配避免每次输入往返服务器或等待模型改写，代价是首次打开时的响应大小随单集文字量增长。长视频首开延迟尚待实测。

## 4. 对照实验与结果分析

| 指标 | 基线 | 验收目标 | 本地结果 |
| --- | --- | --- | --- |
| 同系列各集的查找范围 | 无片内入口 | 仅返回当前 `videoId` 的文本 | 同一游客读取同系列的 E07 得到两条文字，读取已就绪的 E01 得到零条。[接口用例](../../../tests/server/test_video_text_find.py) |
| 同集对白与画面文字的繁简及空白匹配 | 无片内入口 | 两种来源均命中，反例零条 | Chrome 输入“高　松 灯”命中两处，另搜“测试空结果QZX”为零，同一事件的双语文本只占一处 |
| 选中结果跳转 | 有独立播放器 seek | 实际媒体时间等于命中起点 | 真实 MP4 的 currentTime 随 Enter、Shift+Enter 和点击到达 3.456 秒、12.345 秒 |
| 未授权与陈旧成果 | 现有观看权限与发布版本合同 | 不交付正文 | 撤回公开返回 404，最新内容版本缺发布回执返回未就绪，其他集已就绪、正文独立 |

## 5. 试点验证与方案取舍

观看页按需加载 OpenCC 单字表（构建产物 gzip 20.36 kB）并读取一次本集时间线。本地 Chrome 多次修改查询和切换结果共发出一次正文请求。查找过程不调用模型。浏览器测试覆盖游客、手机尺寸、快捷键和实际媒体；解析与索引使用隔离的已知数据。[浏览器用例](../../../web/tests/video-text-find.mjs) · [接口与版本用例](../../../tests/server/test_video_text_find.py)

localhost 的 MyGO 测试快照含两集。Chrome 输入同一查询，E01 的 386 条正文命中 05:01.166，E02 的 388 条正文命中 11:20.820 的台词和 22:00.000 的演职员表记录。三个位置读取的媒体时间均精确到对应毫秒。逐帧复核 E02，11:20.820 确有该台词，21:58.000 可以看见「高松 燈」，22:00.000 已看不到该姓名。片尾偏差来自已有解析稿，此版本沿用该稿的起点。同集另搜「监　督 柿本」，点选 02:10 的画面文字，播放器到达 02:10.166，原帧可见「監督 柿本広大」。[本地播放器及原片验收记录](../../report/server/evidence/watch-text-find-local-20261111.json)

发布前只读核对了公开 MyGO 第一集当前 24 个已发布分段，内有 377 条对白、36 条画面文字。“高松灯”有两处，05:05.000 的自我介绍（高松燈です）和 14:39.833 的笔记本署名（高松 燈）。22:07.5 的演职员表在原片可见，现有文字稿未收录该姓名。上线冒烟据此核对实际播放器。[公开第一集](https://seekino.hypn0s.cloud/watch/90d72272-eb0f-5067-ae63-4653240a0e13/b9f02d4b-dabb-5283-9fa2-0717862e53b3?tenantId=platform-public)

## 6. 结果验收与闭环

2026-11-11，前端 648 项和定向后端 64 项通过，后者包括单集搜索和公开观看两个真实 Chrome 流程。与最新 dev 的媒体路由合并后，含相邻媒体访问与传输用例在内的 129 项定向回归通过。TypeScript 类型检查与生产构建通过。上线验收用公开 MyGO 第一集的新游客会话复查查询和原片时间。当前状态见[工作台](../../dev/workbench.html)。
