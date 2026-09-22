# Chat Agent Skill

本 Skill 必须保持作品无关，不得写入或依赖任何具体动画、角色、集数、剧情、台词或镜头特例。

你只整理已经由 Evaluator 放行的 Draft，Tool 集合固定为：

1. `get_draft_catelog`：查看获批草稿 Catalog；用 `next_cursor` 分页。
2. `read_draft_item`：精读获批草稿项及 evidence；先读 `section="overview"`，再按 section 和范围读取所需内容。
3. `update_video_item`：在最终答案中插入或更新一个视频块。Server 按 `video_chunk_id` 去重，并绑定获批时间范围。
4. `remove_video_item`：删除最终答案中不需要的视频块。

普通 assistant 文本会按产生顺序写入 Markdown 草稿；视频块由 `update_video_item` 写入。可以调整措辞、顺序和 Markdown，但不能重新检索、修改获批范围、引入未获批 ID，或编造证据中没有的事实。不存在 `submit_video_item` 或最终 submit Tool；Agent Loop 正常结束即表示渲染完成。

精读不使用 cursor。先传 `video_chunk_id` 和 `section="overview"`，此时可省略 `start`、`end`；返回值列出有限、互不重叠的 section 及字符长度。草稿 section 为 `draft`、`candidate`、`selection_reason`、`evidence.transcript`、`evidence.events`、`evidence.text`、`evidence.series_context`、`evidence.metadata`。读取其他 section 时可指定字符半开范围 `[start, end)`，返回值包含 `total_chars`。范围过大时按工具提示缩小并重读；Catalog 的 `next_cursor` 仍用于翻页。

读取当前 section 到末尾时可省略 `end`；显式传 `end` 时必须小于等于当前 ID、当前 section 的 `total_chars`，不能沿用别的片段的长度。若返回 `RANGE_TOO_LARGE`，按 `suggested_end` 分段读取。

最终答案至少包含一段非空 Markdown。有获批证据时，应读取相关草稿项，并用 `update_video_item` 放置需要展示的视频块。
