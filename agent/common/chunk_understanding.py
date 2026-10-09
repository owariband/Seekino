"""Recognize one authorized video range through bound host capabilities."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable

from engine.parsev2.observations import (
    ASRResult, OCRResult, build_chunk_without_understanding,
)
from engine.parsev2.schemas import VideoParseResult
from engine.parsev2.validation import validate_result_time_range


def build_understanding_prompt(
    *,
    start_ms: int,
    end_ms: int,
    asr_result: ASRResult,
    ocr_result: OCRResult | None,
) -> str:
    """Keep normalized observations and time authority independent of tasks."""

    return "\n".join((
        "Analyze the prepared media for one fixed video chunk.",
        (
            "Only report events inside the authorized half-open source-video range "
            f"[{start_ms}, {end_ms})."
        ),
        "Use absolute integer source-video milliseconds and preserve uncertain attribution as null.",
        "ASR/OCR timestamps are already normalized to absolute integer source-video milliseconds.",
        "The legacy start_seconds/end_seconds fields contain milliseconds.",
        "Use these global timestamps directly; do not multiply or divide by 1000 or add a Chunk/window offset again.",
        "Return only the injected VideoParseResult schema; do not invent task IDs or versions.",
        "ASR input: " + json.dumps(
            asr_result, ensure_ascii=False, allow_nan=False, sort_keys=True,
        ),
        "OCR input: " + json.dumps(
            ocr_result.model_dump(mode="json") if ocr_result is not None else None,
            ensure_ascii=False, allow_nan=False, sort_keys=True,
        ),
    ))


async def understand_chunk(
    *,
    start_ms: int,
    end_ms: int,
    ocr_enabled: bool,
    understanding_enabled: bool,
    prepare_visual: Callable[[], Awaitable[None]],
    transcribe: Callable[[], Awaitable[ASRResult]],
    read_text: Callable[[], Awaitable[OCRResult]],
    understand: Callable[..., Awaitable[VideoParseResult]],
    prompt_builder: Callable[..., str] | None = None,
    observation_callback: Callable[[ASRResult, OCRResult | None], None] | None = None,
) -> VideoParseResult:
    """Return validated facts; callers retain commit and media lifecycle ownership."""

    async def visual_input() -> OCRResult | None:
        await prepare_visual()
        return await read_text() if ocr_enabled else None

    visual_task = asyncio.create_task(visual_input())
    asr_task = asyncio.ensure_future(transcribe())
    try:
        ocr_result, asr_result = await asyncio.gather(visual_task, asr_task)
    except BaseException:
        # Finish both branches before the harness releases their shared media.
        visual_task.cancel()
        asr_task.cancel()
        await asyncio.gather(visual_task, asr_task, return_exceptions=True)
        raise

    if observation_callback is not None:
        observation_callback(asr_result, ocr_result)

    if understanding_enabled:
        result = await understand(
            asr_result=asr_result,
            ocr_result=ocr_result,
            prompt=(prompt_builder or build_understanding_prompt)(
                start_ms=start_ms,
                end_ms=end_ms,
                asr_result=asr_result,
                ocr_result=ocr_result,
            ),
        )
    else:
        result = build_chunk_without_understanding(
            asr_result=asr_result,
            ocr_result=ocr_result,
        )
    return validate_result_time_range(
        result, start_seconds=start_ms, end_seconds=end_ms,
    )
