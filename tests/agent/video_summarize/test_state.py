from __future__ import annotations

from agent.video_summarize.state import SummaryChunk, build_chunk_digest


def _chunk(task_id: str, sequence_no: int, events: list[dict]) -> SummaryChunk:
    return SummaryChunk(task_id, sequence_no, 1, {"timeline": events})


def test_digest_includes_screen_text_verbatim_and_orders_chunks():
    chunks = (
        _chunk("chunk-1", 1, [
            {"event_type": "SCENE", "start_seconds": 1000, "end_seconds": 2000,
             "description": "Second scene"},
        ]),
        _chunk("chunk-0", 0, [
            {"event_type": "ON_SCREEN_TEXT", "start_seconds": 0, "end_seconds": 500,
             "description": "Title card", "text": "第十二話 出発"},
            {"event_type": "ACTION", "start_seconds": 500, "end_seconds": 1000,
             "description": "Someone enters"},
        ]),
    )
    digest = build_chunk_digest(chunks, 100_000)
    lines = digest.splitlines()
    assert lines[0] == "## chunk 0 ref=chunk-0"
    assert "0-500 ON_SCREEN_TEXT: Title card | text: 第十二話 出発" in lines
    assert "500-1000 ACTION: Someone enters" in lines
    assert "## chunk 1 ref=chunk-1" in lines
    assert "1000-2000 SCENE: Second scene" in lines
    # Chunks are ordered by sequence_no, not by the input tuple order.
    assert digest.index("## chunk 0") < digest.index("## chunk 1")


def test_digest_truncation_keeps_headers_and_screen_text_and_states_omission():
    chunks = (_chunk("chunk-0", 0, [
        {"event_type": "ON_SCREEN_TEXT", "description": "Card", "text": "第十二話 出発"},
        *[{"event_type": "ACTION", "description": f"event {index} with a longer descriptive sentence."}
          for index in range(20)],
    ]),)
    assert len(build_chunk_digest(chunks, 100_000)) > 400
    digest = build_chunk_digest(chunks, 200)
    assert len(digest) <= 200
    assert "## chunk 0 ref=chunk-0" in digest
    assert "| text: 第十二話 出発" in digest
    assert "digest omitted" in digest and "event lines" in digest
    kept = digest.count("ACTION: ")
    assert 1 <= kept < 20


def test_digest_truncation_drops_screen_text_from_the_end_when_required_alone_exceeds():
    chunks = (_chunk("chunk-0", 0, [
        {"event_type": "ON_SCREEN_TEXT", "description": "Card", "text": f"屏幕文字 {index}"}
        for index in range(10)
    ]),)
    digest = build_chunk_digest(chunks, 120)
    assert len(digest) <= 120
    assert "## chunk 0 ref=chunk-0" in digest
    assert "digest omitted" in digest
    assert "屏幕文字 0" in digest
    assert "屏幕文字 9" not in digest
