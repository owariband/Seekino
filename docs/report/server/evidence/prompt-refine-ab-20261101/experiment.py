"""Frozen real-input Prompt/Refine comparison. No task writes or ASR calls."""
from __future__ import annotations

import base64
from collections import Counter
from copy import deepcopy
from dataclasses import asdict
import hashlib
import json
import os
from pathlib import Path
import sys
import time

HERE = Path(__file__).resolve().parent
OUT = HERE.parent
sys.path.insert(0, str(OUT))
from control import ENV, ROOT
os.environ.update(ENV)
sys.path.insert(0, str(ROOT))

from pydantic import BaseModel, ConfigDict
from gateway.config import ModelBinding, load_config
from gateway.model_factory import GatewayClient
from gateway.multimodal import ImageInput, TextInput
from gateway.usage import capture_model_usage, model_call_context
from engine.parsev2.schemas import VideoParseResult
from engine.parsev2.validation import validate_result_time_range
from agent.video_parse_v2.prompts import _canvas_instructions
from agent.video_parse_v2.timelineEvent_canvas import CanvasCompletion, ParseCanvasWrite, ParseContextCanvas
from agent.utils.context_canvas_tools import build_context_canvas_tools
from agent.utils.tool import as_langchain_tools

SOURCE_CALL = "d38de9b9-67e2-4eb1-b45c-15ee8ac42859"
DURATION_MS = 6979873


def write(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str) + "\n")


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def usage_record(value):
    # The usage context is a MappingProxyType; dataclasses.asdict cannot deepcopy it.
    return {**{key: getattr(value, key) for key in (
        "call_id", "provider", "model", "operation", "started_at", "request_id",
        "status", "ended_at", "granularity", "usage_source", "usage_scope",
    )}, "context": dict(value.context),
        "usage": asdict(value.usage) if value.usage is not None else None}


def original_request():
    path = OUT / "model-trace" / SOURCE_CALL / "events.jsonl"
    with path.open() as f:
        return json.loads(f.readline())["payload"]["request"]


def prompt_data():
    request = original_request()
    prompt = max((x["text"] for x in request["messages"][0]["content"] if x["type"] == "text"), key=len)
    values = {}
    for label in ("ASR input", "OCR input", "Background context", "all_speakers"):
        line = next(line for line in prompt.splitlines() if line.startswith(label + ": "))
        values[label] = json.loads(line[len(label) + 2:])
    return prompt, values


def candidate_understanding():
    _, values = prompt_data()
    return "\n".join((
        "Interpret this course video excerpt and return one VideoParseResult JSON object. No tools or commentary.",
        "The ASR is from the complete source video. Treat its wording, timestamps and voice groups as the primary dialogue evidence. Preserve the complete supplied dialogue even when it extends beyond this chunk's images or nominal time range. Do not trim or resegment speech just to fit a chunk or sentence boundary; adjacent duplicate dialogue and continuity are handled by Refine. Change ASR only when the supplied evidence clearly supports a correction.",
        "Use the supplied speaker background to name speakers; reuse supported mappings. Keep unresolved speakers null. Do not treat voice-group IDs as person names.",
        "Add significant visual events and visible screen text supported by the images. Images contain no audio. Preserve the original-language dialogue in text_variants; add translations only when supplied as evidence. Write concise factual descriptions in Simplified Chinese.",
        "Use the existing Schema consistently: persons registers people, participants records semantic involvement, and visible_persons records actual visual presence. A named speaker is also a participant. Omit host-owned speaker_source.",
        "All timestamps are absolute source-video integer milliseconds, including legacy *_seconds fields. Images have adjacent timestamp_ms labels. Nominal chunk: [0, 60000); supplied images: [0, 63000). These ranges do not truncate ASR dialogue. Source video: [0, 6979873). Return timeline ordered by start_seconds.",
        "all_speakers: " + json.dumps(values["all_speakers"], ensure_ascii=False, sort_keys=True),
        "ASR input: " + json.dumps(values["ASR input"], ensure_ascii=False, sort_keys=True),
        "OCR input: " + json.dumps(values["OCR input"], ensure_ascii=False, sort_keys=True),
        "Background context: " + json.dumps(values["Background context"], ensure_ascii=False, sort_keys=True),
    ))


