from __future__ import annotations

import math

import pytest
from pydantic import ValidationError

from engine.rag.encoding import EncoderContract
from engine.rag.vision.event_frames import (
    CaptureFrame, EventFrameContract, accept_event_frame_embedding,
    aggregate_event_frame_embeddings, plan_event_frame_embeddings,
)


def _requests():
    contract = EventFrameContract(max_height=720)
    frames = [CaptureFrame(requested_at_ms=at, image_sha256=letter * 64)
              for at, letter in zip(contract.timestamps(1000, 5000), "abc", strict=True)]
    return contract, plan_event_frame_embeddings(EncoderContract("test", "image", "v1", 2), frames, contract)


def test_capture_defaults_and_midpoints_preserve_integer_source_times():
    contract = EventFrameContract(max_height=720)
    assert contract.timestamps(1000, 5000) == (1666, 3000, 4333)
    assert contract.timestamps(54950, 54951) == (54950,)
    assert contract.timestamps(1000, 1100) == (1050,)


def test_image_protocol_and_l2_mean_do_not_use_old_video_contract():
    contract, requests = _requests()
    artifacts = [accept_event_frame_embedding(request, modality="image", model="image", vector=vector)
                 for request, vector in zip(requests, ([2., 0.], [0., 7.], [4., 0.]), strict=True)]
    assert aggregate_event_frame_embeddings(requests, artifacts, contract) == pytest.approx((2 / math.sqrt(5), 1 / math.sqrt(5)))
    with pytest.raises(ValueError, match="image embedding"):
        accept_event_frame_embedding(requests[0], modality="video", model="image", vector=[1., 0.])
    with pytest.raises(ValueError, match="image embedding"):
        accept_event_frame_embedding(requests[0], modality="image", model="other", vector=[1., 0.])


def test_single_frame_normalizes_and_cancellation_missing_or_cross_plan_artifact_fails():
    contract, requests = _requests()
    artifact = accept_event_frame_embedding(requests[0], modality="image", model="image", vector=[3., 4.])
    assert aggregate_event_frame_embeddings(requests[:1], [artifact], contract) == pytest.approx((.6, .8))
    with pytest.raises(ValueError, match="exactly one"):
        aggregate_event_frame_embeddings(requests, [artifact], contract)
    other_contract, other = _requests()
    with pytest.raises(ValueError, match="different plan"):
        aggregate_event_frame_embeddings(other[:1], [artifact], other_contract)
    opposite = [accept_event_frame_embedding(requests[i], modality="image", model="image", vector=vector)
                for i, vector in enumerate(([1., 0.], [-1., 0.]))]
    with pytest.raises(ValueError, match="nonzero"):
        aggregate_event_frame_embeddings(requests[:2], opposite, contract)


@pytest.mark.parametrize("vector", [[0., 0.], [1.], [float("nan"), 1.], [float("inf"), 1.], [True, 1.]])
def test_image_artifacts_require_finite_numeric_nonzero_encoder_dimensions(vector):
    _contract, requests = _requests()
    with pytest.raises(ValueError):
        accept_event_frame_embedding(requests[0], modality="image", model="image", vector=vector)


def test_frame_input_and_encoder_proof_change_without_using_file_paths():
    contract, requests = _requests()
    assert requests[0].input_sha256 == "a" * 64
    assert requests[0].input_sha256 != requests[1].input_sha256
    assert requests[0].encoder_id == requests[1].encoder_id
    changed = plan_event_frame_embeddings(requests[0].encoder, [requests[0].frame], EventFrameContract(max_height=360))[0]
    assert changed.encoder_id != requests[0].encoder_id
    assert requests[0].frame.actual_at_ms is None


def test_near_cancellation_uses_stable_final_l2_and_rejects_boolean_fps():
    contract, requests = _requests()
    artifacts = [accept_event_frame_embedding(request,modality="image",model="image",vector=vector)
                 for request,vector in zip(requests[:2],([1.,1e-200],[-1.,1e-200]),strict=True)]
    assert aggregate_event_frame_embeddings(requests[:2],artifacts,contract) == pytest.approx((0.,1.))
    with pytest.raises(ValidationError):
        EventFrameContract(max_height=720,fps=True)
