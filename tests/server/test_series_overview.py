from __future__ import annotations

from server.database import create_session_factory
from server.model.video import Video
from server.repository import video_parse_content as result_store
from server.repository.library import LibraryRepository


def _seed(factory, seed_video, **kwargs):
    # The shared fixture gives every video the same hash; a series holds one hash once.
    report = seed_video(factory, **kwargs)
    with factory.begin() as session:
        video = session.get(Video, report.video_id)
        video.hash = video.id.replace("-", "").ljust(64, "0")
    return report


def _seed_display(factory, seed_video, *, series_name="Test Series", episode_order=1,
                  title="课程标题", description="课程简介", outline=True):
    report = _seed(factory, seed_video, series_name=series_name, episode_order=episode_order)
    with factory.begin() as session:
        result_store.save_result(
            session, report.video_parse_task_id, field="display",
            result={"title": title, "description": description},
            detail={"display": {"source_fingerprint": "fp"}}, now_ms=1,
        )
        if outline:
            result_store.save_result(
                session, report.video_parse_task_id, field="outline",
                result={"sections": [
                    {"start_ms": 0, "end_ms": 12_000, "title": "开场", "summary": "老师介绍课程目标。"},
                ]},
                detail={"outline": {"source_fingerprint": "fp"}}, now_ms=1,
            )
    return report


def _overview(factory, *, series_ids, tenant_id, series_name=None):
    repo = LibraryRepository(factory)
    with factory() as session:
        return repo.series_overview(
            session, series_name=series_name, tenant_id=tenant_id, series_ids=series_ids,
        )


def test_series_overview_returns_two_scoped_series_without_video_id(repository, seed_video):
    factory = create_session_factory(repository[0])
    first = _seed_display(factory, seed_video, series_name="Series A", episode_order=1)
    second = _seed_display(factory, seed_video, series_name="Series B", episode_order=1)
    result = _overview(
        factory, series_ids=[first.series_id, second.series_id], tenant_id=first.tenant_id,
    )
    assert [item["series"] for item in result["series"]] == ["Series A", "Series B"]
    for item in result["series"]:
        assert "video_id" not in item
        assert item["displayName"] == item["series"]
        for video in item["videos"]:
            assert "video_id" not in video
            assert video["summarized"] is True
            assert video["hasOutline"] is True


def test_series_overview_orders_videos_by_episode(repository, seed_video):
    factory = create_session_factory(repository[0])
    first = _seed_display(factory, seed_video, series_name="Course", episode_order=7)
    _seed_display(factory, seed_video, series_name="Course", episode_order=1)
    result = _overview(factory, series_ids=[first.series_id], tenant_id=first.tenant_id)
    assert [video["episode"] for video in result["series"][0]["videos"]] == [1, 7]


def test_series_overview_marks_video_without_display_as_not_summarized(repository, seed_video):
    factory = create_session_factory(repository[0])
    first = _seed_display(factory, seed_video, series_name="Course", episode_order=1)
    _seed(factory, seed_video, series_name="Course", episode_order=2)
    result = _overview(factory, series_ids=[first.series_id], tenant_id=first.tenant_id)
    videos = {video["episode"]: video for video in result["series"][0]["videos"]}
    assert videos[1]["summarized"] is True
    assert videos[1]["title"] == "课程标题"
    assert videos[1]["description"] == "课程简介"
    assert videos[2]["summarized"] is False
    assert videos[2]["title"] == "episode.mp4"
    assert videos[2]["description"] is None
    assert videos[2]["hasOutline"] is False


def test_series_overview_omits_out_of_scope_series(repository, seed_video):
    factory = create_session_factory(repository[0])
    first = _seed_display(factory, seed_video, series_name="Series A", episode_order=1)
    second = _seed_display(factory, seed_video, series_name="Series B", episode_order=1)
    seed_video(factory, series_name="Series C", episode_order=1)
    result = _overview(
        factory, series_ids=[first.series_id, second.series_id], tenant_id=first.tenant_id,
    )
    assert [item["series"] for item in result["series"]] == ["Series A", "Series B"]


def test_series_overview_reports_unknown_series(repository, seed_video):
    factory = create_session_factory(repository[0])
    first = _seed_display(factory, seed_video, series_name="Course", episode_order=1)
    result = _overview(
        factory, series_ids=[first.series_id], tenant_id=first.tenant_id, series_name="Missing",
    )
    assert result["error"]["code"] == "UNKNOWN_SERIES"
    assert "Course" in result["error"]["hint"]
