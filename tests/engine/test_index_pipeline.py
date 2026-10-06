from __future__ import annotations

import json
from dataclasses import replace

import pytest

from engine.parsev2.schemas import VideoParseResult
from engine.rag.encoding import embedding_receipt
from engine.rag.indexing.pipeline import (
    ChunkIndexSource,
    ChunkIndexingContract,
    compute_source_revision,
    finalize_chunk_index,
    plan_chunk_index,
)
from engine.rag.encoding import (
    EmbeddingArtifact,
    EncoderContract,
    SparseEmbeddingArtifact,
    SparseEncoderContract,
    accept_embedding_result,
    accept_sparse_result,
    reuse_embedding_artifact,
    reuse_sparse_embedding_artifact,
    sparse_embedding_receipt,
    validate_embedding_artifact,
)
from engine.rag.vision.inputs import (
    OrderedFrame,
    OrderedFrameSamplingContract,
    OrderedFramesInputContract,
    OrderedFramesManifest,
    VideoClipInputContract,
    VideoClipManifest,
    compute_visual_input_revision,
    plan_ordered_frame_timestamps,
)
from engine.rag.indexing.projection import SourceEnrichment
from engine.rag.retrieval.fusion import (
    VersionedRankedVideoChunkMatch,
    fuse_versioned_video_chunk_rankings,
)


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
    sparse_encoder=None,
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
        sparse_encoder=sparse_encoder,
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


def _sampling() -> OrderedFrameSamplingContract:
    return OrderedFrameSamplingContract(fps=0.2, max_frames_per_chunk=4)


def _frames_contract(
    sampling: OrderedFrameSamplingContract | None = None, **updates: object,
) -> OrderedFramesInputContract:
    values = {
        "sampling": sampling or _sampling(),
        "max_height": 720,
        "mime_type": "image/jpeg",
        "preprocess_revision": "jpeg-rgb-resize-v1",
    }
    values.update(updates)
    return OrderedFramesInputContract.from_sampling(**values)


def _frames_manifest(
    contract: OrderedFramesInputContract,
    actual_times: tuple[float, ...] | None = None,
) -> OrderedFramesManifest:
    assert contract.sampling is not None
    requested = plan_ordered_frame_timestamps(0, 10, contract.sampling)
    actual_times = requested if actual_times is None else actual_times
    assert len(requested) == len(actual_times)
    return OrderedFramesManifest(
        frames=tuple(
            OrderedFrame(index, actual, chr(97 + index) * 64, planned)
            for index, (actual, planned) in enumerate(zip(actual_times, requested))
        ),
        sampling_contract_id=contract.sampling_contract_id,
        mime_type=contract.mime_type,
        preprocess_revision=contract.preprocess_revision,
        sampling=contract.sampling,
        max_height=contract.max_height,
    )


def _frames_plan(contract: OrderedFramesInputContract, manifest: OrderedFramesManifest):
    return plan_chunk_index(
        _source(visual_input=manifest), _contract(visual_input_contract=contract),
    )


def _sparse_encoder(**updates: object) -> SparseEncoderContract:
    values = {
        "provider": "local",
        "model": "sparse-v1",
        "encoder_revision": "runtime-v1",
    }
    values.update(updates)
    return SparseEncoderContract(**values)


def _sparse_plan(*, result=None, enrichments=(), encoder=None):
    return plan_chunk_index(
        _source(result=result, enrichments=enrichments),
        _contract(sparse_encoder=encoder or _sparse_encoder()),
    )


def _dense_artifacts(plan) -> tuple[EmbeddingArtifact, ...]:
    return tuple(
        accept_embedding_result(
            request,
            modality=request.target.modality,
            model=request.target.model,
            vector=[1, 0] if request.channel == "semantic" else [0, 1],
        )
        for request in plan.encoding_requests
    )


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


@pytest.mark.parametrize(
    ("fps", "cap", "expected"),
    [
        (0.2, 10, (12.5, 17.5)),
        (2.0, 3, (35 / 3, 15.0, 55 / 3)),
        (0.01, 10, (15.0,)),
    ],
)
def test_uniform_midpoint_sampling_uses_absolute_times_and_frame_limits(
    fps: float, cap: int, expected: tuple[float, ...],
) -> None:
    sampling = OrderedFrameSamplingContract(
        fps=fps,
        max_frames_per_chunk=cap,
        strategy="uniform_midpoint_v1",
    )
    assert plan_ordered_frame_timestamps(10, 20, sampling) == pytest.approx(expected)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("fps", 0), ("fps", float("nan")),
        ("max_frames_per_chunk", 0), ("strategy", "uniform_edges_v1"),
    ],
)
def test_ordered_frame_sampling_rejects_invalid_parameters(field: str, value: object) -> None:
    values = {"fps": 0.2, "max_frames_per_chunk": 4, field: value}
    with pytest.raises(ValueError):
        OrderedFrameSamplingContract(**values)


