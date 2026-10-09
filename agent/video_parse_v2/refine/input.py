from __future__ import annotations

from typing import Any, NotRequired, TypedDict

from engine.parsev2.schemas import VideoParseResult

from ..input import ParseInput
from ..state import LeaseToken


class RefineInput(TypedDict):
    """一次区间纠偏的输入快照；成员映射以 chunk_parse_task_id 为 key。"""

    interval_parse_task_id: str
    video_parse_task_id: str
    inputs: ParseInput
    lease: LeaseToken
    start_sequence_no: int
    chunk_count: int
    member_sequence_nos: dict[str, int]
    member_bounds: dict[str, tuple[int, int]]
    member_contents: dict[str, VideoParseResult]
    member_versions: dict[str, int]
    member_scopes: dict[str, list[int]]

    member_evidence: NotRequired[dict[str, Any]]
    background: NotRequired[dict[str, Any]]
    member_fingerprints: NotRequired[dict[str, str]]
    write_bounds: NotRequired[tuple[int, int]]
