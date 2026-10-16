from __future__ import annotations

import subprocess
import sys

import pytest
from pydantic import ValidationError

from agent.video_parse_v2.input import ParseInput


@pytest.mark.parametrize("statement", [
    "from agent.video_parse_v2.input import ParseConfig",
    "from server.config.parse_profile import ParseConfig",
])
def test_configuration_import_does_not_load_runtime(statement):
    subprocess.run([sys.executable, "-B", "-c", statement + "\n" + """
import sys
assert 'agent.video_parse_v2.runtime' not in sys.modules
assert not any(name == 'langgraph' or name.startswith('langgraph.') for name in sys.modules)
"""], check=True)


def test_public_parse_agent_remains_available_on_demand():
    subprocess.run([sys.executable, "-B", "-c", """
import sys
import agent.video_parse_v2 as package
assert 'agent.video_parse_v2.runtime' not in sys.modules
from agent.video_parse_v2 import ParseAgent
from agent.video_parse_v2.runtime import ParseAgent as RuntimeParseAgent
assert ParseAgent is RuntimeParseAgent
assert package.ParseAgent is ParseAgent
"""], check=True)


def _input_payload() -> dict:
    return {
        "series_id": "series-1",
        "video_id": "video-1",
        "parse_config": {
            "chunk_duration": 20,
            "interval_duration": 60,
            "batch_max_size": 10,
            "dispatch_interval_seconds": 0,
            "models": {
                "asr": {
                    "source": "openai-main", "model": "gpt-4o-transcribe",
                    "request_timeout_seconds": 60, "max_output_tokens": 4096,
                    "mode": "lite", "fps": 1,
                },
                "understanding": {
                    "source": "ark-main", "model": "doubao-vision",
                    "request_timeout_seconds": 120, "max_output_tokens": 8192,
                },
                "embedding": {
                    "source": "ark-embedding", "model": "multimodal-embedding",
                    "dimensions": 1024, "video_fps": 1,
                    "request_timeout_seconds": 60,
                },
            },
            "visual_input": {
                "mode": "ordered_frames",
                "fps": 1,
                "max_frames_per_chunk": 30,
                "max_height": 720,
            },
            "indexing": {"index_generation": 1, "sparse_enabled": False, "contract_fingerprint": "0" * 64},
            "execution": {
                "parse_concurrency": 10,
                "refine_concurrency": 10,
                "index_concurrency": 10,
                "parse_execution_timeout_seconds": 1800,
                "refine_execution_timeout_seconds": 1800,
                "index_execution_timeout_seconds": 1800,
                "max_retry_count": 0,
                "retry_initial_delay_seconds": 1,
                "retry_max_delay_seconds": 30,
                "max_model_calls": 100,
            },
        },
    }


def test_config_snapshot_round_trip_and_default_switches() -> None:
    inputs = ParseInput.model_validate(_input_payload())
    assert ParseInput.model_validate_json(inputs.model_dump_json()) == inputs
    assert inputs.parse_config.understanding_enabled is True
    assert inputs.parse_config.ocr_enabled is False
    assert inputs.parse_config.compact_enabled is False
    assert inputs.parse_config.prefix_context is None
    with pytest.raises(ValidationError, match="frozen"):
        inputs.parse_config.execution.max_model_calls = 200


def test_legacy_model_snapshot_does_not_add_unset_policy_fields():
    inputs = ParseInput.model_validate(_input_payload())
    rendered = inputs.model_dump(mode="json")["parse_config"]["models"]["understanding"]
    assert "context_window_tokens" not in rendered
    assert "supports_vision_input" not in rendered
    assert "max_input_chars" not in rendered


def test_historical_indexing_snapshot_remains_readable_without_inventing_generation():
    payload = _input_payload()
    payload["parse_config"]["indexing"].pop("index_generation")
    inputs = ParseInput.model_validate(payload)
    assert inputs.parse_config.indexing.index_generation is None
    assert inputs.model_dump(mode="json")["parse_config"]["indexing"] == payload["parse_config"]["indexing"]