def test_structured_frame_manifest_persists_rules_and_realized_evidence() -> None:
    sampling = _sampling()
    contract = _frames_contract(sampling)
    manifest = _frames_manifest(contract, (2.4, 7.6))
    persisted = manifest.as_dict()
    assert contract.sampling_contract_id == sampling.sampling_contract_id
    assert contract.claims()["sampling"] == sampling.claims()
    assert contract.claims()["max_height"] == 720
    assert manifest.input_contract == contract
    assert json.loads(json.dumps(persisted)) == persisted
    frame = persisted["frames"][0]
    assert (frame["at_seconds"], frame["requested_at_seconds"]) == (2.4, 2.5)
    assert frame["input_sha256"] == "a" * 64
    plan = _frames_plan(contract, manifest)
    document = finalize_chunk_index(plan, _dense_artifacts(plan))
    assert document.receipt.as_dict()["ordered_frames_manifest"] == persisted


def test_structured_manifest_validates_requested_plan_and_actual_times() -> None:
    contract = _frames_contract()
    manifest = _frames_manifest(contract)
    requested_failures = (
        manifest.frames[:-1],
        (replace(manifest.frames[0], requested_at_seconds=2.6), manifest.frames[1]),
    )
    for frames in requested_failures:
        with pytest.raises(ValueError, match="sampling time plan"):
            _frames_plan(contract, replace(manifest, frames=frames))

    with pytest.raises(ValueError, match="ordered by at_seconds"):
        replace(
            manifest,
            frames=(
                replace(manifest.frames[0], at_seconds=8),
                replace(manifest.frames[1], at_seconds=2),
            ),
        )
    outside = replace(
        manifest,
        frames=(manifest.frames[0], replace(manifest.frames[1], at_seconds=10)),
    )
    with pytest.raises(ValueError, match="half-open Chunk"):
        _frames_plan(contract, outside)


@pytest.mark.parametrize(
    ("scope", "field", "value"),
    [
        ("sampling", "fps", 0.3), ("sampling", "max_frames_per_chunk", 1),
        ("input", "max_height", 1080), ("input", "mime_type", "image/png"),
        ("input", "preprocess_revision", "jpeg-rgb-resize-v2"),
    ],
)
def test_structured_frame_rule_change_rejects_old_visual_reuse(
    scope: str,
    field: str,
    value: object,
) -> None:
    old_contract = _frames_contract()
    if scope == "sampling":
        assert old_contract.sampling is not None
        new_contract = _frames_contract(replace(old_contract.sampling, **{field: value}))
    else:
        new_contract = _frames_contract(**{field: value})
    old = _frames_plan(old_contract, _frames_manifest(old_contract))
    new = _frames_plan(new_contract, _frames_manifest(new_contract))
    assert (old_contract.sampling_contract_id != new_contract.sampling_contract_id) == (
        scope == "sampling"
    )
    assert old.vision_encoder_id != new.vision_encoder_id
    receipt = embedding_receipt(old.vision_request.target, [0.0, 1.0])
    assert reuse_embedding_artifact(
        new.vision_request,
        source_kind="v2_index_cache",
        metadata=receipt,
        vector=receipt["vector"],
    ) is None


@pytest.mark.parametrize("change", ["actual_time", "frame_bytes"])
def test_realized_frame_change_invalidates_only_visual_input_reuse(change: str) -> None:
    contract = _frames_contract()
    original = _frames_manifest(contract)
    first = replace(
        original.frames[0],
        **({"at_seconds": 2.4} if change == "actual_time" else {"input_sha256": "f" * 64}),
    )
    changed = replace(original, frames=(first, original.frames[1]))
    old = _frames_plan(contract, original)
    new = _frames_plan(contract, changed)

    assert old.source_revision == new.source_revision
    assert old.projection_revision == new.projection_revision
    assert old.vision_request.target.input_sha256 != new.vision_request.target.input_sha256
    receipt = embedding_receipt(old.vision_request.target, [0.0, 1.0])
    assert reuse_embedding_artifact(
        new.vision_request,
        source_kind="v2_index_cache",
        metadata=receipt,
        vector=receipt["vector"],
    ) is None
    with pytest.raises(ValueError, match="manifest differs from its input identity"):
        replace(old.vision_request, visual_input=changed)


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


