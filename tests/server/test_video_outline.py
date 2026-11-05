from __future__ import annotations

from server.database import create_session_factory
from server.repository import video_parse_content as result_store
from server.repository.library import LibraryRepository


def _seed_outline(repository, seed_video, *, episode_order=7):
    engine = repository[0]
    factory = create_session_factory(engine)
    report = seed_video(factory, episode_order=episode_order)
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


def test_video_outline_resolves_series_and_episode_without_video_id(repository, seed_video):
    factory, report = _seed_outline(repository, seed_video)
    repo = LibraryRepository(factory)
    with factory() as session:
        result = repo.video_outline(
            session, videos=[{"series": report.series_name, "episode": report.episode_order}],
            tenant_id=report.tenant_id, series_ids=[report.series_id],
        )
    item = result["videos"][0]
    assert item["series"] == report.series_name
    assert item["episode"] == report.episode_order
    assert item["available"] is True
    assert item["title"] == "课程标题"
    assert item["description"] == "课程简介"
    assert "video_id" not in item
    assert [section["start_ms"] for section in item["sections"]] == [0, 12_000]
    # Chunk 0 spans [0, 10000), chunk 1 spans [10000, 20000); the first section
    # overlaps both, the second only chunk 1.
    assert item["sections"][0]["video_chunk_ids"] == list(report.video_chunk_ids)
    assert item["sections"][1]["video_chunk_ids"] == [report.video_chunk_ids[1]]


def test_video_outline_without_outline_returns_unavailable(repository, seed_video):
    engine = repository[0]
    factory = create_session_factory(engine)
    report = seed_video(factory)
    repo = LibraryRepository(factory)
    with factory() as session:
        result = repo.video_outline(
            session, videos=[{"series": report.series_name, "episode": report.episode_order}],
            tenant_id=report.tenant_id, series_ids=[report.series_id],
        )
    assert result["videos"] == [{
        "series": report.series_name, "episode": report.episode_order,
        "available": False, "reason": "该视频尚未生成章节大纲。",
    }]


def test_video_outline_omitted_series_uses_the_only_scoped_series(repository, seed_video):
    factory, report = _seed_outline(repository, seed_video)
    repo = LibraryRepository(factory)
    with factory() as session:
        result = repo.video_outline(
            session, videos=[{"series": None, "episode": report.episode_order}],
            tenant_id=report.tenant_id, series_ids=[report.series_id],
        )
    assert result["videos"][0]["series"] == report.series_name


def test_video_outline_omitted_series_with_multiple_scope_is_ambiguous(repository, seed_video):
    engine = repository[0]
    factory = create_session_factory(engine)
    first = seed_video(factory, series_name="Series A")
    second = seed_video(factory, series_name="Series B")
    repo = LibraryRepository(factory)
    with factory() as session:
        result = repo.video_outline(
            session, videos=[{"series": None, "episode": 1}],
            tenant_id=first.tenant_id, series_ids=[first.series_id, second.series_id],
        )
    assert result["error"]["code"] == "AMBIGUOUS_SERIES"
    assert "Series A" in result["error"]["hint"] and "Series B" in result["error"]["hint"]


def test_video_outline_reports_unknown_series_and_episode(repository, seed_video):
    engine = repository[0]
    factory = create_session_factory(engine)
    report = seed_video(factory)
    repo = LibraryRepository(factory)
    with factory() as session:
        unknown_series = repo.video_outline(
            session, videos=[{"series": "Missing", "episode": report.episode_order}],
            tenant_id=report.tenant_id, series_ids=[report.series_id],
        )
        assert unknown_series["error"]["code"] == "UNKNOWN_SERIES"
        assert report.series_name in unknown_series["error"]["hint"]

        unknown_episode = repo.video_outline(
            session, videos=[{"series": report.series_name, "episode": 999}],
            tenant_id=report.tenant_id, series_ids=[report.series_id],
        )
        assert unknown_episode["error"]["code"] == "UNKNOWN_EPISODE"

        out_of_tenant = repo.video_outline(
            session, videos=[{"series": report.series_name, "episode": report.episode_order}],
            tenant_id="other-workspace", series_ids=[report.series_id],
        )
        assert out_of_tenant["error"]["code"] == "UNKNOWN_SERIES"


def test_video_outline_batches_multiple_addresses(repository, seed_video):
    engine = repository[0]
    factory = create_session_factory(engine)
    first = seed_video(factory, series_name="Series A", episode_order=1)
    second = seed_video(factory, series_name="Series B", episode_order=1)
    repo = LibraryRepository(factory)
    with factory() as session:
        result = repo.video_outline(
            session,
            videos=[
                {"series": "Series A", "episode": 1},
                {"series": "Series B", "episode": 1},
            ],
            tenant_id=first.tenant_id, series_ids=[first.series_id, second.series_id],
        )
    assert [item["series"] for item in result["videos"]] == ["Series A", "Series B"]
    assert all(item["available"] is False for item in result["videos"])
