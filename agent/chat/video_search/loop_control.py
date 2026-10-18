"""Deterministic Loop Control for the single-pass Result pipeline.

The model only reports a self-check. Whether to re-plan, ask the user or
degrade is decided here, from that report and the recorded retrieval, so a
request can never loop without a concrete gap or measurable progress.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence

from ..common.state import RunInputs
from .state import LoopOutcome, ResultAttempt, RetrievalCall

EvaluatorRisk = str

# Questions about who said a line: stills cannot settle it and voice matches are close.
_SPEAKER_QUESTION = re.compile(
    r"谁|誰|哪位|哪个(?:角色|人)|哪個(?:角色|人)|说话人|說話人|"
    r"\bwho\b|\bwhom\b|\bwhose\b|which character|\bspeaker\b",
    re.IGNORECASE,
)
# Claims that depend on what an image looks like rather than on dialogue.
_VISUAL_QUESTION = re.compile(
    r"画面|畫面|截图|截圖|图片|圖片|镜头|鏡頭|构图|構圖|长得像|長得像|相似|像不像|"
    r"\bscreenshots?\b|\bimages?\b|\bpictures?\b|\bframes?\b|\bshots?\b|looks? like|\bsimilar\b",
    re.IGNORECASE,
)


def evaluator_risks(
    inputs: RunInputs,
    draft_ids: Sequence[str],
    calls: Iterable[RetrievalCall],
    *,
    low_confidence_rank: int,
) -> tuple[EvaluatorRisk, ...]:
    """Return the high-risk reasons that require an independent Evaluator review."""

    # Judge the user's own words: a Supervisor sub-query often adds "speaker" to any quote lookup.
    question = inputs.get("original_user_message") or inputs["user_message"]
    risks: list[EvaluatorRisk] = []
    if _SPEAKER_QUESTION.search(question):
        risks.append("speaker_attribution")
    if inputs.get("image_urls") or _VISUAL_QUESTION.search(question):
        risks.append("visual_similarity")
    best_rank: dict[str, int] = {}
    for call in calls:
        if call.status == "error":
            continue
        for hit in call.result.hits:
            identifier = hit.candidate.video_chunk_id
            best_rank[identifier] = min(best_rank.get(identifier, hit.candidate.fused_rank), hit.candidate.fused_rank)
    # An empty draft is an honest "not found"; the Result self-check owns that case.
    if any(best_rank.get(identifier, low_confidence_rank + 1) > low_confidence_rank for identifier in draft_ids):
        risks.append("low_retrieval_confidence")
    return tuple(risks)


def attempt_score(attempt: ResultAttempt) -> tuple[bool, bool, int]:
    """Order attempts by self-check, then Evaluator verdict, then fewer named gaps."""

    return attempt.passed, not attempt.evaluator_rejected, -len(attempt.missing_evidence)


def improved(current: ResultAttempt, previous: Sequence[ResultAttempt]) -> bool:
    """Score monotonicity: a loop counts only if its self-check or its draft evidence got better."""

    if not previous:
        return True
    if attempt_score(current) > max(attempt_score(item) for item in previous):
        return True
    drafted_before = frozenset().union(*(item.draft_ids for item in previous))
    return bool(current.draft_ids - drafted_before)


def decide(
    current: ResultAttempt, previous: Sequence[ResultAttempt], *, max_loops: int,
) -> LoopOutcome:
    """Choose the exit for one outer loop: pass, ask the user, re-plan or degrade."""

    if current.output is not None:
        if current.output.self_check.needs_user_confirmation:
            return "ask_user"
        if current.passed:
            return "pass"
    if not current.missing_evidence:
        # Fail B re-plans only on a concrete gap that a new search could close.
        return "degrade"
    if current.loop >= max_loops or not improved(current, previous):
        return "degrade"
    return "replan"


def best_attempt(attempts: Sequence[ResultAttempt]) -> ResultAttempt:
    """Pick the best answered attempt; later loops win ties because they saw more evidence."""

    answered = [item for item in attempts if item.output is not None]
    if not answered:
        raise ValueError("Loop Control requires at least one Result")
    return max(answered, key=lambda item: (attempt_score(item), len(item.items), item.loop))


def replan_advice(attempt: ResultAttempt) -> str:
    """Turn a failed self-check into the next Plan turn's advice."""

    if attempt.output is None:
        return attempt.evaluator_advice
    check = attempt.output.self_check
    return (
        "Result self-check failed. Search for the missing evidence before drafting again. "
        f"Reason: {check.reason.strip()[:600]} Missing evidence: "
        + "; ".join(item[:300] for item in check.missing_evidence)
    )[:2_000]


def uncertainty_note(attempt: ResultAttempt, user_message: str) -> str:
    """Host-written note that marks a degraded answer as not fully verified."""

    reason = attempt.output.self_check.reason.strip() if attempt.output is not None else ""
    if attempt.evaluator_rejected and attempt.evaluator_advice.strip():
        reason = reason or attempt.evaluator_advice.strip()
    missing = "；".join(item[:200] for item in attempt.missing_evidence[:3] if item.strip() != reason)
    if re.search(r"[぀-ヿ㐀-鿿]", user_message):
        note = "> **未能完全核实**：以下回答和片段仅基于目前找到的最佳证据。"
        if reason:
            note += f"原因：{reason[:300]}"
        if missing:
            note += f"\n> 仍缺少：{missing}"
        return note
    note = "> **Not fully verified**: this answer and its clips use the best evidence found so far."
    if reason:
        note += f" Reason: {reason[:300]}"
    if missing:
        note += f"\n> Still missing: {missing}"
    return note
