from __future__ import annotations

from typing import NotRequired, TypedDict

from engine.parsev2.schemas import VideoParseResult

from ..input import ParseInput
from ..host import ChunkExecution


class ChunkState(TypedDict):
    """单 Chunk 子图的瞬时数据；持久状态、连接和媒体路径不进入这里。"""

    chunk_parse_task_id: str
    inputs: NotRequired[ParseInput]
    execution: NotRequired[ChunkExecution]
    visual_ready: NotRequired[bool]
    asr_result: NotRequired[object]
    ocr_result: NotRequired[object]
    result: NotRequired[VideoParseResult]
    # 临时音视频、帧路径及调用器由 Task/Lease 的运行时上下文管理。
