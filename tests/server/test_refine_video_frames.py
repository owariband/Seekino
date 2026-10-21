from __future__ import annotations

import asyncio
from contextvars import ContextVar
from datetime import UTC, datetime, timedelta
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest

from agent.video_parse_v2.input import FallbackModelConfig, ParseInput
from agent.video_parse_v2.state import LeaseToken
from agent.video_parse_v2.timelineEvent_canvas import CanvasCompletion, ParseContextCanvas
from engine.parsev2.schemas import VideoParseResult
from gateway.config import ClaudeCodeModelSourceConfig, load_config
from gateway.media import ToolImageArtifact
from server.config.parse_profile import default_video_parse_v2_config
from server.model.dto.jobDTO import IntervalParseLease
from server.service.video_parse_persistence import VideoParseLeaseLostError
from server.service.video_parse import VideoParseRuntimeService


@pytest.fixture
def refine_frames(tmp_path, monkeypatch):
    gateway = load_config()
    config = default_video_parse_v2_config(gateway)
    understanding = config.models.understanding.model_copy(update={
        "source": "codex_local", "supports_vision_input": None,
        "fallback": FallbackModelConfig(
            source="deepseek_api", model="fallback-model", request_timeout_seconds=30.0,
            supports_vision_input=None,
        ),
    })
    config = config.model_copy(update={"models": config.models.model_copy(update={
        "understanding": understanding, "refine": None,
    })})
    inputs = ParseInput(series_id=str(uuid4()), video_id=str(uuid4()), parse_config=config)
    lease = IntervalParseLease(
        interval_parse_task_id=str(uuid4()), video_parse_task_id=str(uuid4()),
        chunk_parse_task_ids=("left-member", "right-member"), start_sequence_no=2,
        chunk_count=2, phase="REFINING", worker_id="worker", lease_version=3,
        lease_expire_time=datetime.now(UTC) + timedelta(minutes=5), retry_count=0,
        max_retry_count=0, retry_initial_delay_seconds=1.0, retry_max_delay_seconds=1.0,
    )
    bounds = {"left-member": (60_000, 180_000), "right-member": (180_000, 240_000)}
    snapshot = {
        "inputs": inputs, "interval_parse_task_id": lease.interval_parse_task_id,
        "video_parse_task_id": lease.video_parse_task_id,
        "lease": LeaseToken(owner=lease.worker_id, version=lease.lease_version,
                            expires_at=lease.lease_expire_time),
        "member_bounds": bounds, "write_bounds": (120_000, 240_000),
    }
    canvas = ParseContextCanvas(scope="refine:frames", members={
        member: {"sequence_no": index + 2, "version": 1, "bounds": limits,
                 "content": VideoParseResult(persons=[], timeline=[])}
        for index, (member, limits) in enumerate(bounds.items())
    })
    source_path = tmp_path / "original.mp4"
    source_path.write_bytes(b"host-selected-video")
    source = SimpleNamespace(
        series_id=inputs.series_id, task=SimpleNamespace(video_id=inputs.video_id),
        original_source_uri="artifact://source/original.mp4", duration_seconds=300_000,
        content_hash="a" * 64,
    )
    runtime = object.__new__(VideoParseRuntimeService)
    runtime._gateway_config = gateway
    runtime._persistence = SimpleNamespace(load_task_input=Mock(return_value=source))
    runtime._artifact_store = SimpleNamespace(resolve=Mock(return_value=source_path))
    runtime._interval_lease = ContextVar("refine-frame-lease", default=lease)
    manager = SimpleNamespace(check_active=Mock())
    runtime._model_manager = ContextVar("refine-frame-manager", default=manager)
    generated = AsyncMock(return_value=CanvasCompletion(complete=True))
    monkeypatch.setattr(runtime, "_generate_text_structured", generated)
    extracted = []

    def extract(path, timestamps, *, output_dir, max_height, timeout_seconds):
        assert path == source_path
        assert max_height == config.visual_input.max_height
        assert 0 < timeout_seconds <= understanding.request_timeout_seconds
        output_dir.mkdir(parents=True)
        image = output_dir / "sample.jpg"
        image.write_bytes(b"\xff\xd8\xff" + str(timestamps[0]).encode())
        extracted.append((path, list(timestamps), image))
        return [(timestamps[0], image)]

    monkeypatch.setattr("agent.utils.video_frames.extract_video_frames", extract)
    return SimpleNamespace(runtime=runtime, snapshot=snapshot, canvas=canvas, generated=generated,
                           manager=manager, source=source, source_path=source_path, extracted=extracted)


def _run(case):
    return asyncio.run(case.runtime.generate_interval_refinement(
        case.snapshot, prompt="Review the interval", canvas=case.canvas,
    ))


