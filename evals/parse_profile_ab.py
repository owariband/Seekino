"""Pure request variants and analysis for the parse-profile 2x2 replay experiment.

The experiment replays one saved DeepSeek Understanding request under four
combinations of Event enum subset and reasoning effort. A variant may differ
only on three surfaces: the timeline union inside ``text.format.schema``, the
Event sentence in the main prompt, and ``reasoning.effort``. This module has no
CLI, network or file I/O; the runner owns loading requests and model calls.

See docs/design/03-product-reassessment/parse-event-profile-trd.md sections 2-4.
"""
from __future__ import annotations

from copy import deepcopy
from decimal import Decimal
from typing import Any, Literal

FULL_EVENT_SENTENCE = (
    "Combine dialogue with significant scene changes, actions, observable emotions and supported relationship changes. Use SCENE, ACTION, EMOTION, RELATION_CHANGE, ON_SCREEN_TEXT or OTHER as appropriate; do not force every event type into every chunk or repeat one sustained event for every frame."
)
COURSE_EVENT_SENTENCE = (
    "Combine dialogue with SCENE summaries of what is being taught or demonstrated in each span, including code being written, run outputs and diagrams, and use ON_SCREEN_TEXT only for non-dialogue text such as slide titles. Use only SCENE, DIALOGUE and ON_SCREEN_TEXT; do not repeat one sustained scene for every frame."
)

EVENT_TYPES = (
    "SCENE", "ACTION", "DIALOGUE", "EMOTION",
    "RELATION_CHANGE", "ON_SCREEN_TEXT", "OTHER",
)
COURSE_EVENT_TYPES = ("SCENE", "DIALOGUE", "ON_SCREEN_TEXT")

# DeepSeek Flash list prices in CNY per million tokens; peak doubles every rate.
_INPUT_CNY_PER_MILLION = Decimal("1")
_CACHED_INPUT_CNY_PER_MILLION = Decimal("0.02")
_OUTPUT_CNY_PER_MILLION = Decimal("4")
_TOKENS_PER_MILLION = Decimal(1_000_000)


def build_variant(
    request: dict,
    *,
    event_profile: Literal["full", "course"],
    reasoning_effort: Literal["none", "low"],
) -> dict:
    """Return a deep copy of one saved request for a single 2x2 variant."""
    if event_profile not in {"full", "course"}:
        raise ValueError(f"unsupported event profile: {event_profile!r}")
    if reasoning_effort not in {"none", "low"}:
        raise ValueError(f"unsupported reasoning effort: {reasoning_effort!r}")

    variant = deepcopy(request)
    union = _timeline_union(variant)
    prompt_index = _main_prompt_index(variant)
    prompt = variant["input"][0]["content"][prompt_index]["text"]
    if not isinstance(prompt, str) or prompt.count(FULL_EVENT_SENTENCE) != 1:
        raise ValueError("the full Event sentence must appear exactly once in the main prompt")

    variant["reasoning"] = {"effort": reasoning_effort}
    if event_profile == "course":
        variant["input"][0]["content"][prompt_index]["text"] = prompt.replace(
            FULL_EVENT_SENTENCE, COURSE_EVENT_SENTENCE,
        )
        variant["text"]["format"]["schema"]["properties"]["timeline"]["items"]["anyOf"] = [
            branch for branch in union
            if branch["properties"]["event_type"]["const"] in COURSE_EVENT_TYPES
        ]
    return variant


def _timeline_union(request: dict) -> list[dict]:
    """Validate and return the seven-branch timeline ``anyOf`` union."""
    try:
        union = request["text"]["format"]["schema"]["properties"]["timeline"]["items"]["anyOf"]
    except (KeyError, TypeError) as error:
        raise ValueError("request must hold a timeline items anyOf union") from error
    if not isinstance(union, list) or len(union) != len(EVENT_TYPES):
        raise ValueError(
            f"timeline union must contain exactly {len(EVENT_TYPES)} event types"
        )
    event_types = []
    for branch in union:
        properties = branch.get("properties") if isinstance(branch, dict) else None
        event_type = None
        if isinstance(properties, dict) and isinstance(properties.get("event_type"), dict):
            event_type = properties["event_type"].get("const")
        if not isinstance(event_type, str):
            raise ValueError(
                "every timeline union branch must declare properties.event_type.const"
            )
        event_types.append(event_type)
    if set(event_types) != set(EVENT_TYPES):
        raise ValueError(
            f"timeline union must contain exactly the expected {len(EVENT_TYPES)} "
            f"event types; got {event_types}"
        )
    return union


