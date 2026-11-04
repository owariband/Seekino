from __future__ import annotations

from server.database import create_session_factory
from server.repository import video_parse_content as result_store
from server.repository.library import LibraryRepository


def _seed_outline(repository, seed_video):
    engine = repository[0]
    factory = create_session_factory(engine)
    report = seed_video(factory)
    with factory.begin() as session:
        result_store.save_result(
            session, report.video_parse_task_id, field="display",
            result={"title": "课程标题", "description": "课程简介"},
            detail={"display": {"source_fingerprint": "fp"}}, now_ms=1,
        )
        result_store.save_result(
            session, report.video_parse_task_id, field="outline",
            result={"sections": [
                {"start_ms": 0, "end_ms": 12_000, "title": "开场", "summary": "老师介绍课程目标。"},
                {"start_ms": 12_000, "end_ms": 20_000, "title": "正题", "summary": "讲解核心算法。"},
            ]},
            detail={"outline": {"source_fingerprint": "fp"}}, now_ms=1,
        )
    return factory, report


def test_video_outline_returns_sections_with_overlapping_chunk_ids(repository, seed_video):
    factory, report = _seed_outline(repository, seed_video)
    repo = LibraryRepository(factory)
    with factory() as session:
        result = repo.video_outline(
            session, video_id=report.video_id, tenant_id=report.tenant_id,
            series_ids=[report.series_id],
        )
    assert result["video_id"] == report.video_id
    assert result["title"] == "课程标题"
    assert result["description"] == "课程简介"
    assert [section["start_ms"] for section in result["sections"]] == [0, 12_000]
    # Chunk 0 spans [0, 10000), chunk 1 spans [10000, 20000); the first section
    # overlaps both, the second only chunk 1.
    assert result["sections"][0]["video_chunk_ids"] == list(report.video_chunk_ids)
    assert result["sections"][1]["video_chunk_ids"] == [report.video_chunk_ids[1]]


def test_video_outline_without_outline_returns_unavailable(repository, seed_video):
    engine = repository[0]
    factory = create_session_factory(engine)
    report = seed_video(factory)
    repo = LibraryRepository(factory)
    with factory() as session:
        result = repo.video_outline(
            session, video_id=report.video_id, tenant_id=report.tenant_id,
            series_ids=[report.series_id],
        )
    assert result == {"available": False, "reason": "该视频尚未生成章节大纲。"}


def test_video_outline_out_of_scope_video_returns_none(repository, seed_video):
    engine = repository[0]
    factory = create_session_factory(engine)
    report = seed_video(factory)
    repo = LibraryRepository(factory)
    with factory() as session:
        assert repo.video_outline(
            session, video_id=report.video_id, tenant_id="other-workspace",
            series_ids=[report.series_id],
        ) is None
        assert repo.video_outline(
            session, video_id=report.video_id, tenant_id=report.tenant_id,
            series_ids=["other-series"],
        ) is None
