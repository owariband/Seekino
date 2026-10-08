from __future__ import annotations

import json
from dataclasses import replace

import pytest

from engine.parsev2.schemas import VideoParseResult
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
            modality="text" if request.channel == "semantic" else "video",
            model=request.encoder.model,
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


def _another_plan(plan, change: str):
    if change == "replace_version":
        return replace(plan, content_version=plan.content_version + 1)
    if change == "replace_generation":
        return replace(plan, index_generation=plan.index_generation + 1)
    return plan_chunk_index(
        _source(content_version=plan.content_version + (change == "version")),
        _contract(
            index_generation=plan.index_generation + (change == "generation"),
            sparse_encoder=plan.sparse_encoder,
        ),
    )


def test_plan_projects_formal_source_and_emits_two_exact_encoding_requests() -> None:
    plan = plan_chunk_index(_source(), _contract())

    assert (plan.content_version, plan.index_generation) == (2, 1)
    assert plan.projection_revision
    assert [request.channel for request in plan.encoding_requests] == [
        "semantic",
        "vision",
    ]
    assert plan.semantic_request is not None
    assert plan.semantic_request.semantic_text == plan.projection.semantic_text
    assert plan.vision_request.visual_input == _clip()
    assert plan.vision_request.visual_input.input_sha256 == "a" * 64
    assert plan.semantic_request.encoder == _contract().semantic_encoder
    assert plan.vision_request.encoder == _contract().vision_encoder
    for request in plan.encoding_requests:
        for field in ("target", "input_sha256", "encoder_id", "content_version", "index_generation"):
            assert not hasattr(request, field)


def test_source_revision_tracks_only_formal_source_content() -> None:
    initial_source = _source(content_version=2)
    rebuilt_source = _source(content_version=9)
    changed_source = _source(result=_result("其实我很难过"))
    initial = plan_chunk_index(initial_source, _contract(index_generation=1))
    rebuilt = plan_chunk_index(rebuilt_source, _contract(index_generation=7))

    assert compute_source_revision(initial_source.result) == compute_source_revision(rebuilt_source.result)
    assert compute_source_revision(initial_source.result) != compute_source_revision(changed_source.result)
    assert initial.projection_revision == rebuilt.projection_revision
    assert initial.vision_request.visual_input == rebuilt.vision_request.visual_input


def test_questions_are_lexical_only_and_do_not_change_semantic_input() -> None:
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
    assert old.semantic_request is not None and new.semantic_request is not None
    assert old.semantic_request.semantic_text == new.semantic_request.semantic_text
    assert "是谁说了没事？" not in new.semantic_request.semantic_text


