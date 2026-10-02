from __future__ import annotations

from dataclasses import replace

import pytest

from engine.parsev2.schemas import VideoParseResult
from engine.retrieval.embedding_reuse import embedding_receipt
from engine.retrieval.index_pipeline import (
    ChunkIndexSource,
    ChunkIndexingContract,
    EmbeddingArtifact,
    EncoderContract,
    OrderedFrame,
    OrderedFramesInputContract,
    OrderedFramesManifest,
    VideoClipInputContract,
    VideoClipManifest,
    accept_embedding_result,
    compute_source_revision,
    compute_visual_input_revision,
    finalize_chunk_index,
    plan_chunk_index,
    reuse_embedding_artifact,
    validate_embedding_artifact,
)
from engine.retrieval.index_projection import SourceEnrichment


def _result(text: str = "我没事") -> VideoParseResult:
    return VideoParseResult.model_validate({
        "persons": ["千早爱音"],
        "timeline": [{
            "event_type": "DIALOGUE",
            "start_seconds": 1,
            "end_seconds": 3,
            "description": "千早爱音回答",
            "participants": ["千早爱音"],
            "visible_persons": ["千早爱音"],
            "speaker": "千早爱音",
            "text_variants": [{
                "language": "zh-Hans",
                "role": "ORIGINAL",
                "text": text,
            }],
        }],
    })


def _clip_contract(**updates: object) -> VideoClipInputContract:
    values = {
        "fps": 1.0,
        "max_height": 720,
        "mime_type": "video/mp4",
        "preprocess_revision": "ffmpeg-h264-v1",
    }
    values.update(updates)
    return VideoClipInputContract(**values)


def _contract(
    *,
    visual_input_contract=None,
    index_generation: int = 1,
) -> ChunkIndexingContract:
    return ChunkIndexingContract(
        retrieval_contract_id="parse-v2-retrieval-v1",
        semantic_encoder=EncoderContract(
            provider="ark",
            model="embed-v1",
            encoder_revision="gateway-ark-embedding-v1",
            dimensions=2,
        ),
        vision_encoder=EncoderContract(
            provider="ark",
            model="embed-v1",
            encoder_revision="gateway-ark-embedding-v1",
            dimensions=2,
        ),
        visual_input_contract=visual_input_contract or _clip_contract(),
        description_language="zh-Hans",
        index_generation=index_generation,
    )


def _clip(**updates: object) -> VideoClipManifest:
    values = {
        "input_sha256": "a" * 64,
        "start_seconds": 0,
        "end_seconds": 10,
        "fps": 1.0,
        "max_height": 720,
        "mime_type": "video/mp4",
        "preprocess_revision": "ffmpeg-h264-v1",
    }
    values.update(updates)
    return VideoClipManifest(**values)


def _source(
    *,
    result: VideoParseResult | None = None,
    visual_input=None,
    content_version: int = 2,
    enrichments=(),
) -> ChunkIndexSource:
    return ChunkIndexSource(
        workspace_id="workspace-1",
        series_id="series-1",
        video_id="video-1",
        video_chunk_id="chunk-1",
        parse_task_id="task-1",
        content_version=content_version,
        chunk_start_seconds=0,
        chunk_end_seconds=10,
        result=result or _result(),
        visual_input=visual_input or _clip(),
        enrichments=enrichments,
    )


def test_plan_projects_formal_source_and_emits_two_exact_encoding_requests() -> None:
    plan = plan_chunk_index(_source(), _contract())

    assert plan.source_revision == compute_source_revision(_result())
    assert plan.projection_revision
    assert [request.channel for request in plan.encoding_requests] == [
        "semantic",
        "vision",
    ]
    assert plan.semantic_request is not None
    assert plan.semantic_request.semantic_text == plan.projection.semantic_text
    assert plan.vision_request.visual_input == _clip()
    assert plan.vision_request.target.input_sha256 == "a" * 64
    assert plan.semantic_encoder_id.startswith("text:sha256:")
    assert plan.vision_encoder_id.startswith("video:sha256:")


def test_source_revision_tracks_only_formal_source_content() -> None:
    initial = plan_chunk_index(_source(content_version=2), _contract(index_generation=1))
    rebuilt = plan_chunk_index(_source(content_version=9), _contract(index_generation=7))
    changed = plan_chunk_index(_source(result=_result("其实我很难过")), _contract())

    assert initial.source_revision == rebuilt.source_revision
    assert initial.source_revision != changed.source_revision
    assert initial.projection_revision == rebuilt.projection_revision
    assert initial.vision_request.target.input_sha256 == rebuilt.vision_request.target.input_sha256