def understanding_inputs(variant):
    request = original_request()
    images = HERE / "frames"
    images.mkdir(exist_ok=True)
    result = []
    for block in request["messages"][0]["content"]:
        if block["type"] == "text":
            text = block["text"]
            if len(text) > 100 and variant == "candidate":
                text = candidate_understanding()
            result.append(TextInput(text))
        else:
            ref = block["image_url"]["url"]
            url = (OUT / "model-trace" / SOURCE_CALL / ref["$diagnostic_media"]).read_text()
            assert hashlib.sha256(url.encode()).hexdigest() == ref["sha256"]
            header, body = url.split(",", 1)
            suffix = ".jpg" if "image/jpeg" in header else ".png"
            path = images / (ref["sha256"] + suffix)
            if not path.exists():
                path.write_bytes(base64.b64decode(body))
            result.append(ImageInput(path, detail=block["image_url"].get("detail", "high")))
    return result


class RefinedMember(VideoParseResult):
    sequence_no: int


class DirectRefineResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    members: list[RefinedMember]


def refine_input():
    source = json.loads((HERE / "refine-source.json").read_text())
    _, data = prompt_data()
    # Both arms use the same real, unchanged persisted contents and ASR evidence.
    members = []
    for m in source["members"]:
        evidence = deepcopy(m["detail"]["evidence"])
        # This comparison trusts whole-video cue timing instead of re-aligning
        # individual words. Both arms get the same compact ASR view; the raw
        # source file still retains every original word record.
        for cue in evidence.get("asr", {}).get("transcript", []):
            cue.pop("words", None)
        members.append({
            "sequence_no": m["sequence_no"], "content_version": m["version"],
            "nominal_chunk_range_ms": [m["start_ms"], m["end_ms"]],
            "content": m["content"], "evidence": evidence,
        })
    return {"background": data["Background context"], "members": members}


REFINE_TASK = "\n".join((
    "Refine these three adjacent chunks as one continuous course excerpt.",
    "The ASR comes from the complete source video. Prefer its dialogue wording and timestamps. Preserve complete dialogue across nominal chunk boundaries; do not truncate or split merely to fit a chunk. Remove repeated dialogue across adjacent chunks and repair genuine omissions or continuity errors using the supplied ASR. Keep each retained dialogue once, in a relevant member. Prefer existing event grouping; do not reconstruct every sentence's word-level timing.",
    "Use the supplied course background for supported speaker names. Preserve existing visual facts; no new visual or voice evidence is available in this text-editing comparison. Do not invent visual changes or reinterpret unidentified voices without evidence.",
    "Make only supported corrections. Keep unrelated events and their content unchanged. All timestamps are absolute integer milliseconds. The supplied ASR reaches 181440 ms, beyond nominal chunk [120000,180000); retaining that dialogue is allowed. Keep timestamps inside the source video [0,6979873), and do not invent out-of-evidence events. Maintain persons, participants and visible_persons consistently; omit host-owned speaker_source in new output.",
))


def canvas_and_prompt():
    src = json.loads((HERE / "refine-source.json").read_text())
    data = refine_input()
    # Widen only this in-memory experiment's media authority to the supplied ASR.
    # This is intentionally NOT a production range-policy implementation.
    canvas = ParseContextCanvas(
        scope="refine:prompt-ab-20261101", stage="refine",
        members={m["chunk_parse_task_id"]: {
            "sequence_no": m["sequence_no"], "version": m["version"],
            "bounds": (0, 181440), "scope": m["detail"].get("scope", [m["sequence_no"]]),
            "content": VideoParseResult.model_validate(m["content"]),
        } for m in src["members"]},
        evidence={m["chunk_parse_task_id"]: data["members"][m["sequence_no"]]["evidence"] for m in src["members"]},
        background=data["background"], write_bounds=(0, 181440),
    )
    prompt = "\n".join((
        REFINE_TASK,
        _canvas_instructions(),
        "Read the members, events, ASR evidence and background with get_context_canvas. Apply corrections with write_context_canvas. Complete the review with CanvasCompletion {\"complete\":true}.",
        "Nominal chunk ranges: 0=[0,60000), 1=[60000,120000), 2=[120000,180000).",
        canvas.checkpoint(),
    ))
    return canvas, prompt


