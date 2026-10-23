from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from pathlib import Path

import pytest

from engine.rag.indexing.events import EventIndexingContract
from engine.rag.vision.event_frames import EventFrameContract
from gateway.embedding import EmbeddingResult
from server.service.video_parse_persistence import INDEX_EMBEDDINGS_DETAIL_KEY
import server.service.video_parse_index_coordinator as coordinator_module
from server.service.video_parse_index_coordinator import (
    VideoParseIndexCoordinator, VideoParseIndexCoordinationError,
)
from server.service.video_parse_media import VideoParseMediaService
from tests.server.test_video_parse_index_coordinator import (
    _Embedder, _Repository, _Store, _content, _contract, _fixture, _lease,
)


class EventRepository(_Repository):
    """Keep the production per-Event merge semantics instead of the old shallow fake."""

    def __init__(self, snapshot):
        super().__init__(snapshot)
        self.targets = []

    def load_chunk_indexing_snapshot(self, lease, *, event_target=None, now=None):
        self.targets.append(("load", deepcopy(event_target)))
        return self.snapshot

    def save_index_embeddings(self, lease, chunk_parse_task_id, content_version,
                              representations, *, receipt=None, event_target=None, now=None):
        self.targets.append(("save", deepcopy(event_target)))
        self.saved.append((chunk_parse_task_id, content_version, deepcopy(representations), deepcopy(receipt)))
        envelope = self.snapshot.chunk.content_detail.setdefault(INDEX_EMBEDDINGS_DETAIL_KEY, {
            "content_version": content_version, "representations": {}, "receipt": {},
        })
        assert envelope["content_version"] == content_version
        cached = envelope["representations"]
        for key, value in representations.items():
            if key == "events":
                events = cached.setdefault("events", {})
                for event_id, event in value.items():
                    events[event_id] = deepcopy(event)
            else:
                cached[key] = deepcopy(value)
        envelope["receipt"].update(deepcopy(receipt or {}))

    def complete_chunk_indexing(self, lease, content_version, index_generation, *, event_target=None, now=None):
        self.targets.append(("complete", deepcopy(event_target)))
        return super().complete_chunk_indexing(lease, content_version, index_generation, now=now)


class EventStore:
    index_name = "seekino_video_events_development_v3"

    def __init__(self):
        self.documents = {}
        self.bulk_calls = []
        self.deleted = []
        self.bound = []
        self.visible = True
        self.set_matches = True
        self.fail_write = None

    def bind_contract(self, contract):
        self.bound.append(contract)

    def ensure_index(self, contract):
        self.bound.append(contract)

    def verify_ready(self, documents):
        return self.visible and all(self.documents.get(document.event_id) == document for document in documents)

    def bulk_upsert(self, documents, *, write_version=None, refresh=False):
        self.bulk_calls.append((tuple(documents), write_version, refresh))
        written = documents[:1] if self.fail_write == "partial" else documents
        self.documents.update((document.event_id, document) for document in written)
        if self.fail_write:
            raise TimeoutError("Event bulk response lost")

    def delete_older(self, task_id, chunk_id, version):
        self.deleted.append((task_id, chunk_id, version))

    def verify_set(self, task_id, chunk_id, version, expected_ids):
        actual = {key for key, value in self.documents.items()
                  if (value.parse_task_id, value.video_chunk_id, value.version) == (task_id, chunk_id, version)}
        return self.set_matches and actual == set(expected_ids)


class EventEmbedder(_Embedder):
    def __init__(self):
        super().__init__()
        self.text_inputs = []
        self.fail_text_call = None
        self.image_calls = []
        self.fail_image_call = None

    def embed_text(self, text):
        self.text_inputs.append(text)
        if len(self.text_inputs) == self.fail_text_call:
            raise RuntimeError("text provider failed")
        return super().embed_text(text)

    def embed_image(self, path):
        content = Path(path).read_bytes()
        self.image_calls.append(content)
        if len(self.image_calls) == self.fail_image_call:
            raise RuntimeError("image provider failed")
        timestamp = float(content.decode().split(":")[1])
        vector = [0., 2.] if timestamp == 3. else [2., 0.]
        return EmbeddingResult(model="embed-v1", modality="image", vector=vector)


