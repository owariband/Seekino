from copy import deepcopy
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import select

from engine.parsev2.edit_reference import TechniqueObservation
from server.database import create_session_factory
from server.model.parse_content import ChunkParseContent, IntervalParseContent
from server.model.task import ChunkParseTask, IntervalParseTask, VideoParseTask
from server.repository.editing_template import EditingTemplateRepository
from server.repository.library import LibraryRepository
from server.service.library import LibraryService
from server.service.mcp_retrieval import McpRetrievalService
from server.service.video_parse_persistence import INDEX_EMBEDDINGS_DETAIL_KEY
from server.storage.elasticsearch_references import observation_fingerprint
from tests.engine.parsev2.test_edit_reference import make_chunk
from tests.server.test_mcp_transport import _call, _client, _service


@pytest.mark.parametrize("anchored", [False, True], ids=["legacy", "cited-signals"])
def test_search_and_style_card_replay_the_same_current_publication(repository, seed_video, anchored):
    engine, persistence = repository
    factory = create_session_factory(engine)
    src = seed_video(factory)
    index = "edit_references_test"
    doc = make_chunk()
    doc["observations"][0]["technique_key"] = "硬切"
    obs = TechniqueObservation.model_validate(doc["observations"][0])
    segment_id = obs.segment.segment_id
    card = {"summary": "构图变化", "techniques": [
        {"technique_key": "硬切", "description": "切换景别", "segment_ids": [segment_id]}]}
    interval = {"style_card": card}
    if anchored:
        interval = {"style_card": {"scope": "构图变化", "signals": [{"name": "切远", "description": "切换景别",
            "examples": [{"segment_id": segment_id, "at_ms": 167, "detail": "全身剪影"}]}]},
            "members": [{"sequence_no": 0, "observations": [{"segment_id": segment_id, "technique_key": "硬切",
                "name": obs.note.name, "summary": obs.note.summary, "role": obs.note.role, "camera": obs.note.motion.camera}]}]}
    with factory.begin() as session:
        task = session.get(VideoParseTask, src.video_parse_task_id)
        task.parse_config = {**task.parse_config, "profile": "edit_reference"}
        children = []
        for position, chunk_id in enumerate(src.video_chunk_ids):
            child = session.scalar(select(ChunkParseTask).where(
                ChunkParseTask.video_task_id == task.id, ChunkParseTask.chunk_id == chunk_id))
            stored = session.scalar(select(ChunkParseContent).where(ChunkParseContent.chunk_task_id == child.id))
            detail = deepcopy(stored.detail)
            detail[INDEX_EMBEDDINGS_DETAIL_KEY]["receipt"]["edit_reference"] = {
                "index_name": index, "segments": [] if position else [segment_id]}
            stored.content = {"observations": []} if position else doc
            stored.detail = detail
            children.append(child.id)
        interval_id = str(uuid4())
        session.add(IntervalParseTask(id=interval_id, video_task_id=task.id, chunk_task_id=children[0],
            start_sequence_no=0, chunk_count=2, chunk_task_ids=children, ready_mask=3,
            status="COMPLETE", next_execute_time=task.next_execute_time))
        session.add(IntervalParseContent(interval_task_id=interval_id, content=interval))

    hit = {"tenant_id": src.tenant_id, "video_id": src.video_id, "series_id": src.series_id,
           "task_id": src.video_parse_task_id, "chunk_id": src.video_chunk_ids[0], "version": 1,
           "generation": 1, "segment_id": segment_id, "fingerprint": observation_fingerprint(obs)}

    def resolve(candidate, tenant=src.tenant_id, series_ids=None):
        return persistence.reference_observations([candidate], tenant_id=tenant, series_ids=series_ids, index_name=index)

    published, = resolve(hit)
    assert published.key.model_dump() == hit
    assert published.observation == obs
    assert resolve(hit, tenant="outside") == []
    assert resolve(hit, series_ids=["outside"]) == []
    for key in ("generation", "version", "tenant_id", "fingerprint", "segment_id", "video_id"):
        assert resolve({**hit, key: 2 if key in ("generation", "version") else "other"}) == []

    service = McpRetrievalService(library=LibraryService(LibraryRepository(factory)), media=SimpleNamespace(get_original=lambda *a, **kw: True),
        editing_repository=EditingTemplateRepository(factory),
        event_retrieval=None, parse_persistence=persistence, local_tenant_id=src.tenant_id,
        reference_store=SimpleNamespace(index_name=index, search=lambda *a, **kw: [hit]))
    found, = service.find_edit_templates("人物登场")["techniques"]
    whole = service.get_style_card(src.video_id)
    exemplar, = whole["exemplars"]
    token = exemplar["evidence_ref"]
    assert token == found["evidence_ref"]
    if anchored:
        example, = whole["style_card"]["signals"][0]["examples"]
        assert example["evidence_ref"] == token and example["at_ms"] == 167
        assert whole["style_card_format"] == "anchored_signals"
    else:
        assert whole["style_card_format"] == "legacy_technique_groups"
    read = service.read_evidence(token)
    assert read["note"] == obs.note.model_dump()
    assert read["measured"]["frame_ms"] == [100, 133, 167]
    assert read["media"]["clip_url"] == found["media"]["clip_url"]
    chosen = service.read_evidence(token, start_ms=50, end_ms=300)
    assert "startMs=50&endMs=300" in chosen["media"]["clip_url"]
    cloud_media = service.read_evidence(token, cloud=True)["media"]
    assert cloud_media["original_url"] is None and cloud_media["clip_url"] == found["media"]["clip_url"]
    revision = dict(new_template={"name": "登场"}, template_id=None, parent_version_id=None, spec={}, runtime={},
                    visual_requirements={}, slots=[], params_schema={}, files={"cut.js": "export default 1"},
                    entry="cut.js", evaluation={"context": "first edit"}, check=None, request_id="first", source_refs=[token])
    saved = service.save_edit_template_version(**revision)
    ident = {"template_id": saved["template_id"], "version_id": saved["version_id"]}
    original = service.get_edit_template(**ident)["version"]
    assert original["sources"][0]["status"] == "current" and original["sources"][0]["note"] == obs.note.model_dump()
    with factory.begin() as session:
        session.get(VideoParseTask, src.video_parse_task_id).status = "INDEXING"
    assert resolve(hit) == []
    assert service.save_edit_template_version(**revision) == saved
    archived = service.get_edit_template(**ident)["version"]
    assert archived["sources"][0]["status"] == "unavailable" and archived["content_hash"] == original["content_hash"]
    with pytest.raises(ValueError, match="EVIDENCE_STALE"):
        service.read_evidence(token)
    with pytest.raises(ValueError, match="EVIDENCE_STALE"):
        service.get_style_card(src.video_id)


def test_reference_playback_uses_local_authority_and_exact_source_clip():
    clip = "/api/videos/clip?evidenceRef=ref&startMs=100&endMs=290"
    media = {"original_url": "/api/videos/content?videoId=one&variant=original", "clip_url": clip}
    def find(**kwargs):
        return {"templates": [], "techniques": [{"segment_id": "seg-0000100", "start_ms": 100,
                "end_ms": 290, "media": media.copy()}]}
    with _client(_service(find_edit_templates=find)) as client:
        result = _call(client, "find_edit_templates", {"query": "人物登场"})
        returned = result["structuredContent"]["techniques"][0]["media"]
        assert returned["clip_url"] == "http://127.0.0.1:9621" + clip
        assert returned["play_url"] == returned["clip_url"] + "&inline=true"
        media["clip_url"] = "https://outside.example/api/videos/clip?evidenceRef=ref"
        assert _call(client, "find_edit_templates", {"query": "人物登场"})["isError"]