def test_sparse_is_opt_in_and_persists_versioned_identity() -> None:
    disabled = plan_chunk_index(_source(), _contract())
    disabled_document = finalize_chunk_index(disabled, _dense_artifacts(disabled))
    assert disabled.sparse_request is None and disabled.sparse_encoder_id is None
    assert "sparse_encoder_id" not in disabled_document.receipt.as_dict()
    encoder = _sparse_encoder()
    plan = _sparse_plan(encoder=encoder)
    request = plan.sparse_request
    assert request is not None
    assert [item.channel for item in plan.encoding_requests] == ["semantic", "vision"]
    assert request.text == plan.projection.semantic_text.strip()
    artifact = accept_sparse_result(
        request, model=encoder.model, weights={"scene": 0.4, "dialogue": 1.2},
    )
    document = finalize_chunk_index(
        plan, _dense_artifacts(plan), sparse_artifact=artifact,
    )
    assert document.sparse_token_weights == (("dialogue", 1.2), ("scene", 0.4))
    assert document.receipt.as_dict()["sparse_encoder_id"] == encoder.encoder_id

    ranked = VersionedRankedVideoChunkMatch(
        rank=1, video_chunk_id="chunk-1", score=0.9,
        retrieval_identity=document.retrieval_identity,
    )
    for by_alias, key in ((False, "sparse_encoder_id"), (True, "sparseEncoderId")):
        wire = ranked.model_dump(mode="json", by_alias=by_alias)
        assert wire["retrieval_identity"][key] == encoder.encoder_id
        assert VersionedRankedVideoChunkMatch.model_validate(wire) == ranked
    changed_identity = document.retrieval_identity.model_copy(
        update={"sparse_encoder_id": "sparse:sha256:" + "f" * 64},
    )
    with pytest.raises(ValueError, match="identity changed between channels"):
        fuse_versioned_video_chunk_rankings(
            [ranked], [ranked.model_copy(update={"retrieval_identity": changed_identity})],
            text_weight=1, vision_weight=1, channel_top_k=10, rrf_k=60,
        )

    old = _sparse_plan(enrichments=(SourceEnrichment(
        language="zh-Hans", questions=["她说了什么？"],
    ),), encoder=encoder)
    new = _sparse_plan(enrichments=(SourceEnrichment(
        language="zh-Hans", questions=["是谁说了没事？"],
    ),), encoder=encoder)
    assert old.projection_revision != new.projection_revision
    assert old.sparse_request.input_sha256 == new.sparse_request.input_sha256


def test_sparse_artifact_and_plan_proofs_must_match_exactly() -> None:
    plan = _sparse_plan()
    request = plan.sparse_request
    assert request is not None
    valid = accept_sparse_result(request, model=request.encoder.model, weights={"scene": 1})
    dense = _dense_artifacts(plan)

    with pytest.raises(ValueError, match="required"):
        finalize_chunk_index(plan, dense)
    for artifact, message in (
        (replace(valid, input_sha256="f" * 64), "input"),
        (replace(valid, encoder_id="sparse:sha256:" + "f" * 64), "encoder"),
    ):
        with pytest.raises(ValueError, match=message):
            finalize_chunk_index(plan, dense, sparse_artifact=artifact)
    disabled = plan_chunk_index(_source(), _contract())
    with pytest.raises(ValueError, match="not requested"):
        finalize_chunk_index(disabled, _dense_artifacts(disabled), sparse_artifact=valid)
    with pytest.raises(ValueError, match="model"):
        accept_sparse_result(request, model="other", weights={"scene": 1})
    with pytest.raises(ValueError, match="duplicate"):
        SparseEmbeddingArtifact(
            request.input_sha256, request.encoder.encoder_id,
            (("scene", 1.0), ("scene", 2.0)),
        )

    other = _sparse_plan(result=_result("其实我很难过"))
    for forged in (
        replace(plan, sparse_request=other.sparse_request),
        replace(plan, sparse_encoder_id="sparse:sha256:" + "f" * 64),
        replace(plan, sparse_request=None),
    ):
        with pytest.raises(ValueError, match="sparse request"):
            finalize_chunk_index(forged, _dense_artifacts(forged), sparse_artifact=valid)


def test_sparse_empty_activation_and_cache_proof_are_explicit() -> None:
    empty = _sparse_plan(result=VideoParseResult(persons=[], timeline=[]))
    assert empty.sparse_encoder_id is not None and empty.sparse_request is None
    empty_document = finalize_chunk_index(empty, _dense_artifacts(empty))
    assert empty_document.sparse_input_sha256 is None
    assert empty_document.sparse_token_weights is None

    plan = _sparse_plan()
    request = plan.sparse_request
    assert request is not None
    artifact = accept_sparse_result(
        request, model=request.encoder.model, weights={"scene": 0},
    )
    receipt = sparse_embedding_receipt(request, artifact)
    document = finalize_chunk_index(
        plan, _dense_artifacts(plan), sparse_artifact=artifact,
    )
    assert artifact.token_weights == document.sparse_token_weights == ()
    assert receipt["token_weights"] == {}
    assert reuse_sparse_embedding_artifact(
        request, source_kind="v2_index_cache", metadata=receipt,
    ) == artifact

    changed_input = _sparse_plan(result=_result("其实我很难过")).sparse_request
    assert changed_input is not None
    assert reuse_sparse_embedding_artifact(
        changed_input, source_kind="v2_index_cache", metadata=receipt,
    ) is None
    for field, value in (
        ("provider", "remote"), ("model", "sparse-v2"),
        ("encoder_revision", "runtime-v2"), ("normalization", "l2"),
    ):
        changed = _sparse_plan(encoder=replace(_sparse_encoder(), **{field: value}))
        assert reuse_sparse_embedding_artifact(
            changed.sparse_request, source_kind="v2_index_cache", metadata=receipt,
        ) is None
    invalid = {**receipt, "token_weights": {"scene": 10**10000}}
    assert reuse_sparse_embedding_artifact(
        request, source_kind="v2_index_cache", metadata=invalid,
    ) is None
