from __future__ import annotations

from typing import TypedDict

from engine.parsev2.schemas import VideoParseResult

from ..input import ParseInput
from ..host import LeaseToken


class PrefixContext(TypedDict):
    """一次后台 Agent 调用的上下文：当前目标、版本和两类前置背景。"""

    chunk_parse_task_id: str
    video_parse_task_id: str
    inputs: ParseInput
    lease: LeaseToken
    sequence_no: int
    allowed_start_seconds: int
    allowed_end_seconds: int
    scope: list[int]
    content: VideoParseResult
    content_version: int
    series_prefix_compact: str
    video_prefix_compact: str
