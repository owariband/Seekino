from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Iterable, Mapping, Sequence

from engine.common.usage import parse_chunk_scope

from .background import BackgroundImage
from .prompts import build_video_parse_context
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
from .video_chunk_parse import VideoUnderstandingAgentProtocol


class UnderstandStage:
    def __init__(
        self,
        understanding_agent: VideoUnderstandingAgentProtocol,
        *,
        max_context_events: int = 8,
        max_context_chars: int = 2000,
    ) -> None:
        if max_context_events <= 0:
            raise ValueError("max_context_events must be greater than zero")
        if max_context_chars < 256:
            raise ValueError("max_context_chars must be at least 256")
        self._understanding_agent = understanding_agent
        self._max_context_events = max_context_events
        self._max_context_chars = max_context_chars

    def understand_chunk(
        self,
        compact: ContextSnapshot,
        video_chunk: AnalysisWindow,
        *,
        transcript: TranscriptAnalysis,
        series_bible: str | None = None,
        background_images: Sequence[BackgroundImage] = (),
    ) -> VideoChunkParseResult:
        expected_sequence_no = compact.after_chunk_sequence_no + 1
        if video_chunk.sequence_no != expected_sequence_no:
            raise ValueError(
                "context snapshot must immediately precede the video chunk; "
                f"expected {expected_sequence_no}, got {video_chunk.sequence_no}"
            )

        model_context = build_video_parse_context(
            video_chunk,
            compact,
            series_bible=series_bible,
        )
        with parse_chunk_scope(video_chunk.sequence_no):
            evidence = self._understanding_agent.understand(
                str(video_chunk.media_path),
                transcript=transcript,
                context=model_context,
                duration_seconds=video_chunk.analysis_duration_seconds,
                background_images=background_images,
            )
        observed_events, owned_events = self._merge_window_events(
            video_chunk,
            evidence.events,
            duration_seconds=video_chunk.analysis_end_seconds,
            existing_events=compact.recent_events,
        )
        context_after = self._advance_context(
            compact,
            owned_events,
            after_chunk_sequence_no=video_chunk.sequence_no,
        )
        return VideoChunkParseResult(
            window=video_chunk,
            context_before=compact,
            candidate_transcript=evidence.candidate_transcript,
            video_parse=evidence.provenance,
            is_fallback=(
                evidence.provenance is not None
                and evidence.provenance.fallback_reason is not None
            ),
            window_summary=evidence.summary,
            observed_events=observed_events,
            events=owned_events,
            self_compact=_render_events(owned_events, max_chars=4000),
            context_after=context_after,
        )

    def run(
        self,
        *,
        source_video: str,
        duration_seconds: int,
        chunk_duration_seconds: int,
        context_overlap_seconds: int,
        windows: Sequence[AnalysisWindow],
        series_bible: str | None = None,
        background_images: Sequence[BackgroundImage] = (),
        candidate_transcripts: (
            Mapping[int, TranscriptAnalysis]
            | Iterable[tuple[int, TranscriptAnalysis]]
            | None
        ) = None,
        completed_chunks: Sequence[VideoChunkParseResult] = (),
        ensure_active: Callable[[], None] | None = None,
        on_chunk_complete: Callable[[VideoChunkParseResult], None] | None = None,
    ) -> VideoParseResult:
        self._validate_windows(windows, duration_seconds)
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
                all_events.sort(key=lambda event: (event.start_seconds, event.end_seconds))
                context_before = completed.context_after
                continue

            candidate_transcript = TranscriptAnalysis(transcript=[])
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
            if ensure_active is not None:
                ensure_active()
            chunk_result = self.understand_chunk(
                context_before,
                window,
                transcript=candidate_transcript,
                series_bible=series_bible,
                background_images=background_images,
            )
            all_events.extend(chunk_result.events)
            all_events.sort(key=lambda event: (event.start_seconds, event.end_seconds))
            chunk_results.append(chunk_result)
            if on_chunk_complete is not None:
                on_chunk_complete(chunk_result)
            context_before = chunk_result.context_after

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
            duration_seconds=duration_seconds,
            chunk_duration_seconds=chunk_duration_seconds,
            context_overlap_seconds=context_overlap_seconds,
            chunks=chunk_results,
            events=all_events,
        )

    @staticmethod
    def _validate_windows(
        windows: Sequence[AnalysisWindow],
        duration_seconds: int,
    ) -> None:
        if duration_seconds <= 0:
            raise ValueError("duration_seconds must be greater than zero")
        if not windows:
            raise ValueError("at least one analysis window is required")

        expected_start_seconds = 0
        for expected_sequence_no, window in enumerate(windows):
            if window.sequence_no != expected_sequence_no:
                raise ValueError(
                    "analysis windows must have contiguous sequence numbers"
                )
            if window.chunk_start_seconds != expected_start_seconds:
                raise ValueError(
                    "canonical chunks must be contiguous and non-overlapping"
                )
            expected_start_seconds = window.chunk_end_seconds
        if expected_start_seconds != duration_seconds:
            raise ValueError("canonical chunks must cover the complete video")

    @staticmethod
    def _merge_window_events(
        window: AnalysisWindow,
        events: Sequence[TimelineEvent],
        *,
        duration_seconds: int,
        existing_events: Sequence[ContextEvent | OwnedTimelineEvent],
    ) -> tuple[list[ObservedTimelineEvent], list[OwnedTimelineEvent]]:
        observed_events: list[ObservedTimelineEvent] = []
        owned_events: list[OwnedTimelineEvent] = []

        for event in events:
            video_start_seconds = min(
                duration_seconds,
                window.analysis_start_seconds + event.start_seconds,
            )
            video_end_seconds = min(
                duration_seconds,
                window.analysis_start_seconds + event.end_seconds,
            )
            if video_end_seconds <= video_start_seconds:
                continue

            midpoint_twice = video_start_seconds + video_end_seconds
            owned = (
                2 * window.chunk_start_seconds
                <= midpoint_twice
                < 2 * window.chunk_end_seconds
            )
            candidate_payload = event.model_dump()
            candidate_payload.update(
                {
                    "start_seconds": video_start_seconds,
                    "end_seconds": video_end_seconds,
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
                    video_start_seconds=video_start_seconds,
                    video_end_seconds=video_end_seconds,
                    owned_by_current_chunk=owned,
                    kept_after_deduplication=kept,
                )
            )

        owned_events.sort(key=lambda event: (event.start_seconds, event.end_seconds))
        observed_events.sort(
            key=lambda event: (event.video_start_seconds, event.video_end_seconds)
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
        start_seconds=event.start_seconds,
        end_seconds=event.end_seconds,
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
            start_seconds=event.start_seconds,
            end_seconds=event.end_seconds,
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
            start_seconds=event.start_seconds,
            end_seconds=event.end_seconds,
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
    start_seconds: int,
    end_seconds: int,
    event_type: str,
    description: str,
    text: str | None,
) -> str:
    content = description
    if text:
        content = f"{description}；台词：{text}"
    return (
        f"[{_format_seconds(start_seconds)}-{_format_seconds(end_seconds)}] "
        f"{event_type} {content}"
    )


def _format_seconds(value: int) -> str:
    whole_seconds, milliseconds = divmod(value, 1000)
    minutes, seconds = divmod(whole_seconds, 60)
    return f"{minutes:02d}:{seconds:02d}.{milliseconds:03d}"


def _is_duplicate(
    candidate: OwnedTimelineEvent,
    existing_events: Sequence[ContextEvent | OwnedTimelineEvent],
) -> bool:
    candidate_key = _event_key(candidate)
    candidate_midpoint = candidate.start_seconds + candidate.end_seconds
    for existing in existing_events:
        if _event_key(existing) != candidate_key:
            continue
        existing_midpoint = existing.start_seconds + existing.end_seconds
        if abs(candidate_midpoint - existing_midpoint) <= 2000:
            return True
    return False


def _event_key(
    event: ContextEvent | OwnedTimelineEvent,
) -> tuple[str, str]:
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