def _main_prompt_index(request: dict) -> int:
    """Find the sole ``input_text`` block that is not a ``timestamp_ms=`` label."""
    try:
        content = request["input"][0]["content"]
    except (KeyError, IndexError, TypeError) as error:
        raise ValueError("request must contain input[0].content blocks") from error
    if not isinstance(content, list):
        raise ValueError("request must contain input[0].content blocks")
    indices = [
        index for index, block in enumerate(content)
        if isinstance(block, dict)
        and block.get("type") == "input_text"
        and not str(block.get("text", "")).startswith("timestamp_ms=")
    ]
    if len(indices) != 1:
        raise ValueError(
            "request must contain exactly one non-timestamp input_text prompt block"
        )
    return indices[0]


def variant_diff_paths(a: dict, b: dict) -> list[str]:
    """List JSON-path-like leaf locations where two request trees differ.

    Object keys use dots and arrays use ``[index]``; a changed array length is
    reported once at the array path, for example ``input[0].content[4].text``.
    """
    paths: list[str] = []
    _collect_diffs(a, b, "", paths)
    return paths


def _collect_diffs(a: Any, b: Any, path: str, paths: list[str]) -> None:
    if isinstance(a, dict) and isinstance(b, dict):
        keys = list(a)
        keys.extend(key for key in b if key not in a)
        for key in keys:
            child = f"{path}.{key}" if path else key
            if key not in a or key not in b:
                paths.append(child)
            else:
                _collect_diffs(a[key], b[key], child, paths)
        return
    if isinstance(a, list) and isinstance(b, list):
        if len(a) != len(b):
            paths.append(path or "$")
            return
        for index, (left, right) in enumerate(zip(a, b, strict=True)):
            _collect_diffs(left, right, f"{path}[{index}]", paths)
        return
    if type(a) is not type(b) or a != b:
        paths.append(path or "$")


def estimate_cost_cny(usage: dict, band: Literal["base", "peak"]) -> Decimal:
    """Price one DeepSeek Flash usage record; reasoning tokens are output tokens."""
    if band not in {"base", "peak"}:
        raise ValueError(f"unsupported rate band: {band!r}")
    input_tokens = int(usage["input_tokens"])
    cached_tokens = int((usage.get("input_tokens_details") or {}).get("cached_tokens") or 0)
    output_tokens = int(usage["output_tokens"])
    if min(input_tokens, cached_tokens, output_tokens) < 0 or cached_tokens > input_tokens:
        raise ValueError("usage token counts must be non-negative and cached input cannot exceed input")

    multiplier = Decimal("2") if band == "peak" else Decimal("1")
    cost = (
        (input_tokens - cached_tokens) * _INPUT_CNY_PER_MILLION
        + cached_tokens * _CACHED_INPUT_CNY_PER_MILLION
        + output_tokens * _OUTPUT_CNY_PER_MILLION
    ) * multiplier / _TOKENS_PER_MILLION
    return cost.normalize()


def dialogue_coverage(result: dict, asr_cues: list[dict]) -> float:
    """Fraction of ASR cues overlapping a DIALOGUE event under half-open intervals."""
    if not asr_cues:
        return 1.0
    dialogue_bounds = [
        (event["start_seconds"], event["end_seconds"])
        for event in result.get("timeline", [])
        if event.get("event_type") == "DIALOGUE"
    ]
    covered = 0
    for cue in asr_cues:
        cue_bounds = (cue["start_seconds"], cue["end_seconds"])
        if any(_overlaps(cue_bounds, bounds) for bounds in dialogue_bounds):
            covered += 1
    return covered / len(asr_cues)


def _overlaps(a: tuple[int, int], b: tuple[int, int]) -> bool:
    return a[0] < b[1] and b[0] < a[1]


def summarize(rows: list[dict]) -> dict:
    """Aggregate replay rows by variant; mean cost remains a decimal string."""
    grouped: dict[str, list[dict]] = {}
    for row in rows:
        grouped.setdefault(row["variant"], []).append(row)

    summary = {}
    for variant, items in grouped.items():
        n = len(items)
        total_cost = sum((Decimal(str(item["cost_cny"])) for item in items), Decimal("0"))
        summary[variant] = {
            "n": n,
            "mean_cost_cny": _decimal_text(total_cost / n),
            "mean_reasoning_tokens": sum(item["reasoning_tokens"] for item in items) / n,
            "mean_output_tokens": sum(item["output_tokens"] for item in items) / n,
            "schema_pass_rate": sum(1 for item in items if item["schema_ok"]) / n,
            "mean_dialogue_coverage": sum(item["dialogue_coverage"] for item in items) / n,
        }
    return summary


def _decimal_text(value: Decimal) -> str:
    return format(value.normalize(), "f")