def test_explicit_model_policy_is_frozen_and_round_trips():
    payload = _input_payload()
    policy = {
        "context_window_tokens": 128000, "context_safety_margin_tokens": 2048,
        "auto_compact_tokens": 90000, "compact_target_tokens": 60000,
        "max_input_chars": 400000, "supports_vision_input": True,
    }
    payload["parse_config"]["models"]["understanding"].update(policy)
    inputs = ParseInput.model_validate(payload)
    restored = ParseInput.model_validate_json(inputs.model_dump_json())
    model = restored.parse_config.models.understanding
    assert {name: getattr(model, name) for name in policy} == policy
    with pytest.raises(ValidationError, match="frozen"):
        model.context_window_tokens = 256000


def test_compact_requires_its_own_settings() -> None:
    payload = _input_payload()
    payload["parse_config"]["compact_enabled"] = True
    with pytest.raises(ValidationError, match="prefix_context is required"):
        ParseInput.model_validate(payload)

    payload["parse_config"]["prefix_context"] = {
        "series_compact_max_chars": 4000,
        "video_compact_max_chars": 2000,
        "max_tool_calls": 0,
    }
    payload["parse_config"]["models"]["prefix_context"] = {
        "source": "ark-main",
        "model": "doubao-agent",
        "request_timeout_seconds": 120,
        "max_output_tokens": 4096,
    }
    inputs = ParseInput.model_validate(payload)
    assert inputs.parse_config.prefix_context.max_tool_calls == 0


def test_interval_cannot_exceed_bitmap_capacity() -> None:
    payload = _input_payload()
    payload["parse_config"]["interval_duration"] = 20 * 63
    ParseInput.model_validate(payload)
    payload["parse_config"]["interval_duration"] += 1
    with pytest.raises(ValidationError, match="more than 63 chunks"):
        ParseInput.model_validate(payload)


def test_retry_maximum_cannot_be_below_initial_delay() -> None:
    payload = _input_payload()
    payload["parse_config"]["execution"]["retry_max_delay_seconds"] = 0.5
    with pytest.raises(ValidationError, match="retry_max_delay_seconds"):
        ParseInput.model_validate(payload)


@pytest.mark.parametrize("value", [0, -1, True, "100"])
def test_call_budget_requires_a_positive_integer(value: object) -> None:
    payload = _input_payload()
    payload["parse_config"]["execution"]["max_model_calls"] = value
    with pytest.raises(ValidationError):
        ParseInput.model_validate(payload)


@pytest.mark.parametrize("value", [float("inf"), float("nan")])
def test_dispatch_interval_must_be_finite(value: float) -> None:
    payload = _input_payload()
    payload["parse_config"]["dispatch_interval_seconds"] = value
    with pytest.raises(ValidationError):
        ParseInput.model_validate(payload)


def test_credentials_are_not_part_of_parse_config() -> None:
    payload = _input_payload()
    payload["parse_config"]["api_key"] = "secret"
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        ParseInput.model_validate(payload)


def test_model_snapshot_is_credential_free_and_required_for_enabled_stages() -> None:
    payload = _input_payload()
    payload["parse_config"]["models"]["asr"]["api_key"] = "secret"
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        ParseInput.model_validate(payload)

    payload = _input_payload()
    payload["parse_config"]["ocr_enabled"] = True
    with pytest.raises(ValidationError, match="models.ocr is required"):
        ParseInput.model_validate(payload)


def test_disabled_understanding_requires_a_real_deterministic_input() -> None:
    payload = _input_payload()
    payload["parse_config"]["understanding_enabled"] = False
    payload["parse_config"]["models"]["asr"]["mode"] = "none"

    with pytest.raises(
        ValidationError,
        match="understanding_enabled=false requires ASR or OCR input",
    ):
        ParseInput.model_validate(payload)

    payload["parse_config"]["models"]["asr"]["mode"] = "lite"
    parsed = ParseInput.model_validate(payload)
    assert parsed.parse_config.understanding_enabled is False