def test_questions_are_lexical_only_and_do_not_invalidate_semantic_vector() -> None:
    old = plan_chunk_index(
        _source(enrichments=(SourceEnrichment(
            language="zh-Hans",
            questions=["她说了什么？"],
        ),)),
        _contract(),
    )
    new = plan_chunk_index(
        _source(enrichments=(SourceEnrichment(
            language="zh-Hans",
            questions=["是谁说了没事？"],
        ),)),
        _contract(),
    )

    assert old.projection_revision != new.projection_revision
    assert old.source_revision == new.source_revision
    assert old.semantic_request is not None and new.semantic_request is not None
    assert old.semantic_request.target.input_sha256 == new.semantic_request.target.input_sha256
    assert "是谁说了没事？" not in new.semantic_request.semantic_text


def test_semantic_source_change_invalidates_only_the_semantic_representation() -> None:
    old = plan_chunk_index(_source(result=_result("我没事")), _contract())
    new = plan_chunk_index(_source(result=_result("其实我很难过")), _contract())

    assert old.semantic_request is not None and new.semantic_request is not None
    assert old.source_revision != new.source_revision
    assert old.projection_revision != new.projection_revision
    assert old.semantic_request.target.input_sha256 != new.semantic_request.target.input_sha256
    assert old.vision_request.target == new.vision_request.target


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("fps", 2.0),
        ("max_height", 1080),
        ("mime_type", "video/webm"),
        ("preprocess_revision", "ffmpeg-h264-v2"),
    ],
)
def test_visual_preprocess_change_invalidates_encoder_and_reuse_identity(
    field: str,
    value: object,
) -> None:
    old = plan_chunk_index(_source(), _contract())
    changed_input = replace(_clip(), **{field: value})
    changed_contract = replace(_clip_contract(), **{field: value})
    new = plan_chunk_index(
        _source(visual_input=changed_input),
        _contract(visual_input_contract=changed_contract),
    )

    assert old.vision_encoder_id != new.vision_encoder_id
    assert old.vision_request.target.input_contract_id != new.vision_request.target.input_contract_id
    receipt = embedding_receipt(old.vision_request.target, [0.0, 1.0])
    assert reuse_embedding_artifact(
        new.vision_request,
        source_kind="v2_index_cache",
        metadata=receipt,
        vector=receipt["vector"],
    ) is None


def test_ordered_frame_input_revision_covers_frame_order_time_and_content() -> None:
    contract = OrderedFramesInputContract(
        sampling_contract_id="uniform-three-frames-v1",
        mime_type="image/jpeg",
        preprocess_revision="rgb-resize-v1",
    )
    frames = (
        OrderedFrame(ordinal=0, at_seconds=1.0, input_sha256="a" * 64),
        OrderedFrame(ordinal=1, at_seconds=2.0, input_sha256="b" * 64),
    )
    manifest = OrderedFramesManifest(
        frames=frames,
        sampling_contract_id=contract.sampling_contract_id,
        mime_type=contract.mime_type,
        preprocess_revision=contract.preprocess_revision,
    )
    changed_time = replace(
        manifest,
        frames=(frames[0], replace(frames[1], at_seconds=2.5)),
    )
    changed_order = replace(
        manifest,
        frames=(
            replace(frames[0], input_sha256=frames[1].input_sha256),
            replace(frames[1], input_sha256=frames[0].input_sha256),
        ),
    )

    assert compute_visual_input_revision(manifest) != compute_visual_input_revision(changed_time)
    assert compute_visual_input_revision(manifest) != compute_visual_input_revision(changed_order)
    plan = plan_chunk_index(
        _source(visual_input=manifest),
        _contract(visual_input_contract=contract),
    )
    assert plan.vision_request.target.visual_input_kind == "ordered_frames"
    assert plan.vision_request.target.sampling_contract_id == contract.sampling_contract_id


def test_ordered_frames_must_stay_inside_the_chunk() -> None:
    contract = OrderedFramesInputContract(
        sampling_contract_id="uniform-v1",
        mime_type="image/jpeg",
        preprocess_revision="rgb-v1",
    )
    manifest = OrderedFramesManifest(
        frames=(OrderedFrame(ordinal=0, at_seconds=10, input_sha256="a" * 64),),
        sampling_contract_id=contract.sampling_contract_id,
        mime_type=contract.mime_type,
        preprocess_revision=contract.preprocess_revision,
    )

    with pytest.raises(ValueError, match="half-open Chunk"):
        plan_chunk_index(
            _source(visual_input=manifest),
            _contract(visual_input_contract=contract),
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("sampling_contract_id", "scene-aware-v2"),
        ("mime_type", "image/png"),
        ("preprocess_revision", "rgb-resize-v2"),
    ],
)
def test_ordered_frame_contract_change_invalidates_visual_identity(
    field: str,
    value: str,
) -> None:
    contract = OrderedFramesInputContract(
        sampling_contract_id="uniform-v1",
        mime_type="image/jpeg",
        preprocess_revision="rgb-resize-v1",
    )
    manifest = OrderedFramesManifest(
        frames=(OrderedFrame(ordinal=0, at_seconds=1, input_sha256="a" * 64),),
        sampling_contract_id=contract.sampling_contract_id,
        mime_type=contract.mime_type,
        preprocess_revision=contract.preprocess_revision,
    )
    old = plan_chunk_index(
        _source(visual_input=manifest),
        _contract(visual_input_contract=contract),
    )
    changed_contract = replace(contract, **{field: value})
    changed_manifest = replace(manifest, **{field: value})
    new = plan_chunk_index(
        _source(visual_input=changed_manifest),
        _contract(visual_input_contract=changed_contract),
    )

    assert old.vision_encoder_id != new.vision_encoder_id
    assert (
        old.vision_request.target.input_contract_id
        != new.vision_request.target.input_contract_id
    )
    assert (
        old.vision_request.target.input_sha256
        != new.vision_request.target.input_sha256
    )


