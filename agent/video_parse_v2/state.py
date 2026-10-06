from __future__ import annotations

from typing import NotRequired, TypedDict

from .input import ParseInput
from .host import LeaseToken, VideoChunk, VideoTaskStatus


class RunState(TypedDict):
    """主 Graph 的运行状态；持久化任务状态由 DB 维护。"""

    video_parse_task_id: str
    inputs: NotRequired[ParseInput]
    duration_seconds: NotRequired[int]
    status: NotRequired[VideoTaskStatus]
    lease: NotRequired[LeaseToken]
    video_chunks: NotRequired[tuple[VideoChunk, ...]]
    # 模型实例、DB 连接和临时媒体路径不进入 RunState。
