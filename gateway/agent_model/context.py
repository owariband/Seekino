from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import dataclass, field, replace
from math import ceil
from typing import Any, Callable, Literal
from uuid import uuid4


# These constants describe this module's wire representation. They are deliberately
# local: no other subsystem should depend on the shape of a compacted message.
_BYTES_PER_ESTIMATED_TOKEN = 3
_MAX_CHECKPOINT_CHARACTERS = 12_000
_MAX_ACTIVITY_SUMMARY_CHARACTERS = 8_000
_MAX_SUMMARY_LINE_CHARACTERS = 500
_RECENT_NON_TOOL_MESSAGES = 4
_COMPACTION_MARKER = "anivag_context_checkpoint"

ContextCheckpoint = Callable[[], object]
OpenAIMessage = dict[str, Any]


class ContextWindowOverflow(RuntimeError):
    """A request cannot fit within its configured context budget."""


@dataclass(frozen=True, slots=True)
class ContextWindowPolicy:
    """Token boundaries for one model, including output and safety reserves."""

    context_window_tokens: int
    output_reserve_tokens: int
    safety_margin_tokens: int
    auto_compact_tokens: int
    compact_target_tokens: int

    def __post_init__(self) -> None:
        for name in (
            "context_window_tokens",
            "output_reserve_tokens",
            "safety_margin_tokens",
            "auto_compact_tokens",
            "compact_target_tokens",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{name} must be a positive integer")
        if self.maximum_input_tokens <= 0:
            raise ValueError(
                "context_window_tokens must exceed output and safety reserves"
            )
        if self.auto_compact_tokens > self.maximum_input_tokens:
            raise ValueError(
                "auto_compact_tokens must not exceed the maximum input budget"
            )
        if self.compact_target_tokens >= self.auto_compact_tokens:
            raise ValueError(
                "compact_target_tokens must be lower than auto_compact_tokens"
            )

    @property
    def maximum_input_tokens(self) -> int:
        return (
            self.context_window_tokens
            - self.output_reserve_tokens
            - self.safety_margin_tokens
        )


@dataclass(slots=True)
class ContextWindowState:
    """Identity and counters for replacement windows within one Agent invocation."""

    window_number: int = 1
    current_window_id: str = field(default_factory=lambda: str(uuid4()))
    previous_window_id: str | None = None
    compaction_count: int = 0

    def advance(self, next_window_id: str) -> None:
        self.previous_window_id = self.current_window_id
        self.current_window_id = next_window_id
        self.window_number += 1
        self.compaction_count += 1


@dataclass(frozen=True, slots=True)
class ContextWindowMeasurement:
    characters: int
    estimated_tokens: int


@dataclass(frozen=True, slots=True)
class ContextWindowUpdate:
    messages: list[OpenAIMessage]
    compacted: bool
    reason: Literal["auto", "hard_limit"] | None
    before: ContextWindowMeasurement
    after: ContextWindowMeasurement
    window_number: int
    window_id: str
    previous_window_id: str | None
    cleared_tool_results: int = 0


class ContextWindowController:
    """Build a bounded replacement history without owning canonical Agent state."""

    def __init__(
        self,
        *,
        policy: ContextWindowPolicy | None,
        maximum_input_characters: int | None,
        label: str,
    ) -> None:
        if maximum_input_characters is not None and (
            isinstance(maximum_input_characters, bool)
            or not isinstance(maximum_input_characters, int)
            or maximum_input_characters <= 0
        ):
            raise ValueError(
                "maximum_input_characters must be positive when provided"
            )
        if not label.strip():
            raise ValueError("context-window label must not be empty")
        self.policy = policy
        self.maximum_input_characters = maximum_input_characters
        self.label = label
        self.state = ContextWindowState()
        self._reported_input_tokens: int | None = None
        self._reported_request_estimated_tokens: int | None = None

    def record_provider_input_tokens(
        self,
        input_tokens: int | None,
        *,
        request_estimated_tokens: int,
    ) -> None:
        if input_tokens is None:
            return
        if (
            isinstance(input_tokens, bool)
            or not isinstance(input_tokens, int)
            or input_tokens < 0
        ):
            return
        self._reported_input_tokens = input_tokens
        self._reported_request_estimated_tokens = request_estimated_tokens

    def prepare(
        self,
        messages: list[OpenAIMessage],
        *,
        contract: object,
        checkpoint: ContextCheckpoint | None = None,
    ) -> ContextWindowUpdate:
        candidate = deepcopy(messages)
        before = self._project_active_tokens(measure_context(candidate, contract))
        hard_limit_reached = self._hard_limit_reached(before)
        auto_limit_reached = (
            self.policy is not None
            and before.estimated_tokens >= self.policy.auto_compact_tokens
        )
        if not hard_limit_reached and not auto_limit_reached:
            return self._unchanged(candidate, before)

        reason: Literal["auto", "hard_limit"] = (
            "hard_limit" if hard_limit_reached else "auto"
        )
        stable_end = _stable_prefix_end(candidate)
        recent_start = _recent_history_start(candidate, stable_end)
        compactable = candidate[stable_end:recent_start]
        recent = candidate[recent_start:]
        next_window_id = str(uuid4())
        next_window_number = self.state.window_number + 1
        rendered_checkpoint = _render_checkpoint(checkpoint)
        summary = _summarize_activity(compactable)
        changed = bool(compactable)

        if changed:
            candidate = [
                *candidate[:stable_end],
                _checkpoint_message(
                    summary=summary,
                    checkpoint=rendered_checkpoint,
                    window_number=next_window_number,
                    window_id=next_window_id,
                    previous_window_id=self.state.current_window_id,
                ),
                *recent,
            ]
            if (
                self.policy is not None
                and self._project_active_tokens(
                    measure_context(candidate, contract)
                ).estimated_tokens
                > self.policy.compact_target_tokens
            ):
                candidate[stable_end] = _checkpoint_message(
                    summary=_clip(summary, 1_000),
                    checkpoint=rendered_checkpoint,
                    window_number=next_window_number,
                    window_id=next_window_id,
                    previous_window_id=self.state.current_window_id,
                )

        cleared_tool_results = 0
        if self._hard_limit_reached(
            self._project_active_tokens(measure_context(candidate, contract))
        ):
            if not changed:
                candidate = [
                    *candidate[:recent_start],
                    _checkpoint_message(
                        summary="No older completed activity was available to compact.",
                        checkpoint=rendered_checkpoint,
                        window_number=next_window_number,
                        window_id=next_window_id,
                        previous_window_id=self.state.current_window_id,
                    ),
                    *recent,
                ]
                changed = True
            candidate, cleared_tool_results = self._clear_recent_tool_results(
                candidate,
                contract=contract,
            )

        after = self._project_active_tokens(measure_context(candidate, contract))
        if self._hard_limit_reached(after):
            candidate = _shrink_checkpoint_messages(candidate)
            after = self._project_active_tokens(
                measure_context(candidate, contract)
            )
        if self._hard_limit_reached(after):
            raise ContextWindowOverflow(self._overflow_message(after))

        if not changed:
            # A soft trigger may fire while the only sizeable data is the newest Tool
            # batch. It must survive one inference step before becoming compactable.
            return self._unchanged(candidate, before)

        self.state.advance(next_window_id)
        return ContextWindowUpdate(
            messages=candidate,
            compacted=True,
            reason=reason,
            before=before,
            after=after,
            window_number=self.state.window_number,
            window_id=self.state.current_window_id,
            previous_window_id=self.state.previous_window_id,
            cleared_tool_results=cleared_tool_results,
        )

    def _project_active_tokens(
        self,
        measurement: ContextWindowMeasurement,
    ) -> ContextWindowMeasurement:
        if (
            self._reported_input_tokens is None
            or self._reported_request_estimated_tokens is None
        ):
            return measurement
        projected = max(
            0,
            self._reported_input_tokens
            + measurement.estimated_tokens
            - self._reported_request_estimated_tokens,
        )
        return replace(
            measurement,
            estimated_tokens=max(measurement.estimated_tokens, projected),
        )

    def _clear_recent_tool_results(
        self,
        messages: list[OpenAIMessage],
        *,
        contract: object,
    ) -> tuple[list[OpenAIMessage], int]:
        tool_names = _tool_names_by_call_id(messages)
        indexes = [
            index
            for index, message in enumerate(messages)
            if message.get("role") == "tool"
            and not _is_compacted_tool_result(message.get("content"))
        ]
        indexes.sort(
            key=lambda index: _serialized_character_count(messages[index]),
            reverse=True,
        )
        cleared = 0
        for index in indexes:
            measurement = self._project_active_tokens(
                measure_context(messages, contract)
            )
            if not self._hard_limit_reached(measurement):
                break
            message = messages[index]
            tool_call_id = str(message.get("tool_call_id") or "")
            original_bytes = len(
                _serialized_text(message.get("content")).encode("utf-8")
            )
            message["content"] = json.dumps(
                {
                    "context_window": "tool_result_released",
                    "tool": tool_names.get(tool_call_id),
                    "tool_call_id": tool_call_id,
                    "original_bytes": original_bytes,
                    "recoverable": True,
                    "instruction": (
                        "Canonical state remains outside message history. Use the "
                        "registered Catalog/Read Tools to inspect it. Do not repeat "
                        "a mutation solely because this result was released."
                    ),
                },
                ensure_ascii=False,
                separators=(",", ":"),
            )
            cleared += 1
        return messages, cleared

    def _hard_limit_reached(self, measurement: ContextWindowMeasurement) -> bool:
        if (
            self.maximum_input_characters is not None
            and measurement.characters > self.maximum_input_characters
        ):
            return True
        return (
            self.policy is not None
            and measurement.estimated_tokens >= self.policy.maximum_input_tokens
        )

    def _overflow_message(self, measurement: ContextWindowMeasurement) -> str:
        limits = []
        if (
            self.maximum_input_characters is not None
            and measurement.characters > self.maximum_input_characters
        ):
            limits.append(
                f"message character budget {measurement.characters}>"
                f"{self.maximum_input_characters}"
            )
        if (
            self.policy is not None
            and measurement.estimated_tokens >= self.policy.maximum_input_tokens
        ):
            limits.append(
                f"estimated token budget {measurement.estimated_tokens}>="
                f"{self.policy.maximum_input_tokens}"
            )
        return (
            f"{self.label} message context budget exceeded after compaction: "
            + ", ".join(limits)
        )

    def _unchanged(
        self,
        messages: list[OpenAIMessage],
        measurement: ContextWindowMeasurement,
    ) -> ContextWindowUpdate:
        return ContextWindowUpdate(
            messages=messages,
            compacted=False,
            reason=None,
            before=measurement,
            after=measurement,
            window_number=self.state.window_number,
            window_id=self.state.current_window_id,
            previous_window_id=self.state.previous_window_id,
        )


def measure_context(
    messages: list[OpenAIMessage],
    contract: object,
) -> ContextWindowMeasurement:
    payload = {"messages": messages, "contract": contract}
    projected, image_count = _media_budget_projection(payload)
    rendered = json.dumps(
        projected,
        ensure_ascii=False,
        separators=(",", ":"),
        default=str,
    )
    byte_count = len(rendered.encode("utf-8"))
    return ContextWindowMeasurement(
        characters=len(rendered),
        estimated_tokens=max(1, ceil(byte_count / _BYTES_PER_ESTIMATED_TOKEN)) + image_count * 2048,
    )


def preflight_context(
    messages: list[OpenAIMessage],
    contract: object,
    *,
    policy: ContextWindowPolicy | None,
    maximum_input_characters: int | None,
    fixed_input_tokens: int = 0,
    label: str,
) -> ContextWindowMeasurement:
    """Measure one initial request and reject it without attempting compaction."""

    if maximum_input_characters is not None and (
        isinstance(maximum_input_characters, bool)
        or not isinstance(maximum_input_characters, int)
        or maximum_input_characters <= 0
    ):
        raise ValueError(
            "maximum_input_characters must be positive when provided"
        )
    if (
        isinstance(fixed_input_tokens, bool)
        or not isinstance(fixed_input_tokens, int)
        or fixed_input_tokens < 0
    ):
        raise ValueError("fixed_input_tokens must be a non-negative integer")
    if not label.strip():
        raise ValueError("context-window label must not be empty")

    payload = measure_context(messages, contract)
    measurement = replace(
        payload,
        estimated_tokens=payload.estimated_tokens + fixed_input_tokens,
    )
    exceeded = []
    if (
        maximum_input_characters is not None
        and measurement.characters > maximum_input_characters
    ):
        exceeded.append(
            f"serialized character budget {measurement.characters}>"
            f"{maximum_input_characters}"
        )
    if (
        policy is not None
        and measurement.estimated_tokens >= policy.maximum_input_tokens
    ):
        exceeded.append(
            f"estimated token budget {measurement.estimated_tokens}>="
            f"{policy.maximum_input_tokens} "
            f"(payload={payload.estimated_tokens}, "
            f"fixed_input_tokens={fixed_input_tokens})"
        )
    if exceeded:
        raise ContextWindowOverflow(
            f"{label} initial context budget exceeded: " + ", ".join(exceeded)
        )
    return measurement


def _stable_prefix_end(messages: list[OpenAIMessage]) -> int:
    index = 0
    while index < len(messages) and messages[index].get("role") in {
        "system",
        "developer",
    }:
        index += 1
    if index < len(messages) and messages[index].get("role") == "user":
        return index + 1
    return min(1, len(messages))


def _recent_history_start(
    messages: list[OpenAIMessage],
    stable_end: int,
) -> int:
    for index in range(len(messages) - 1, stable_end - 1, -1):
        message = messages[index]
        if message.get("role") != "assistant" or not message.get("tool_calls"):
            continue
        following = messages[index + 1 :]
        if not following or any(item.get("role") != "tool" for item in following):
            continue
        expected = {
            str(call.get("id"))
            for call in message.get("tool_calls", [])
            if isinstance(call, dict) and call.get("id")
        }
        actual = {
            str(item.get("tool_call_id"))
            for item in following
            if item.get("tool_call_id")
        }
        if actual and actual <= expected:
            return index

    candidate = max(stable_end, len(messages) - _RECENT_NON_TOOL_MESSAGES)
    if candidate < len(messages) and messages[candidate].get("role") == "tool":
        return _matching_assistant_index(messages, candidate, stable_end)
    return candidate


def _matching_assistant_index(
    messages: list[OpenAIMessage],
    tool_index: int,
    stable_end: int,
) -> int:
    tool_call_id = messages[tool_index].get("tool_call_id")
    for index in range(tool_index - 1, stable_end - 1, -1):
        message = messages[index]
        if message.get("role") != "assistant":
            continue
        if any(
            isinstance(call, dict) and call.get("id") == tool_call_id
            for call in message.get("tool_calls", [])
        ):
            return index
    return tool_index + 1


def _summarize_activity(messages: list[OpenAIMessage]) -> str:
    if not messages:
        return "No older completed activity was available to compact."
    tool_names = _tool_names_by_call_id(messages)
    lines = []
    for message in messages:
        role = message.get("role")
        if role == "assistant":
            content = _clip(_serialized_text(message.get("content")), 300)
            calls = []
            for call in message.get("tool_calls", []):
                if not isinstance(call, dict):
                    continue
                function = call.get("function")
                if not isinstance(function, dict):
                    continue
                arguments = _clip(_serialized_text(function.get("arguments")), 240)
                calls.append(f"{function.get('name')}({arguments})")
            parts = []
            if content:
                parts.append(f"visible_text={content}")
            if calls:
                parts.append("tool_calls=" + ", ".join(calls))
            if parts:
                lines.append("assistant: " + "; ".join(parts))
        elif role == "tool":
            call_id = str(message.get("tool_call_id") or "")
            metadata = _tool_result_metadata(message.get("content"))
            lines.append(
                f"tool {tool_names.get(call_id, 'unknown')}[{call_id}]: {metadata}"
            )
        elif role in {"user", "system", "developer"}:
            content = _clip(
                _serialized_text(message.get("content")),
                _MAX_SUMMARY_LINE_CHARACTERS,
            )
            if content:
                lines.append(f"{role}: {content}")
        if sum(len(line) for line in lines) >= _MAX_ACTIVITY_SUMMARY_CHARACTERS:
            break
    return _clip("\n".join(lines), _MAX_ACTIVITY_SUMMARY_CHARACTERS)


def _tool_result_metadata(content: object) -> str:
    text = _serialized_text(content)
    try:
        value = json.loads(text)
    except (TypeError, ValueError):
        return f"completed; output_bytes={len(text.encode('utf-8'))}"
    if not isinstance(value, dict):
        return f"completed; output_bytes={len(text.encode('utf-8'))}"
    keys = (
        "accepted",
        "operation",
        "video_chunk_id",
        "draft_item_count",
        "result_count",
        "returned_count",
        "partial",
        "complete",
        "next_cursor",
    )
    metadata = {key: value[key] for key in keys if key in value}
    metadata["output_bytes"] = len(text.encode("utf-8"))
    return _clip(
        json.dumps(metadata, ensure_ascii=False, separators=(",", ":")),
        _MAX_SUMMARY_LINE_CHARACTERS,
    )


def _render_checkpoint(checkpoint: ContextCheckpoint | None) -> str:
    if checkpoint is None:
        return "No canonical workspace checkpoint was supplied."
    value = checkpoint()
    if isinstance(value, str):
        rendered = value
    else:
        rendered = json.dumps(
            value,
            ensure_ascii=False,
            separators=(",", ":"),
            default=str,
        )
    if len(rendered) <= _MAX_CHECKPOINT_CHARACTERS:
        return rendered
    prefix_limit = _MAX_CHECKPOINT_CHARACTERS - 300
    while prefix_limit > 0:
        envelope = json.dumps(
            {
                "checkpoint_truncated": True,
                "serialized_prefix": rendered[:prefix_limit],
                "omitted_characters": len(rendered) - prefix_limit,
                "recovery": "Use Catalog/Read Tools for omitted canonical state.",
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
        if len(envelope) <= _MAX_CHECKPOINT_CHARACTERS:
            return envelope
        prefix_limit -= len(envelope) - _MAX_CHECKPOINT_CHARACTERS
    return json.dumps(
        {
            "checkpoint_truncated": True,
            "recovery": "Use Catalog/Read Tools for canonical state.",
        },
        separators=(",", ":"),
    )


def _checkpoint_message(
    *,
    summary: str,
    checkpoint: str,
    window_number: int,
    window_id: str,
    previous_window_id: str,
) -> OpenAIMessage:
    return {
        "role": "user",
        "content": (
            f"<{_COMPACTION_MARKER} window_number=\"{window_number}\" "
            f"window_id=\"{window_id}\" "
            f"previous_window_id=\"{previous_window_id}\">\n"
            "The harness replaced older message history. This text is a navigation "
            "checkpoint, not primary evidence. Canonical Evidence and Draft state "
            "remain outside the prompt; use the registered Catalog/Read Tools to "
            "recover exact content. Do not repeat completed mutations unless the "
            "checkpoint says they are absent.\n\n"
            "<completed_activity>\n"
            f"{summary}\n"
            "</completed_activity>\n\n"
            "<canonical_workspace>\n"
            f"{checkpoint}\n"
            "</canonical_workspace>\n"
            f"</{_COMPACTION_MARKER}>"
        ),
    }


def _shrink_checkpoint_messages(
    messages: list[OpenAIMessage],
) -> list[OpenAIMessage]:
    for message in messages:
        if (
            message.get("role") == "user"
            and _COMPACTION_MARKER in _serialized_text(message.get("content"))
        ):
            message["content"] = (
                f"<{_COMPACTION_MARKER}>Older history and one or more Tool results "
                "were released from the active window. Canonical data is unchanged; "
                "use the registered Catalog/Read Tools to recover it."
                f"</{_COMPACTION_MARKER}>"
            )
    return messages


def _tool_names_by_call_id(messages: list[OpenAIMessage]) -> dict[str, str]:
    names = {}
    for message in messages:
        for call in message.get("tool_calls", []):
            if not isinstance(call, dict) or not call.get("id"):
                continue
            function = call.get("function")
            if isinstance(function, dict) and function.get("name"):
                names[str(call["id"])] = str(function["name"])
    return names


def _is_compacted_tool_result(content: object) -> bool:
    text = _serialized_text(content)
    return '"context_window":"tool_result_released"' in text


def _serialized_character_count(value: object) -> int:
    return len(
        json.dumps(
            _media_budget_projection(value)[0],
            ensure_ascii=False,
            separators=(",", ":"),
            default=str,
        )
    )


def _media_budget_projection(value: object) -> tuple[object, int]:
    """Budget media inputs without treating base64 as text tokens."""
    if isinstance(value, dict):
        if value.get("type") in {"image_url", "input_image"}:
            return {"type": value["type"], "image": "<image input>"}, 1
        if value.get("type") in {"video_url", "input_video"}:
            return {
                "type": value["type"], "video": "<video input>",
                **({"fps": value["fps"]} if "fps" in value else {}),
            }, 1
        projected = {}
        count = 0
        for key, item in value.items():
            projected[key], images = _media_budget_projection(item)
            count += images
        return projected, count
    if isinstance(value, list):
        projected = []
        count = 0
        for item in value:
            converted, images = _media_budget_projection(item)
            projected.append(converted)
            count += images
        return projected, count
    return value, 0


def _serialized_text(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return json.dumps(
        _media_budget_projection(value)[0],
        ensure_ascii=False,
        separators=(",", ":"),
        default=str,
    )


def _clip(value: str, maximum_characters: int) -> str:
    if len(value) <= maximum_characters:
        return value
    omitted = len(value) - maximum_characters
    suffix = f"…<{omitted} characters omitted>"
    return value[: max(0, maximum_characters - len(suffix))] + suffix