def test_external_results_are_validated_before_finalizing_logical_document() -> None:
    plan = plan_chunk_index(_source(), _contract())
    assert plan.semantic_request is not None
    semantic = accept_embedding_result(
        plan.semantic_request,
        modality="text",
        model="embed-v1",
        vector=[1, 0],
    )
    vision = accept_embedding_result(
        plan.vision_request,
        modality="video",
        model="embed-v1",
        vector=[0, 1],
    )

    document = finalize_chunk_index(plan, [semantic, vision])

    assert document.projection_revision == plan.projection_revision
    assert document.semantic_input_sha256 == plan.semantic_request.target.input_sha256
    assert document.vision_input_sha256 == plan.vision_request.target.input_sha256
    assert document.semantic_vector == (1.0, 0.0)
    assert document.vision_vector == (0.0, 1.0)
    assert document.retrieval_identity.model_dump() == {
        "workspace_id": "workspace-1",
        "series_id": "series-1",
        "video_id": "video-1",
        "parse_task_id": "task-1",
        "content_version": 2,
        "index_generation": 1,
        "source_revision": plan.source_revision,
        "projection_revision": plan.projection_revision,
        "retrieval_contract_id": "parse-v2-retrieval-v1",
        "semantic_encoder_id": plan.semantic_encoder_id,
        "vision_encoder_id": plan.vision_encoder_id,
    }
    assert document.receipt.as_dict() == {
        "parse_task_id": "task-1",
        "video_chunk_id": "chunk-1",
        "content_version": 2,
        "source_revision": plan.source_revision,
        "projection_revision": plan.projection_revision,
        "retrieval_contract_id": "parse-v2-retrieval-v1",
        "semantic_encoder_id": plan.semantic_encoder_id,
        "vision_encoder_id": plan.vision_encoder_id,
        "index_generation": 1,
    }
    assert not hasattr(document, "document_id")
    assert not hasattr(document, "external_version")


@pytest.mark.parametrize(
    ("modality", "model", "vector"),
    [
        ("video", "embed-v1", [1, 0]),
        ("text", "another-model", [1, 0]),
        ("text", "embed-v1", [1]),
        ("text", "embed-v1", [0, 0]),
    ],
)
def test_external_response_must_match_request_contract(
    modality: str,
    model: str,
    vector: list[float],
) -> None:
    request = plan_chunk_index(_source(), _contract()).semantic_request
    assert request is not None

    with pytest.raises(ValueError):
        accept_embedding_result(
            request,
            modality=modality,
            model=model,
            vector=vector,
        )


def test_cached_artifact_must_match_channel_input_encoder_and_dimension() -> None:
    request = plan_chunk_index(_source(), _contract()).vision_request
    valid = EmbeddingArtifact(
        channel="vision",
        input_sha256=request.target.input_sha256,
        encoder_id=request.target.encoder_id,
        vector=(0.0, 1.0),
    )
    assert validate_embedding_artifact(request, valid) == (0.0, 1.0)

    with pytest.raises(ValueError, match="encoder"):
        validate_embedding_artifact(
            request,
            replace(valid, encoder_id="video:sha256:" + "f" * 64),
        )


def test_empty_projection_requests_only_required_visual_embedding() -> None:
    empty = VideoParseResult(persons=[], timeline=[])
    plan = plan_chunk_index(_source(result=empty), _contract())

    assert plan.semantic_request is None
    assert [request.channel for request in plan.encoding_requests] == ["vision"]
    vision = accept_embedding_result(
        plan.vision_request,
        modality="video",
        model="embed-v1",
        vector=[0, 1],
    )
    document = finalize_chunk_index(plan, [vision])
    assert document.semantic_input_sha256 is None
    assert document.semantic_vector is None
