"""Choose a Parse V2 content strategy once, before episode media preparation."""

from collections.abc import Callable, Sequence
from contextlib import AbstractContextManager
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from agent.video_parse_v2.content_route import ContentDecision, TYPE_QUESTION, routing_prompt
from agent.video_parse_v2.input import GenerativeModelConfig, RoutedContentKind
from gateway.config import ModelBinding
from gateway.model_factory import GatewayClient
from gateway.multimodal import ImageInput, TextInput
from gateway.query_hook import QueryHookModelError, QueryHookModels


@dataclass(frozen=True)
class ContentRoute:
    kind: RoutedContentKind
    basis: Literal["transcript", "sampled_frames"]
    model: str


def preview_times(duration_ms: int) -> tuple[int, ...]:
    duration = max(0, duration_ms - 1000)
    return tuple(sorted({duration * step // 10 for step in (2, 5, 8)}))


def classify_video(evidence: dict, *, gateway: GatewayClient, visual_model: GenerativeModelConfig,
                   prepare_frames: Callable[[], AbstractContextManager[Sequence[tuple[int, Path]]]],
                   check_active: Callable[[], None]) -> ContentRoute:
    """Use the full ASR when present; chunk-ASR and silent videos use a bounded visual sample."""
    check_active()
    config = gateway.config
    inputs: list[TextInput | ImageInput] = [TextInput(routing_prompt(evidence))]
    if evidence["transcript"] and config.query_hook.enabled:
        if "query_hook" in config.normalized_models:
            binding = config.resolve_model_binding("query_hook")
            kind = _ask_model(gateway, binding, inputs, check_active)
            return ContentRoute(kind, "transcript", binding.model)
        try:
            choice = QueryHookModels(config.query_hook).route(
                evidence, TYPE_QUESTION, timeout_seconds=config.query_hook.router_timeout_seconds,
                choices=tuple(TYPE_QUESTION["criteria"]), operation="video_content_route",
            )
        except QueryHookModelError:
            check_active()
        else:
            return ContentRoute(ContentDecision(content_kind=choice).content_kind, "transcript", config.query_hook.router_model)

    binding = ModelBinding.model_validate(visual_model.model_dump(exclude_none=True))
    binding = binding.model_copy(update={"kind": "generative", "supports_vision_input": True})
    with prepare_frames() as frames:
        for at_ms, path in frames:
            inputs.extend((TextInput(f"原片 {at_ms} ms"), ImageInput(path)))
        kind = _ask_model(gateway, binding, inputs, check_active)
    return ContentRoute(kind, "sampled_frames", binding.model)


def _ask_model(gateway: GatewayClient, binding: ModelBinding, inputs: Sequence[TextInput | ImageInput],
               check_active: Callable[[], None]) -> RoutedContentKind:
    # Routing is one small choice. Reuse the selected provider's endpoint and credential.
    binding = binding.model_copy(update={"fallback": None})
    model = gateway.create_generative_model(binding)
    try:
        result = model.generate(inputs, schema_name="video_content_type", result_type=ContentDecision,
                                max_model_calls=1, request_timeout_seconds=min(45, binding.request_timeout_seconds or 45),
                                check_control=check_active)
        return result.content_kind
    finally:
        model.close()