def test_semantic_source_change_updates_text_and_preserves_visual_input() -> None:
    old = plan_chunk_index(_source(result=_result("我没事")), _contract())
    new = plan_chunk_index(_source(result=_result("其实我很难过")), _contract())

    assert old.semantic_request is not None and new.semantic_request is not None
    assert old.projection_revision != new.projection_revision
    assert old.semantic_request.semantic_text != new.semantic_request.semantic_text
    assert old.vision_request.visual_input == new.vision_request.visual_input
    assert old.vision_request.encoder == new.vision_request.encoder


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("fps", 2.0),
        ("max_height", 1080),
        ("mime_type", "video/webm"),
        ("preprocess_revision", "ffmpeg-h264-v2"),
    ],
)
def test_visual_preprocess_change_preserves_the_requested_media_contract(
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

    assert new.vision_request.visual_input == changed_input
    assert new.vision_request.visual_input.input_contract == changed_contract
    assert old.vision_request.visual_input != new.vision_request.visual_input


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
    assert plan.vision_request.visual_input == manifest
    assert plan.vision_request.visual_input.kind == "ordered_frames"
    assert plan.vision_request.visual_input.input_contract == contract


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
def test_ordered_frame_contract_change_updates_the_actual_visual_input(
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

    assert old.vision_request.visual_input.input_contract != new.vision_request.visual_input.input_contract
    assert new.vision_request.visual_input == changed_manifest
    assert new.vision_request.visual_input.input_contract == changed_contract


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
def test_structured_frame_rule_change_requires_a_result_for_the_new_plan(
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
    assert old.vision_request.visual_input.input_contract != new.vision_request.visual_input.input_contract
    old_result = _dense_artifacts(old)[1]
    with pytest.raises(ValueError, match="different plan request"):
        validate_embedding_artifact(new.vision_request, old_result)


@pytest.mark.parametrize("change", ["actual_time", "frame_bytes"])
def test_realized_frame_change_retains_actual_media_and_requires_its_own_result(change: str) -> None:
    contract = _frames_contract()
    original = _frames_manifest(contract)
    first = replace(
        original.frames[0],
        **({"at_seconds": 2.4} if change == "actual_time" else {"input_sha256": "f" * 64}),
    )
    changed = replace(original, frames=(first, original.frames[1]))
    old = _frames_plan(contract, original)
    new = _frames_plan(contract, changed)

    assert old.projection_revision == new.projection_revision
    assert new.vision_request.visual_input == changed
    assert compute_visual_input_revision(original) != compute_visual_input_revision(changed)
    changed_request = replace(old.vision_request, visual_input=changed)
    assert changed_request.visual_input == changed
    with pytest.raises(ValueError, match="different plan request"):
        validate_embedding_artifact(changed_request, _dense_artifacts(old)[1])


@pytest.mark.parametrize(("content_version", "index_generation"), [(2, 1), (9, 7)])
def test_external_results_are_validated_before_finalizing_logical_document(
    content_version: int, index_generation: int,
) -> None:
    plan = plan_chunk_index(
        _source(content_version=content_version), _contract(index_generation=index_generation),
    )
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

    assert semantic.request is plan.semantic_request
    assert vision.request is plan.vision_request
    for artifact in (semantic, vision):
        for field in ("input_sha256", "encoder_id", "content_version", "index_generation"):
            assert not hasattr(artifact, field)
    assert document.projection == plan.projection
    assert document.semantic_vector == (1.0, 0.0)
    assert document.vision_vector == (0.0, 1.0)
    assert document.retrieval_identity.model_dump() == {
        "workspace_id": "workspace-1",
        "series_id": "series-1",
        "video_id": "video-1",
        "parse_task_id": "task-1",
        "content_version": content_version,
        "index_generation": index_generation,
    }
    assert document.receipt.as_dict() == {
        "parse_task_id": "task-1",
        "video_chunk_id": "chunk-1",
        "content_version": content_version,
        "index_generation": index_generation,
    }
    for field in (
        "source_revision", "projection_revision", "retrieval_contract_id",
        "semantic_encoder_id", "vision_encoder_id", "semantic_input_sha256",
        "vision_input_sha256", "sparse_encoder_id", "sparse_input_sha256",
    ):
        assert not hasattr(document, field)
        assert not hasattr(document.receipt, field)
    assert not hasattr(document, "document_id")
    assert not hasattr(document, "external_version")


@pytest.mark.parametrize("field", ["lexical", "semantic"])
def test_finalization_rejects_projection_changes_after_planning(field: str) -> None:
    plan = plan_chunk_index(_source(), _contract())
    artifacts = _dense_artifacts(plan)
    if field == "lexical":
        plan.projection.lexical_fields[0].raw_text = "千早爱音离开"
    else:
        plan.projection.semantic_text = "不同的索引文字"
    with pytest.raises(ValueError, match="index projection changed after planning"):
        finalize_chunk_index(plan, artifacts)


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


@pytest.mark.parametrize("change", ["channel", "request", "vector"])
def test_artifact_must_match_request_channel_and_vector_dimension(change: str) -> None:
    plan = plan_chunk_index(_source(), _contract())
    request = plan.vision_request
    valid = EmbeddingArtifact(request=request, vector=(0.0, 1.0))
    assert validate_embedding_artifact(request, valid) == (0.0, 1.0)

    if change == "channel":
        invalid = replace(valid, request=plan.semantic_request)
        message = "channel"
    elif change == "request":
        invalid = replace(valid, request=replace(request))
        message = "different plan request"
    else:
        invalid = replace(valid, vector=(1.0,))
        message = "dimensions"
    with pytest.raises(ValueError, match=message):
        validate_embedding_artifact(request, invalid)
    with pytest.raises(ValueError, match="duplicate" if change == "channel" else message):
        finalize_chunk_index(plan, [_dense_artifacts(plan)[0], invalid])


@pytest.mark.parametrize("channel", ["semantic", "vision"])
@pytest.mark.parametrize("change", [
    "same_input", "version", "generation", "replace_version", "replace_generation",
])
def test_dense_results_cannot_be_injected_into_another_plan(channel: str, change: str) -> None:
    old = plan_chunk_index(_source(), _contract())
    new = _another_plan(old, change)
    old_request = getattr(old, f"{channel}_request")
    new_request = getattr(new, f"{channel}_request")
    assert old_request == new_request
    assert old_request is not new_request
    old_result = next(item for item in _dense_artifacts(old) if item.channel == channel)
    results = [old_result if item.channel == channel else item for item in _dense_artifacts(new)]

    with pytest.raises(ValueError, match="different plan request"):
        finalize_chunk_index(new, results)

    document = finalize_chunk_index(new, _dense_artifacts(new))
    assert (document.content_version, document.index_generation) == (
        new.content_version, new.index_generation,
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
    assert document.semantic_vector is None


def test_sparse_is_opt_in_and_persists_versioned_identity() -> None:
    disabled = plan_chunk_index(_source(), _contract())
    disabled_document = finalize_chunk_index(disabled, _dense_artifacts(disabled))
    assert disabled.sparse_request is None and disabled.sparse_encoder is None
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
    assert document.receipt.as_dict() == disabled_document.receipt.as_dict()

    ranked = VersionedRankedVideoChunkMatch(
        rank=1, video_chunk_id="chunk-1", score=0.9,
        retrieval_identity=document.retrieval_identity,
    )
    for by_alias in (False, True):
        wire = ranked.model_dump(mode="json", by_alias=by_alias)
        assert VersionedRankedVideoChunkMatch.model_validate(wire) == ranked
    changed_identity = document.retrieval_identity.model_copy(
        update={"content_version": plan.content_version + 1},
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
    assert old.sparse_request.text == new.sparse_request.text
    assert old.sparse_request.encoder == new.sparse_request.encoder


def test_sparse_results_must_match_the_plan_request_and_model_contract() -> None:
    plan = _sparse_plan()
    request = plan.sparse_request
    assert request is not None
    valid = accept_sparse_result(request, model=request.encoder.model, weights={"scene": 1})
    dense = _dense_artifacts(plan)

    with pytest.raises(ValueError, match="required"):
        finalize_chunk_index(plan, dense)
    with pytest.raises(ValueError, match="different plan request"):
        finalize_chunk_index(plan, dense, sparse_artifact=replace(valid, request=replace(request)))
    disabled = plan_chunk_index(_source(), _contract())
    with pytest.raises(ValueError, match="not requested"):
        finalize_chunk_index(disabled, _dense_artifacts(disabled), sparse_artifact=valid)
    with pytest.raises(ValueError, match="model"):
        accept_sparse_result(request, model="other", weights={"scene": 1})
    with pytest.raises(ValueError, match="duplicate"):
        SparseEmbeddingArtifact(
            request=request, token_weights=(("scene", 1.0), ("scene", 2.0)),
        )

    other = _sparse_plan(result=_result("其实我很难过"))
    for forged in (
        replace(plan, sparse_request=other.sparse_request),
        replace(plan, sparse_encoder=replace(request.encoder, model="sparse-v2")),
        replace(plan, sparse_request=None),
    ):
        with pytest.raises(ValueError, match="sparse request"):
            finalize_chunk_index(forged, _dense_artifacts(forged), sparse_artifact=valid)


@pytest.mark.parametrize("change", [
    "same_input", "version", "generation", "replace_version", "replace_generation",
])
def test_sparse_results_cannot_be_injected_into_another_plan(change: str) -> None:
    old = _sparse_plan()
    new = _another_plan(old, change)
    assert old.sparse_request == new.sparse_request
    assert old.sparse_request is not new.sparse_request
    old_result = accept_sparse_result(
        old.sparse_request, model=old.sparse_request.encoder.model, weights={"scene": 1},
    )
    with pytest.raises(ValueError, match="different plan request"):
        finalize_chunk_index(new, _dense_artifacts(new), sparse_artifact=old_result)

    result = accept_sparse_result(
        new.sparse_request, model=new.sparse_request.encoder.model, weights={"scene": 1},
    )
    document = finalize_chunk_index(new, _dense_artifacts(new), sparse_artifact=result)
    assert document.sparse_token_weights == (("scene", 1.0),)


def test_sparse_empty_activation_is_valid_without_a_separate_identity() -> None:
    empty = _sparse_plan(result=VideoParseResult(persons=[], timeline=[]))
    assert empty.sparse_encoder is not None and empty.sparse_request is None
    empty_document = finalize_chunk_index(empty, _dense_artifacts(empty))
    assert empty_document.sparse_token_weights is None

    plan = _sparse_plan()
    request = plan.sparse_request
    assert request is not None
    artifact = accept_sparse_result(
        request, model=request.encoder.model, weights={"scene": 0},
    )
    document = finalize_chunk_index(
        plan, _dense_artifacts(plan), sparse_artifact=artifact,
    )
    assert artifact.token_weights == document.sparse_token_weights == ()
    assert artifact.request is request
    for value in (request, artifact):
        for field in ("input_sha256", "encoder_id", "content_version", "index_generation"):
            assert not hasattr(value, field)
