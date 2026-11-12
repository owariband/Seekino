"""Inspect representative original frames, then consolidate only the source-grounded notes."""

from __future__ import annotations

import json
import logging
from bisect import bisect_right
from pathlib import Path

from PIL import Image, ImageDraw
from pydantic import ValidationError

from engine.parsev2.edit_reference import (
    AnchoredStyleCard, PickNote, ReferenceChunk, ReferenceRefinement, TechniqueObservation,
)
from gateway.multimodal import ImageInput, TextInput


_PICK_PROMPT = Path(__file__).with_name("edit_reference_pick.txt").read_text(encoding="utf-8")
_REFINE_PROMPT = Path(__file__).with_name("edit_reference_refine.txt").read_text(encoding="utf-8")


def _frame_sheets(segment, frames, boundaries):
    """Label every native frame, including held drawings, on the source clock."""
    paths = []
    for offset in range(0, len(segment.frame_ms), 20):
        stamps = segment.frame_ms[offset:offset + 20]
        sheet = Image.new("RGB", (4 * 248, ((len(stamps)+3)//4)*162), "#181818")
        draw = ImageDraw.Draw(sheet)
        for i, stamp in enumerate(stamps):
            x, y = i % 4 * 248, i // 4 * 162
            with Image.open(frames[stamp]) as frame:
                frame.thumbnail((246, 138))
                sheet.paste(frame, (x + (246-frame.width)//2, y))
            group = bisect_right(boundaries, stamp) - 1
            draw.text((x+4, y+140), f"B{group}   {stamp} ms", fill="white", font_size=17)
        out = frames[stamps[0]].parent / f"{segment.segment_id}-sheet-{offset}.jpg"
        sheet.save(out, quality=92)
        paths.append(out)
    return paths


async def observe_segments(segments, frames, generate, attempts):
    observations = []
    for segment in segments:
        boundaries = [segment.start_ms, *segment.fine_starts_ms, segment.end_ms]
        fps = segment.fps or len(segment.frame_ms) * 1000 / (segment.end_ms - segment.start_ms)
        prompt = _PICK_PROMPT.format(start_ms=segment.start_ms, end_ms=segment.end_ms, fps=f"{fps:.6g}",
                                    fine_starts_ms=segment.fine_starts_ms, flashes_ms=segment.flashes_ms)
        inputs = [TextInput(prompt), *(ImageInput(path, detail="high")
                                     for path in _frame_sheets(segment, frames, boundaries))]
        for attempt in range(attempts):
            try:
                try:
                    note = await generate(inputs, PickNote)
                except ValidationError as error:
                    note = _completed_response(PickNote, error)
                observations.append(TechniqueObservation(segment=segment, note=note))
                break
            except ValueError as error:
                if attempt + 1 == attempts:
                    raise
                inputs = [*inputs, TextInput(str(error)[:600])]
    return ReferenceChunk(observations=observations)


def _completed_response(schema, error):
    """Accept a complete response when only its closing root brace was dropped."""
    issues = error.errors(include_url=False)
    raw = issues[0].get("input") if len(issues) == 1 and issues[0]["type"] == "json_invalid" else None
    if not isinstance(raw, str):
        raise error
    text = raw.rstrip()
    try:
        json.loads(text)
    except json.JSONDecodeError as exc:
        if exc.pos == len(text) and exc.msg == "Expecting ',' delimiter":
            result = schema.model_validate_json(text + "}")
            logging.getLogger(__name__).info("reference response recovered a missing final object delimiter")
            return result
    raise error


async def consolidate(members: dict[int, ReferenceChunk], generate, attempts) -> ReferenceRefinement:
    sources = [
        {"sequence_no": seq, "segment_id": obs.segment.segment_id,
         "start_ms": obs.segment.start_ms, "end_ms": obs.segment.end_ms,
         "fine_starts_ms": obs.segment.fine_starts_ms, "flashes_ms": obs.segment.flashes_ms,
         "palette": [color.model_dump() for color in obs.segment.palette], "observation": obs.note.model_dump()}
        for seq, chunk in sorted(members.items()) for obs in chunk.observations
    ]
    if not sources:
        return ReferenceRefinement(members=[], style_card=AnchoredStyleCard(
            scope="No representative segment was selected.", signals=[])).for_sources(members)
    inputs = [TextInput(_REFINE_PROMPT), TextInput(json.dumps({"observations": sources}, ensure_ascii=False))]
    for attempt in range(attempts):
        try:
            try:
                draft = await generate(inputs, ReferenceRefinement)
            except ValidationError as error:
                draft = _completed_response(ReferenceRefinement, error)
            return draft.for_sources(members)
        except ValueError as error:
            if attempt + 1 == attempts:
                raise
            inputs = [*inputs, TextInput(str(error)[:600])]