def _event_setup(tmp_path, *, event_count=1, warm_chunk=True):
    ids, media, snapshot = _fixture(tmp_path)
    content = _content()
    content["timeline"][0].update(start_seconds=1000, end_seconds=5000, text_variants=[
        {"language": "ja", "role": "ORIGINAL", "text": "大丈夫"},
        {"language": "zh-Hans", "role": "TRANSLATION", "text": "我没事"},
    ])
    if event_count == 0:
        content["timeline"] = []
    elif event_count == 2:
        content["timeline"].append(dict(event_type="ACTION", start_seconds=5000, end_seconds=6000,
            description="人物挥手", participants=["千早爱音"], visible_persons=["千早爱音"]))
    snapshot = replace(snapshot, chunk=replace(snapshot.chunk, content=content, end_seconds=6000))
    repository, chunk_store, embedder = EventRepository(snapshot), _Store(), EventEmbedder()
    commands, temporary_files = [], []

    def run(command):
        commands.append(tuple(command))
        output = Path(command[-1])
        if "-frames:v" in command:
            output.write_bytes(("frame:" + command[command.index("-ss") + 1]).encode())
            temporary_files.append(output)
        else:
            output.write_bytes(b"deterministic-video-segment")

    media_service = VideoParseMediaService(tmp_path / "media-work", command_runner=run)
    chunk_contract = _contract()
    event_contract = EventIndexingContract(semantic_encoder=chunk_contract.semantic_encoder,
        vision_encoder=chunk_contract.vision_encoder, capture=EventFrameContract(max_height=720))
    events = EventStore()
    target = dict(index_name=events.index_name, contract_id=event_contract.contract_id, contract=event_contract.as_dict())
    lease = replace(_lease(ids["video_task"], ids["chunk"], ids["chunk_task"]), lease_version=9)
    if warm_chunk:
        VideoParseIndexCoordinator(repository, chunk_store, embedder,
            media_resolver=lambda _: media, media_service=media_service,contract=chunk_contract).index_chunk(lease)
        repository.completed.clear()
        repository.targets.clear()
        repository.saved.clear()
        embedder.text_calls = embedder.video_calls = 0
        embedder.text_inputs.clear()
    coordinator = VideoParseIndexCoordinator(repository,chunk_store,embedder,
        media_resolver=lambda _:media,media_service=media_service,contract=chunk_contract,
        event_store=events,event_target=target)
    return coordinator,repository,chunk_store,events,embedder,lease,target,commands,temporary_files


def test_current_chunk_still_builds_missing_events_and_forwards_frozen_target_and_lease(tmp_path):
    service,repository,chunks,events,embedder,lease,target,commands,temporary = _event_setup(tmp_path)
    old_chunk_writes = len(chunks.bulk_calls)
    assert service.index_chunk(lease) == "complete"
    assert len(chunks.bulk_calls) == old_chunk_writes
    assert embedder.video_calls == 0 and embedder.text_calls == 3
    assert embedder.text_inputs == ["千早爱音回答", "大丈夫", "我没事"]
    assert len(embedder.image_calls) == 3
    document = events.bulk_calls[0][0][0]
    assert [(item.source, item.language, item.role) for item in document.semantic_vectors] == [
        ("description", None, None), ("dialogue", "ja", "ORIGINAL"),
        ("dialogue", "zh-Hans", "TRANSLATION"),
    ]
    assert document.vision_vector == pytest.approx((2 / 5 ** .5, 1 / 5 ** .5))
    assert events.bulk_calls[0][1:] == (9,"wait_for")
    assert events.bound[-1].model_dump(mode="json") == target["contract"]
    assert all(frozen == target for _operation,frozen in repository.targets)
    assert {name for name,_target in repository.targets} == {"load","save","complete"}
    receipt = repository.snapshot.chunk.content_detail[INDEX_EMBEDDINGS_DETAIL_KEY]["receipt"]["events"]
    assert receipt["event_count"] == 1 and receipt["event_ids"] == [document.event_id]
    assert [frame.requested_at_ms for frame in document.detail.capture.frames] == [1666,3000,4333]
    assert all(frame.actual_at_ms is None for frame in document.detail.capture.frames)
    assert temporary and all(not path.exists() for path in temporary)


