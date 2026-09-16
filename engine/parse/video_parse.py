from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Iterable, Mapping, Sequence

from .background import BackgroundImage
from .schemas import (
    AnalysisWindow,
    ContextEvent,
    ContextSnapshot,
    ObservedTimelineEvent,
    OwnedTimelineEvent,
    VideoChunkParseResult,
    TimelineEvent,
    TranscriptAnalysis,
    VideoParseResult,
)
from .usage import parse_chunk_scope
from .video_chunk_parse import VideoChunkEvidenceParser


class ParsePipeline:
    def __init__(
        self,
        parser: VideoChunkEvidenceParser,
        *,
        max_context_events: int = 8,
        max_context_chars: int = 2000,
    ) -> None:
        if max_context_events <= 0:
            raise ValueError("max_context_events must be greater than zero")
        if max_context_chars < 256:
            raise ValueError("max_context_chars must be at least 256")
        self._parser = parser
        self._max_context_events = max_context_events
        self._max_context_chars = max_context_chars

    def run(
        self,
        *,
        source_video: str,
        duration_ms: int,
        chunk_duration_ms: int,
        context_overlap_ms: int,
        windows: Sequence[AnalysisWindow],
        series_bible: str | None = None,
        background_images: Sequence[BackgroundImage] = (),
        candidate_transcripts: (
            Mapping[int, TranscriptAnalysis]
            | Iterable[tuple[int, TranscriptAnalysis]]
            | None
        ) = None,
        completed_chunks: Sequence[VideoChunkParseResult] = (),
        on_chunk_complete: Callable[[VideoChunkParseResult], None] | None = None,
    ) -> VideoParseResult:
        self._validate_windows(windows, duration_ms)
        if len(completed_chunks) > len(windows):
            raise ValueError("completed chunks cannot outnumber analysis windows")
        context_before = ContextSnapshot(
            snapshot_id="context-initial",
            after_chunk_sequence_no=-1,
            compact_text="",
            recent_events=[],
        )
        chunk_results: list[VideoChunkParseResult] = []
        all_events: list[OwnedTimelineEvent] = []
        transcript_mapping = (
            candidate_transcripts
            if isinstance(candidate_transcripts, Mapping)
            else None
        )
        transcript_iterator = (
            iter(candidate_transcripts)
            if candidate_transcripts is not None
            and transcript_mapping is None
            else None
        )

        for window in windows:
            if window.sequence_no < len(completed_chunks):
                completed = completed_chunks[window.sequence_no]
                if completed.window != window:
                    raise ValueError(
                        f"completed chunk {window.sequence_no} "
                        "uses a different window"
                    )
                if completed.context_before != context_before:
                    raise ValueError(
                        f"completed chunk {window.sequence_no} breaks context chain"
                    )
                chunk_results.append(completed)
                all_events.extend(completed.events)
                all_events.sort(key=lambda event: (event.start_ms, event.end_ms))
                context_before = completed.context_after
                continue

            candidate_transcript = None
            if transcript_mapping is not None:
                try:
                    candidate_transcript = transcript_mapping[window.sequence_no]
                except KeyError as exc:
                    raise ValueError(
                        f"candidate transcript missing for chunk {window.sequence_no}"
                    ) from exc
            elif transcript_iterator is not None:
                try:
                    transcript_sequence_no, candidate_transcript = next(
                        transcript_iterator
                    )
                except StopIteration as exc:
                    raise ValueError(
                        f"candidate transcript missing for chunk {window.sequence_no}"
                    ) from exc
                if transcript_sequence_no != window.sequence_no:
                    raise ValueError(
                        "candidate transcripts must match unfinished chunks in "
                        f"sequence; expected {window.sequence_no}, got "
                        f"{transcript_sequence_no}"
                    )
            model_context = self._build_model_context(
                window,
                context_before,
                series_bible=series_bible,
            )
            with parse_chunk_scope(window.sequence_no):
                evidence = self._parser.analyze(
                    str(window.media_path),
                    context=model_context,
                    duration_ms=window.analysis_duration_ms,
                    candidate_transcript=candidate_transcript,
                    background_images=background_images,
                )
            observed_events, owned_events = self._merge_window_events(
                window,
                evidence.events,
                duration_ms=duration_ms,
                existing_events=all_events,
            )
            all_events.extend(owned_events)
            all_events.sort(key=lambda event: (event.start_ms, event.end_ms))
            self_compact = _render_events(owned_events, max_chars=4000)
            context_after = self._advance_context(
                context_before,
                owned_events,
                after_chunk_sequence_no=window.sequence_no,
            )
            chunk_result = VideoChunkParseResult(
                window=window,
                context_before=context_before,
                candidate_transcript=evidence.candidate_transcript,
                video_parse=evidence.provenance,
                is_fallback=(
                    evidence.provenance is not None
                    and evidence.provenance.fallback_reason is not None
                ),
                window_summary=evidence.summary,
                observed_events=observed_events,
                events=owned_events,
                self_compact=self_compact,
                context_after=context_after,
            )
            chunk_results.append(chunk_result)
            if on_chunk_complete is not None:
                on_chunk_complete(chunk_result)
            context_before = context_after

        if transcript_iterator is not None:
            try:
                extra_sequence_no, _ = next(transcript_iterator)
            except StopIteration:
                pass
            else:
                raise ValueError(
                    "candidate transcript has no matching unfinished chunk: "
                    f"{extra_sequence_no}"
                )

        return VideoParseResult(
            source_video=source_video,
            duration_ms=duration_ms,
            chunk_duration_ms=chunk_duration_ms,
            context_overlap_ms=context_overlap_ms,
            chunks=chunk_results,
            events=all_events,
        )

    @staticmethod
    def _validate_windows(
        windows: Sequence[AnalysisWindow],
        duration_ms: int,
    ) -> None:
        if duration_ms <= 0:
            raise ValueError("duration_ms must be greater than zero")
        if not windows:
            raise ValueError("at least one analysis window is required")

        expected_start_ms = 0
        for expected_sequence_no, window in enumerate(windows):
            if window.sequence_no != expected_sequence_no:
                raise ValueError(
                    "analysis windows must have contiguous sequence numbers"
                )
            if window.chunk_start_ms != expected_start_ms:
                raise ValueError(
                    "canonical chunks must be contiguous and non-overlapping"
                )
            expected_start_ms = window.chunk_end_ms
        if expected_start_ms != duration_ms:
            raise ValueError("canonical chunks must cover the complete video")

    @staticmethod
    def _build_model_context(
        window: AnalysisWindow,
        context_before: ContextSnapshot,
        *,
        series_bible: str | None,
    ) -> str:
        sections = [
            "ParsePipeline 窗口约束：",
            (
                "- 当前 AnalysisWindow 对应整集绝对时间 "
                f"[{window.analysis_start_ms}, {window.analysis_end_ms}) ms；"
                "模型输出时间仍须相对当前 AnalysisWindow 起点。"
            ),
            (
                "- 当前规范 VideoChunk 对应窗口内相对时间 "
                f"[{window.relative_chunk_start_ms}, "
                f"{window.relative_chunk_end_ms}) ms。"
            ),
            (
                "- AnalysisWindow 两侧内容仅用于看清跨边界事件；"
                "请正常返回窗口内有证据的事件，Engine 会按事件中点"
                "做唯一归属。"
            ),
        ]
        if series_bible and series_bible.strip():
            sections.extend(
                [
                    "",
                    "SeriesBible（只用于身份与术语消歧）：",
                    series_bible.strip(),
                ]
            )
        sections.extend(
            [
                "",
                (
                    "contextBeforeCompact（此前规范 VideoChunk 已确认的"
                    "有界历史；不得据此虚构当前事件）："
                ),
                context_before.compact_text or "（首段，无历史事件）",
            ]
        )
        return "\n".join(sections)

    @staticmethod
    def _merge_window_events(
        window: AnalysisWindow,
        events: Sequence[TimelineEvent],
        *,
        duration_ms: int,
        existing_events: Sequence[OwnedTimelineEvent],
    ) -> tuple[list[ObservedTimelineEvent], list[OwnedTimelineEvent]]:
        observed_events: list[ObservedTimelineEvent] = []
        owned_events: list[OwnedTimelineEvent] = []

        for event in events:
            absolute_start_ms = min(
                duration_ms,
                window.analysis_start_ms + event.start_ms,
            )
            absolute_end_ms = min(
                duration_ms,
                window.analysis_start_ms + event.end_ms,
            )
            if absolute_end_ms <= absolute_start_ms:
                continue

            midpoint_twice = absolute_start_ms + absolute_end_ms
            owned = (
                2 * window.chunk_start_ms
                <= midpoint_twice
                < 2 * window.chunk_end_ms
            )
            candidate_payload = event.model_dump()
            candidate_payload.update(
                {
                    "start_ms": absolute_start_ms,
                    "end_ms": absolute_end_ms,
                    "source_chunk_sequence_no": window.sequence_no,
                }
            )
            candidate = OwnedTimelineEvent.model_validate(candidate_payload)
            kept = owned and not _is_duplicate(
                candidate,
                [*existing_events, *owned_events],
            )
            if kept:
                owned_events.append(candidate)
            observed_events.append(
                ObservedTimelineEvent(
                    relative_event=event,
                    absolute_start_ms=absolute_start_ms,
                    absolute_end_ms=absolute_end_ms,
                    owned_by_current_chunk=owned,
                    kept_after_deduplication=kept,
                )
            )

        owned_events.sort(key=lambda event: (event.start_ms, event.end_ms))
        observed_events.sort(
            key=lambda event: (event.absolute_start_ms, event.absolute_end_ms)
        )
        return observed_events, owned_events

    def _advance_context(
        self,
        context_before: ContextSnapshot,
        owned_events: Sequence[OwnedTimelineEvent],
        *,
        after_chunk_sequence_no: int,
    ) -> ContextSnapshot:
        recent_events = [
            *context_before.recent_events,
            *[_to_context_event(event) for event in owned_events],
        ][-self._max_context_events :]
        recent_events, compact_text = _fit_context(
            recent_events,
            max_chars=self._max_context_chars,
        )
        snapshot_payload = {
            "after_chunk_sequence_no": after_chunk_sequence_no,
            "recent_events": [
                event.model_dump(mode="json") for event in recent_events
            ],
        }
        digest = hashlib.sha256(
            json.dumps(
                snapshot_payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()[:16]
        return ContextSnapshot(
            snapshot_id=f"context-{after_chunk_sequence_no:04d}-{digest}",
            after_chunk_sequence_no=after_chunk_sequence_no,
            compact_text=compact_text,
            recent_events=recent_events,
        )


def _to_context_event(event: OwnedTimelineEvent) -> ContextEvent:
    return ContextEvent(
        chunk_sequence_no=event.source_chunk_sequence_no,
        start_ms=event.start_ms,
        end_ms=event.end_ms,
        event_type=event.event_type,
        description=_truncate(event.description, 400),
        text=_truncate(event.text, 240) if event.text else None,
    )


def _fit_context(
    events: list[ContextEvent],
    *,
    max_chars: int,
) -> tuple[list[ContextEvent], str]:
    selected = list(events)
    rendered = _render_context_events(selected)
    while len(rendered) > max_chars and len(selected) > 1:
        selected.pop(0)
        rendered = _render_context_events(selected)
    if len(rendered) > max_chars:
        rendered = rendered[: max_chars - 1].rstrip() + "…"
    return selected, rendered


def _render_context_events(events: Sequence[ContextEvent]) -> str:
    return "\n".join(
        _render_event_line(
            start_ms=event.start_ms,
            end_ms=event.end_ms,
            event_type=event.event_type,
            description=event.description,
            text=event.text,
        )
        for event in events
    )


def _render_events(
    events: Sequence[OwnedTimelineEvent],
    *,
    max_chars: int,
) -> str:
    if not events:
        return "当前规范 VideoChunk 未归属可确认事件。"
    rendered = "\n".join(
        _render_event_line(
            start_ms=event.start_ms,
            end_ms=event.end_ms,
            event_type=event.event_type,
            description=event.description,
            text=event.text,
        )
        for event in events
    )
    if len(rendered) > max_chars:
        return rendered[: max_chars - 1].rstrip() + "…"
    return rendered


def _render_event_line(
    *,
    start_ms: int,
    end_ms: int,
    event_type: str,
    description: str,
    text: str | None,
) -> str:
    content = description
    if text:
        content = f"{description}；台词：{text}"
    return (
        f"[{_format_ms(start_ms)}-{_format_ms(end_ms)}] "
        f"{event_type} {content}"
    )


def _format_ms(value: int) -> str:
    minutes, remainder = divmod(value, 60_000)
    seconds, milliseconds = divmod(remainder, 1000)
    return f"{minutes:02d}:{seconds:02d}.{milliseconds:03d}"


def _is_duplicate(
    candidate: OwnedTimelineEvent,
    existing_events: Sequence[OwnedTimelineEvent],
) -> bool:
    candidate_key = _event_key(candidate)
    candidate_midpoint = candidate.start_ms + candidate.end_ms
    for existing in existing_events:
        if _event_key(existing) != candidate_key:
            continue
        existing_midpoint = existing.start_ms + existing.end_ms
        if abs(candidate_midpoint - existing_midpoint) <= 2000:
            return True
    return False


def _event_key(event: OwnedTimelineEvent) -> tuple[str, str]:
    content = (
        event.text
        if event.event_type == "DIALOGUE" and event.text
        else event.description
    )
    normalized = "".join(
        character.casefold() for character in content if character.isalnum()
    )
    return event.event_type, normalized


def _truncate(value: str, max_length: int) -> str:
    if len(value) <= max_length:
        return value
    return value[: max_length - 1].rstrip() + "…"
