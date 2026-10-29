from uuid import uuid4

import pytest

from engine.rag.encoding import EncoderContract
from evals.local_text_embedding import (
    Dataset, MeasuredEmbedder, build_documents, load_dataset, query_trial, require_loopback_url, source_result,
)
from engine.rag.retrieval.contracts import RawChunkHit, RawEventHit
from gateway.embedding import EmbeddingResult


class _TextEmbedder:
    def embed_text(self, text):
        assert text.strip()
        return EmbeddingResult(model="fixture", modality="text", vector=[0.6, 0.8])

    embed_query = embed_text

    def embed_image(self, *_args):
        pytest.fail("text pilot must not request images")

    def embed_video(self, *_args):
        pytest.fail("text pilot must not request video")


def _dataset(source="course.mp4"):
    return Dataset.model_validate({"samples": [
        {"id": "gradient", "source_path": source, "start_ms": 1200, "end_ms": 3400,
         "text": "Backpropagation uses the chain rule.", "queries": ["反向传播使用什么规则？"]},
        {"id": "tokenizer", "source_path": source, "start_ms": 7500, "end_ms": 9900,
         "text": "Tokenization maps text into token IDs.", "queries": ["如何把文本变成token？"]},
    ]})


def test_pilot_builds_text_only_documents_with_real_source_ranges():
    measured = MeasuredEmbedder(_TextEmbedder())
    contract, event_contract, chunks, events, facts, sources, scope = build_documents(
        _dataset(), uuid4(), EncoderContract("fixture", "fixture", "v1", 2), measured,
    )
    assert len(measured.calls) == 4
    assert all(call["operation"] == "text" and call["dimensions"] == 2 for call in measured.calls)
    assert contract.vision_encoder is event_contract.vision_encoder is None
    assert len(scope.tasks) == 1
    assert len(chunks) == len(events) == len(facts) == len(sources) == 2
    for chunk, event in zip(chunks, events, strict=True):
        sample = sources[chunk.video_chunk_id]
        assert chunk.vision_vector is event.vision_vector is None
        assert event.detail.capture.frames == ()
        fact = facts[chunk.video_chunk_id]
        assert fact.result.timeline[0].description == sample.text
        assert source_result(sample, start_ms=fact.start_seconds, end_ms=fact.end_seconds)["source_verified"]
        assert (chunk.chunk_start_seconds, chunk.chunk_end_seconds) == (sample.start_ms, sample.end_ms)
    with pytest.raises(ValueError, match="source times differ"):
        source_result(next(iter(sources.values())), start_ms=0, end_ms=1)
    with pytest.raises(ValueError, match="forbids"):
        measured.encode("image", "not a path")


def test_dataset_requires_existing_source_and_unique_ids(tmp_path):
    path = tmp_path / "dataset.json"
    path.write_text(_dataset().model_dump_json(), encoding="utf-8")
    with pytest.raises(ValueError, match="source file does not exist"):
        load_dataset(path)
    (tmp_path / "course.mp4").write_bytes(b"fixture, not decoded by this text-only experiment")
    loaded = load_dataset(path)
    assert loaded.samples[0].source_path == str((tmp_path / "course.mp4").resolve())
    raw = _dataset().model_dump()
    raw["samples"][1]["id"] = raw["samples"][0]["id"]
    with pytest.raises(ValueError, match="unique"):
        Dataset.model_validate(raw)


def test_query_trial_checks_chunk_and_event_sources_with_shared_query_encoding():
    dataset = _dataset()
    measured = MeasuredEmbedder(_TextEmbedder())
    contract, event_contract, chunk_docs, event_docs, facts, sources, scope = build_documents(
        dataset, uuid4(), EncoderContract("fixture", "fixture", "v1", 2), measured,
    )

    class Chunks:
        def recall(self, request, *, exact):
            assert exact
            if request.channel == "lexical":
                return []
            return [RawChunkHit(channel="semantic", video_chunk_id=doc.video_chunk_id,
                                retrieval_identity=doc.retrieval_identity, rank=index + 1, score=1.,
                                start_seconds=doc.chunk_start_seconds, end_seconds=doc.chunk_end_seconds)
                    for index, doc in enumerate(chunk_docs)]

    class Events:
        def search_lexical(self, *args, **kwargs):
            return []

        def search_vector(self, vector, *, channel, sources, filters, limit, exact):
            assert channel == "semantic" and exact
            return [RawEventHit(channel="semantic", event_id=doc.event_id,
                                parse_task_id=doc.parse_task_id, video_chunk_id=doc.video_chunk_id,
                                version=doc.version, timeline_index=doc.timeline_index,
                                event_type=doc.event_type, lexical_fields=doc.lexical_fields,
                                rank=index + 1, score=1.) for index, doc in enumerate(event_docs)]

    rows = query_trial(dataset, measured, Chunks(), Events(), contract, event_contract,
                       facts, sources, scope, top_k=2)
    assert len(rows) == 2
    assert len(measured.calls) == 6  # four document requests and one shared request per query
    assert all(row["chunk_hit_at_k"] and row["event_hit_at_k"] for row in rows)
    assert rows[0]["chunk_hit_at_1"] and not rows[1]["chunk_hit_at_1"]
    for row in rows:
        assert all(hit["source_verified"] for hit in row["chunk_top_k"] + row["event_top_k"])


@pytest.mark.parametrize("url", ["https://example.com", "http://user:password@localhost:9200", "http://127.0.0.1:9200/?key=secret"])
def test_pilot_forbids_nonlocal_or_credential_urls(url):
    with pytest.raises(ValueError, match="loopback"):
        require_loopback_url(url)


def test_pilot_accepts_loopback_only():
    assert require_loopback_url("http://127.0.0.1:9200") == "http://127.0.0.1:9200"