@pytest.mark.parametrize("stale_proof", ["contract_id", "semantic_inputs"])
def test_event_indexing_replaces_vectors_with_stale_encoding_proof(tmp_path, stale_proof):
    service,repository,_chunks,events,embedder,lease,_target,_commands,_temporary = _event_setup(tmp_path)
    assert service.index_chunk(lease) == "complete"
    entry = next(iter(repository.snapshot.chunk.content_detail[INDEX_EMBEDDINGS_DETAIL_KEY]["representations"]["events"].values()))
    if stale_proof == "contract_id":
        entry[stale_proof] = "event-index:sha256:" + "0" * 64
    else:
        entry[stale_proof][next(iter(entry[stale_proof]))] = "0" * 64
    entry["document"]["semantic_vectors"][0]["vector"] = [0., 1.]
    embedder.text_inputs.clear()

    assert service.index_chunk(lease) == "complete"
    assert embedder.text_inputs == (["千早爱音回答", "大丈夫", "我没事"]
                                    if stale_proof == "contract_id" else ["千早爱音回答"])
    document = next(iter(events.documents.values()))
    assert document.semantic_vectors[0].vector == (1., 0.)
    assert any(field.field == "dialogue" and field.raw_text == "我没事" for field in document.lexical_fields)


def test_second_text_failure_reuses_description_then_completes_all_languages(tmp_path):
    service,repository,_chunks,events,embedder,lease,_target,_commands,_temporary = _event_setup(tmp_path)
    embedder.fail_text_call = 2
    with pytest.raises(RuntimeError, match="text provider"):
        service.index_chunk(lease)
    assert not repository.completed and not events.bulk_calls and not embedder.image_calls
    assert embedder.text_inputs == ["千早爱音回答", "大丈夫"]

    embedder.fail_text_call = None
    assert service.index_chunk(lease) == "complete"
    assert embedder.text_inputs == ["千早爱音回答", "大丈夫", "大丈夫", "我没事"]
    assert len(next(iter(events.documents.values())).semantic_vectors) == 3


@pytest.mark.parametrize("retry_image", [False, True])
def test_changed_translation_reuses_other_text_vectors_from_complete_cache(tmp_path, retry_image):
    service,repository,_chunks,events,embedder,lease,_target,_commands,_temporary = _event_setup(tmp_path)
    assert service.index_chunk(lease) == "complete"
    event_id = next(iter(events.documents))
    repository.snapshot.chunk.content["timeline"][0]["text_variants"][1]["text"] = "我很好"
    embedder.text_inputs.clear()
    if retry_image:
        embedder.fail_image_call = len(embedder.image_calls) + 1
        with pytest.raises(RuntimeError, match="image provider"):
            service.index_chunk(lease)
        embedder.fail_image_call = None

    assert service.index_chunk(lease) == "complete"
    assert embedder.text_inputs == ["我很好"]
    assert set(events.documents) == {event_id}
    assert any(field.raw_text == "我很好" for field in events.documents[event_id].lexical_fields)


@pytest.mark.parametrize("damage", ["missing", "invalid_vector"])
def test_incomplete_or_corrupt_complete_text_cache_is_rebuilt(tmp_path, damage):
    service,repository,_chunks,events,embedder,lease,_target,_commands,_temporary = _event_setup(tmp_path)
    assert service.index_chunk(lease) == "complete"
    entry = next(iter(repository.snapshot.chunk.content_detail[INDEX_EMBEDDINGS_DETAIL_KEY]["representations"]["events"].values()))
    if damage == "missing":
        entry["document"]["semantic_vectors"].pop()
    else:
        entry["document"]["semantic_vectors"][1]["vector"] = [1.]
    embedder.text_inputs.clear()

    assert service.index_chunk(lease) == "complete"
    assert embedder.text_inputs == ["千早爱音回答", "大丈夫", "我没事"]
    assert len(next(iter(events.documents.values())).semantic_vectors) == 3


