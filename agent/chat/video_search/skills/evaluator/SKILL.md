# Evaluator Agent Skill

本 Skill 必须保持作品无关，不得写入或依赖任何具体动画、角色、集数、剧情、台词或镜头特例。

你只有 `retrieve` 和 `get_context_canvas`，不能修改草稿。

必须通过 `get_context_canvas(collection="draft")` 从第一页完整遍历目录直到 next_cursor 为空；逐条使用 collection="draft"、reference=video_chunk_id、section="overview" 获取全部 section，然后完整读取每个 section。必须覆盖全部目录页和每项每节的字符区间，目录最后一页或 overview 本身不等于完成审核。必要时独立 retrieve，并用 collection="retrieval" 精读遗漏证据；不能直接写入草稿。

草稿包含 draft、candidate、selection_reason、evidence.transcript、evidence.events、evidence.text、evidence.series_context、evidence.metadata。各项各节的长度独立；读取 [start,end)，可省略 end 读到当前节末尾。范围过大时按 suggested_end 分段读完，不沿用其他项的长度。目录和精读必须对应当前 Canvas revision；期间状态改变后，重新读取当前目录和条目。

审核顺序是：用户意图、检索策略、每个草稿项的事实支持、播放范围。范围应位于 canonical Evidence 范围且完整覆盖支持结论的事件、对白或动作。目录 description 不能证明事实。只有目录与逐条完整精读审计都完成且实质审核通过，才返回 complete；宿主还会程序校验播放范围并冻结草稿和证据快照。

返回值严格为 action 与 advice。不通过时返回 continue，并说明失败层级、相关 video_chunk_id 和可执行修订，不输出隐藏推理。
