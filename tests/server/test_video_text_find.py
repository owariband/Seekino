"""Current, complete, authorized text from a single video, including browser seek."""

from copy import deepcopy
import json
import os
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from server.model.parse_content import ChunkParseContent
from server.model.task import ChunkParseTask, VideoParseTask
from server.model.video import Series, SeriesVisibility, Video
from server.service.media import MediaService
from server.service.video_parse_persistence import VideoParsePersistenceService
from tests.server.test_cloud_access import _guest, cloud_api
from tests.server.watch_browser import run_watch_browser


@pytest.fixture
def watch_text(cloud_api, seed_video):
    api = cloud_api
    tenant = api.services.auth.config.public_tenant_id
    series = api.services.library.create_series(tenant_id=tenant, name="片内查找测试")
    api.services.library.update_series(series.series_id, tenant_id=tenant, changes={"visibility": "public"})
    empty, matching, pending = [seed_video(api.sessions, tenant_id=tenant, series_id=series.series_id,
                                          episode_order=i, content_hash=f"{i:064x}") for i in (1, 7, 8)]
    source = api.store.base_directory / "episode.mp4"
    source.write_bytes(b"source for the browser fixture")
    examples = [
        {"event_type": "DIALOGUE", "start_seconds": 3456, "end_seconds": 5678,
         "speaker": None, "text_variants": [
             {"language": "ja", "role": "ORIGINAL", "text": "高松 燈です"},
             {"language": "zh-Hans", "role": "TRANSLATION", "text": "我是高松灯"},
         ]},
        {"event_type": "ON_SCREEN_TEXT", "start_seconds": 12345, "end_seconds": 13456,
         "language": "ja", "text": "演員表　高 松 燈"},
    ]
    with api.sessions.begin() as session:
        for ep in (empty, matching, pending):
            session.get(Video, ep.video_id).source_uri = api.store.uri_for_path(source)
        session.get(VideoParseTask, pending.video_parse_task_id).status = "INDEXING"
        for chunk_id, fields in zip(matching.video_chunk_ids, examples, strict=True):
            content = session.scalar(select(ChunkParseContent).join(ChunkParseTask)
                                     .where(ChunkParseTask.chunk_id == chunk_id))
            event = {**fields, "participants": [], "visible_persons": [], "description": "内部描述"}
            content.content = {**content.content, "timeline": [*content.content["timeline"], event]}
    api.services.media = MediaService(api.services.library, api.store,
                                      parse_persistence=VideoParsePersistenceService(api.sessions))
    return SimpleNamespace(api=api, tenant=tenant, series_id=series.series_id, source=source,
                           empty=empty, matching=matching, pending=pending)


def test_public_video_text_uses_only_current_visible_episode(watch_text):
    data = watch_text
    with TestClient(data.api.app) as client:
        _guest(client)

        def get(video_id, tenant=data.tenant):
            return client.get("/api/videos/text-timeline", params={"tenantId": tenant, "videoId": video_id})

        response = get(data.matching.video_id)
        assert response.headers["cache-control"] == "private, no-store"
        assert response.json() == {"ready": True, "cues": [
            {"startMs": 3456, "endMs": 5678, "source": "dialogue", "texts": ["高松 燈です", "我是高松灯"]},
            {"startMs": 12345, "endMs": 13456, "source": "screen_text", "texts": ["演員表　高 松 燈"]},
        ]}
        assert get(data.empty.video_id).json() == {"ready": True, "cues": []}
        assert get(data.pending.video_id).json() == {"ready": False, "cues": []}
        assert get(data.matching.video_id, "another-tenant").status_code == 404
        with data.api.sessions.begin() as session:
            content = session.scalar(select(ChunkParseContent).join(ChunkParseTask)
                                     .where(ChunkParseTask.chunk_id == data.matching.video_chunk_ids[1]))
            content.version += 1
        assert get(data.matching.video_id).json() == {"ready": False, "cues": []}
        with data.api.sessions.begin() as session:
            session.get(Series, data.series_id).visibility = SeriesVisibility.PRIVATE
        assert get(data.matching.video_id).status_code == 404


def test_newer_parse_hides_previous_text(watch_text):
    data = watch_text
    with data.api.sessions.begin() as session:
        old = session.get(VideoParseTask, data.matching.video_parse_task_id)
        session.add(VideoParseTask(id=str(uuid4()), video_id=old.video_id, pipeline_version=old.pipeline_version,
                                   status="PENDING", parse_config=deepcopy(old.parse_config), chunk_count=old.chunk_count,
                                   version=old.version + 1, max_attempts=3, is_test=True))
    with TestClient(data.api.app) as client:
        _guest(client)
        response = client.get("/api/videos/text-timeline", params={"tenantId": data.tenant, "videoId": data.matching.video_id})
        assert response.json() == {"ready": False, "cues": []}


@pytest.mark.skipif(os.getenv("SEEKINO_TEST_PUBLIC_COURSE_BROWSER") != "1",
                    reason="Requires built web/dist, ffmpeg/ffprobe and Playwright Chromium")
def test_video_text_find_browser(watch_text, tmp_path):
    data = watch_text
    expected = tmp_path / "expected.json"
    expected.write_text(json.dumps([
        {"videoId": data.empty.video_id, "hits": []},
        {"videoId": data.matching.video_id, "hits": [
            {"startMs": 3456, "source": "dialogue", "text": "高松 燈です"},
            {"startMs": 12345, "source": "screen_text", "text": "演員表　高 松 燈"},
        ]},
    ], ensure_ascii=False))
    run_watch_browser(data.api.app, data.source, "video-text-find.mjs", data.series_id, data.tenant, str(expected))