def test_image_retry_reuses_semantic_and_partial_frames_then_complete_cache_skips_media(tmp_path):
    service,repository,_chunks,events,embedder,lease,_target,_commands,temporary = _event_setup(tmp_path)
    embedder.fail_image_call = 2
    with pytest.raises(RuntimeError,match="image provider"):
        service.index_chunk(lease)
    assert not repository.completed
    assert embedder.text_calls == 3 and len(embedder.image_calls) == 2
    entry = next(iter(repository.snapshot.chunk.content_detail[INDEX_EMBEDDINGS_DETAIL_KEY]["representations"]["events"].values()))
    assert len(entry["semantic_artifacts"]) == 3 and len(entry["frame_artifacts"]) == 1
    assert all(not path.exists() for path in temporary)
    embedder.fail_image_call = None
    assert service.index_chunk(lease) == "complete"
    assert embedder.text_calls == 3 and len(embedder.image_calls) == 4
    calls, frames = len(events.bulk_calls), len(temporary)
    service._media_resolver = lambda _:pytest.fail("completed Event cache must not reopen media")
    assert service.index_chunk(lease) == "complete"
    assert embedder.text_calls == 3 and len(embedder.image_calls) == 4
    assert len(events.bulk_calls) == calls and len(temporary) == frames


def test_empty_timeline_records_verified_zero_event_receipt_without_encoding(tmp_path):
    service,repository,_chunks,events,embedder,lease,_target,_commands,temporary = _event_setup(tmp_path,event_count=0)
    assert service.index_chunk(lease) == "complete"
    assert embedder.text_calls == embedder.video_calls == 0 and not embedder.image_calls
    assert not temporary and not events.bulk_calls
    receipt = repository.snapshot.chunk.content_detail[INDEX_EMBEDDINGS_DETAIL_KEY]["receipt"]["events"]
    assert receipt["event_count"] == 0 and receipt["event_ids"] == []
    assert events.deleted == [(lease.video_parse_task_id,lease.video_chunk_id,2)]


@pytest.mark.parametrize("failure",["partial_write","invisible","wrong_set"])
def test_partial_write_or_unverified_event_set_never_completes_and_retry_reuses_cache(tmp_path,failure):
    service,repository,_chunks,events,embedder,lease,_target,_commands,_temporary = _event_setup(tmp_path,event_count=2)
    if failure == "partial_write":
        events.fail_write = "partial"
    elif failure == "invisible":
        events.visible = False
    else:
        events.set_matches = False
    with pytest.raises((TimeoutError,VideoParseIndexCoordinationError)):
        service.index_chunk(lease)
    assert not repository.completed
    text_calls,image_calls = embedder.text_calls,len(embedder.image_calls)
    events.fail_write,events.visible,events.set_matches = None,True,True
    assert service.index_chunk(lease) == "complete"
    assert embedder.text_calls == text_calls and len(embedder.image_calls) == image_calls
    assert len(events.documents) == 2


def test_lost_successful_bulk_response_completes_after_exact_event_readback(tmp_path):
    service,repository,_chunks,events,embedder,lease,_target,_commands,_temporary = _event_setup(tmp_path)
    events.fail_write = "after_write"
    assert service.index_chunk(lease) == "complete"
    assert len(repository.completed) == 1 and len(events.bulk_calls) == 1
    assert embedder.text_calls == 3 and len(embedder.image_calls) == 3


@pytest.mark.parametrize(("flush_events", "event_saves"), [(8, 1), (1, 2)])
def test_event_cache_is_flushed_in_batches_not_per_encoding_step(tmp_path, monkeypatch, flush_events, event_saves):
    monkeypatch.setattr(coordinator_module, "EVENT_CACHE_FLUSH_EVENTS", flush_events)
    service,repository,_chunks,events,embedder,lease,_target,_commands,_temporary = _event_setup(tmp_path,event_count=2)
    assert service.index_chunk(lease) == "complete"
    assert embedder.text_calls == 4 and len(embedder.image_calls) == 4
    # Each save rewrites the whole cache row, so one per encoding step grows quadratically.
    batches = [saved[2]["events"] for saved in repository.saved if "events" in saved[2]]
    assert len(batches) == event_saves
    assert sum(len(batch) for batch in batches) == 2
    assert all("document" in entry for batch in batches for entry in batch.values())
    assert len(repository.saved) == event_saves + 1  # plus the final Event receipt