def run(stage, variant):
    name = stage + "-" + variant
    dest = HERE / name
    dest.mkdir(exist_ok=True)
    if (dest / "started.json").exists():
        raise RuntimeError("Already attempted this arm; inspect evidence before any rerun")
    os.environ["SEEKINO_MODEL_TRACE_DIR"] = str(dest / "model-trace")
    binding = ModelBinding.model_validate(json.loads((HERE / "model-binding.json").read_text()))
    # No automatic fallback or retry: at most one 600 s run per arm.
    binding = binding.model_copy(update={"fallback": None})
    gateway = GatewayClient(load_config(OUT / "model-config.yaml"), environ=ENV)
    model = gateway.create_generative_model(binding)
    tool_log = []
    options = {}
    canvas = None
    if stage == "understanding":
        inputs = understanding_inputs(variant)
        result_type = VideoParseResult
        schema_name = "video_parse_v2_chunk"
    elif variant == "canvas":
        canvas, prompt = canvas_and_prompt()
        inputs = [TextInput(prompt)]
        result_type = CanvasCompletion
        schema_name = "CanvasCompletion"
        options = {
            "tools": as_langchain_tools(build_context_canvas_tools(
                canvas, role="refine", read_collections=("members", "events", "evidence", "background", "compact"),
                write_args_schema=ParseCanvasWrite, maximum_result_bytes=32000,
                maximum_read_chars=12000, maximum_catalog_items=100,
                on_call=lambda name: (tool_log.append({"name": name, "at": time.time()}), print("tool", name, flush=True)),
            )),
            "context_checkpoint": canvas.checkpoint, "tool_harness": "deepagents",
        }
    else:
        prompt = "\n".join((
            REFINE_TASK,
            "Edit the supplied JSON directly. Return a complete members array for sequence_no 0, 1 and 2, each with persons and timeline. Omitted events are removed, so preserve every unrelated event. No tools or commentary. Return only DirectRefineResult JSON.",
            "Input: " + json.dumps(refine_input(), ensure_ascii=False, sort_keys=True),
        ))
        inputs = [TextInput(prompt)]
        result_type = DirectRefineResult
        schema_name = "DirectRefineResult"
    (dest / "prompt.txt").write_text("\n".join(i.text for i in inputs if isinstance(i, TextInput)))
    write(dest / "started.json", {"at": time.time(), "stage": stage, "variant": variant,
          "source_call_id": SOURCE_CALL, "refine_input_sha256": digest(refine_input()),
          "binding": binding.model_dump(mode="json"), "calls_budget": "one run, 600 s, no retry/fallback"})
    usage = []
    start = time.monotonic()
    print("START", name, flush=True)
    report = {"stage": stage, "variant": variant}
    try:
        with model_call_context({"video_parse_task_id": "experiment-prompt-refine-ab-20261101", "experiment": "prompt-refine-ab-20261101", "stage": stage, "arm": name}), capture_model_usage(lambda u: usage.append(usage_record(u))):
            result = model.generate(inputs, schema_name=schema_name, result_type=result_type,
                                    max_model_calls=None, **options)
        write(dest / "response.json", result.model_dump(mode="json"))
        if canvas is not None:
            result = DirectRefineResult(members=[RefinedMember(sequence_no=m["sequence_no"], **canvas.result(m["chunk_parse_task_id"]).model_dump(mode="json"))
                                                for m in json.loads((HERE / "refine-source.json").read_text())["members"]])
        if stage == "refine":
            if sorted(m.sequence_no for m in result.members) != [0, 1, 2]:
                raise ValueError("Refine must return each of the three bound members exactly once")
            for m in result.members:
                validate_result_time_range(VideoParseResult(persons=m.persons, timeline=m.timeline), start_seconds=0, end_seconds=DURATION_MS)
        else:
            validate_result_time_range(result, start_seconds=0, end_seconds=DURATION_MS)
        write(dest / "result.json", result.model_dump(mode="json"))
        report["status"] = "completed"
    except Exception as e:
        report.update(status="failed", error_type=type(e).__name__, error=str(e)[:2500])
        if canvas is not None:
            write(dest / "partial-canvas.json", {str(m["sequence_no"]): canvas.result(m["chunk_parse_task_id"]).model_dump(mode="json")
                                                for m in json.loads((HERE / "refine-source.json").read_text())["members"]})
        print("FAILED", name, type(e).__name__, flush=True)
    finally:
        report.update(elapsed_seconds=time.monotonic()-start, usage=usage, tools=tool_log)
        write(dest / "summary.json", report)
        gateway.close()
        print("END", name, report["status"], round(report["elapsed_seconds"], 3), flush=True)
    return report


def prepare():
    old, _ = prompt_data()
    (HERE / "understanding-before.txt").write_text(old)
    (HERE / "understanding-candidate.txt").write_text(candidate_understanding())
    write(HERE / "refine-input.json", refine_input())
    inputs = understanding_inputs("candidate")
    assert sum(isinstance(i, ImageInput) for i in inputs) == 189
    canvas, _ = canvas_and_prompt()
    assert len(canvas.items("events")) == 47
    print("prepared 189 unchanged frames; frozen 3-member/47-event Refine input", flush=True)


if __name__ == "__main__":
    if sys.argv[1] == "prepare":
        prepare()
    else:
        run(sys.argv[1], sys.argv[2])
