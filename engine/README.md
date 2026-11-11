# seekino Engine

Engine 是 seekino 的领域算法边界。视频解析算法位于 `parsev2/`，Agent 运行程序位于
`agent/video_parse_v2/`，检索算法位于 `rag/`。版本无关的媒体与模型能力从 `common/` 导入。
Engine 不拥有生产任务、租约、Artifact 或数据库；这些生命周期由 Server 负责，外部模型请求由 Gateway 实现。

## 媒体时间单位

视频时长、Chunk／事件边界、ASR 和采样帧位置统一使用整数毫秒，保留已有
`*_seconds`／`*Seconds` 字段名。例如 `8.008` 秒视频记录为 `8008`。配置中的
Chunk／Interval／重叠／分组时长仍为秒，FPS 仍为帧／秒；超时与租约单位不变。
FFprobe、ASR、FFmpeg 和播放器在接入边界完成秒与毫秒转换；页面展示小数秒。

本地 WhisperX 的字词对齐由 `common/transcription.py` 统一规范化为可选
`transcript[].words`：`text/start_ms/end_ms/score`，未知时间为一对 `null`。
这些时间相对当前音频窗口；Server 加偏移后作为原视频绝对毫秒保存至 ASR evidence。
句段旧字段名和非 WhisperX 响应保持兼容；字幕排版不在此时间归一层实现。
历史整数秒数据须由 Server 迁移并标识单位，不能直接按新语义读取。

## 检索

检索融合文本、对话和画面候选，每个 Event 都关联原视频和时间范围。Server 按访问范围、解析任务及内容版本回源核对后返回片段。[模型配置](../config.yaml)决定向量来源和维度。更换编码模型后，需要用一致的配置重建受影响的索引。
