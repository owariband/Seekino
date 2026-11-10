from copy import deepcopy

from sqlalchemy import select

from engine.parsev2.edit_reference import TechniqueObservation
from server.database import create_session_factory
from server.model.parse_content import ChunkParseContent
from server.model.task import ChunkParseTask, VideoParseTask
from server.service.video_parse_persistence import INDEX_EMBEDDINGS_DETAIL_KEY
from server.storage.elasticsearch_references import observation_fingerprint
from tests.engine.parsev2.test_edit_reference import make_chunk
from tests.server.test_mcp_transport import _call, _client, _service


def test_reference_hits_require_current_source_scope_version_and_receipt(repository, seed_video):
    engine, persistence = repository
    factory = create_session_factory(engine)
    src = seed_video(factory)
    index = "edit_references_test"
    doc = make_chunk()
    obs = TechniqueObservation.model_validate(doc["observations"][0])
    with factory.begin() as s:
        task = s.get(VideoParseTask, src.video_parse_task_id)
        task.parse_config = {**task.parse_config, "profile": "edit_reference"}
        stored = s.scalar(select(ChunkParseContent).join(ChunkParseTask).where(
            ChunkParseTask.video_task_id == task.id, ChunkParseTask.chunk_id == src.video_chunk_ids[0]))
        detail = deepcopy(stored.detail)
        detail[INDEX_EMBEDDINGS_DETAIL_KEY]["receipt"]["edit_reference"] = {
            "index_name": index, "segments": [obs.segment.segment_id]}
        stored.content, stored.detail = doc, detail
    hit = {"tenant_id": src.tenant_id, "video_id": src.video_id, "series_id": src.series_id, "task_id": src.video_parse_task_id,
           "chunk_id": src.video_chunk_ids[0], "version": 1, "generation": 1, "segment_id": obs.segment.segment_id,
           "fingerprint": observation_fingerprint(obs)}

    def resolve(candidate, tenant=src.tenant_id, series_ids=None):
        return persistence.reference_observations([candidate], tenant_id=tenant, series_ids=series_ids, index_name=index)

    assert resolve(hit) == [(hit, obs)]
    assert resolve(hit, tenant="outside") == []
    assert resolve(hit, series_ids=["outside"]) == []
    for key in ("generation", "version", "tenant_id", "fingerprint", "segment_id", "video_id"):
        assert resolve({**hit, key: 2 if key in ("generation", "version") else "other"}) == []
    with factory.begin() as s:
        task = s.get(VideoParseTask, src.video_parse_task_id)
        task.status = "INDEXING"
    assert resolve(hit) == []


def test_reference_playback_uses_local_authority_and_source_interval():
    def find(**kwargs):
        return {"templates": [], "techniques": [{"segment_id": "seg-0000100", "start_ms": 100, "end_ms": 290,
            "media": {"original_url": "/api/videos/content?videoId=one&variant=original"}}]}
    with _client(_service(find_edit_templates=find)) as client:
        result = _call(client, "find_edit_templates", {"query": "人物登场"})
        assert result["structuredContent"]["techniques"][0]["media"]["play_url"] == (
            "http://127.0.0.1:9621/api/videos/content?videoId=one&variant=original#t=0.100,0.290")