@pytest.mark.parametrize("vision", [None, True])
def test_refine_supplies_images_from_bound_source_and_expanded_read_scope(refine_frames, vision):
    case = refine_frames
    config = case.snapshot["inputs"].parse_config
    config = config.model_copy(update={"models": config.models.model_copy(update={
        "understanding": config.models.understanding.model_copy(update={"supports_vision_input": vision}),
    })})
    case.snapshot["inputs"] = case.snapshot["inputs"].model_copy(update={"parse_config": config})
    frozen = config.model_dump(mode="json")
    supplied = []

    async def generate(binding, prompt, **kwargs):
        assert binding.supports_vision_input is True
        assert binding.fallback.supports_vision_input is True
        assert kwargs["canvas"] is case.canvas
        assert kwargs["tool_harness"] == "deepagents"
        assert kwargs["result_type"] is CanvasCompletion
        tools = {tool.name: tool for tool in kwargs["tools"]}
        assert set(tools) == {"get_context_canvas", "write_context_canvas", "inspect_video_frames"}
        # The left context is readable even though it precedes the write interval.
        supplied.append(tools["inspect_video_frames"].invoke({
            "type": "tool_call", "id": "frame-review", "name": "inspect_video_frames",
            "args": {"start_ms": 61_000, "end_ms": 62_000, "frame_count": 1},
        }))
        return CanvasCompletion(complete=True)

    case.generated.side_effect = generate
    assert _run(case).complete
    result = supplied[0]
    assert result.status == "success"
    assert isinstance(result.artifact, ToolImageArtifact)
    assert len(result.artifact.images) == 1
    assert result.artifact.images[0].data_url.startswith("data:image/jpeg;base64,")
    payload = json.loads(result.content)
    assert payload["frames"][0]["requested_timestamp_ms"] == 61_500
    assert payload["video_id"] == case.snapshot["inputs"].video_id
    assert payload["content_hash"] == case.source.content_hash
    assert case.extracted[0][:2] == (case.source_path, [61_500])
    assert all(not path.exists() for _, _, path in case.extracted)
    case.runtime._persistence.load_task_input.assert_called_once_with(case.snapshot["video_parse_task_id"])
    case.runtime._artifact_store.resolve.assert_called_once_with(case.source.original_source_uri)
    assert case.manager.check_active.call_count >= 3
    assert config.model_dump(mode="json") == frozen


@pytest.mark.parametrize("disabled", ["binding", "adapter", "fallback_binding", "fallback_adapter"])
def test_refine_without_visual_support_skips_tool_and_source_lookup(refine_frames, disabled):
    case = refine_frames
    if disabled in {"binding", "fallback_binding"}:
        inputs = case.snapshot["inputs"]
        config = inputs.parse_config
        binding = config.models.understanding
        replacement = (binding.model_copy(update={"supports_vision_input": False})
                       if disabled == "binding" else binding.model_copy(update={
                           "fallback": binding.fallback.model_copy(update={"supports_vision_input": False}),
                       }))
        config = config.model_copy(update={"models": config.models.model_copy(update={
            "understanding": replacement,
        })})
        case.snapshot["inputs"] = inputs.model_copy(update={"parse_config": config})
    else:
        gateway = case.runtime._gateway_config
        source = "codex_local" if disabled == "adapter" else "deepseek_api"
        case.runtime._gateway_config = gateway.model_copy(update={"model_sources": {
            **gateway.model_sources, source: ClaudeCodeModelSourceConfig(),
        }})
    binding = case.snapshot["inputs"].parse_config.models.understanding
    frozen = case.snapshot["inputs"].parse_config.model_dump(mode="json")
    assert _run(case).complete
    actual_binding = case.generated.await_args.args[0]
    assert actual_binding is binding
    assert {tool.name for tool in case.generated.await_args.kwargs["tools"]} == {
        "get_context_canvas", "write_context_canvas",
    }
    case.runtime._persistence.load_task_input.assert_not_called()
    case.runtime._artifact_store.resolve.assert_not_called()
    assert not case.extracted
    assert case.snapshot["inputs"].parse_config.model_dump(mode="json") == frozen


@pytest.mark.parametrize("mismatch", ["series", "video", "lease"])
def test_refine_rejects_source_or_lease_mismatch_before_model_call(refine_frames, mismatch):
    case = refine_frames
    if mismatch == "series":
        case.source.series_id = "another-series"
    elif mismatch == "video":
        case.source.task.video_id = "another-video"
    else:
        current = case.snapshot["lease"]
        case.snapshot["lease"] = LeaseToken(owner=current.owner, version=current.version + 1,
                                           expires_at=current.expires_at)
    with pytest.raises(VideoParseLeaseLostError):
        _run(case)
    case.generated.assert_not_awaited()
    case.runtime._artifact_store.resolve.assert_not_called()
    assert not case.extracted


def test_refine_frame_tool_rejects_outside_scope_and_model_selected_source(refine_frames):
    case = refine_frames

    async def generate(binding, prompt, **kwargs):
        tool = next(tool for tool in kwargs["tools"] if tool.name == "inspect_video_frames")
        outside = tool.invoke({"start_ms": 59_000, "end_ms": 61_000, "frame_count": 1})
        assert json.loads(outside)["error"]["code"] == "INVALID_FRAME_RANGE"
        untrusted = tool.invoke({
            "start_ms": 61_000, "end_ms": 62_000, "frame_count": 1,
            "source_path": "/untrusted/source.mp4",
        })
        assert json.loads(untrusted)["error"]["code"] == "INVALID_ARGUMENTS"
        return CanvasCompletion(complete=True)

    case.generated.side_effect = generate
    assert _run(case).complete
    assert not case.extracted
