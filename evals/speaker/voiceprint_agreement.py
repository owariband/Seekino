"""Score every DIALOGUE line of a parse task against its stored voiceprints.

Read-only against MySQL. Extracts each line's audio with ffmpeg, embeds it with
the configured speaker encoder, and compares it with the task's registered
references (overlapping references are excluded, as the Refine host does).
Writes one JSON row per line so agreement with the timeline label can be
broken down by margin and duration, and disagreements exported for labeling.

Usage:
    uv run --no-sync --env-file .env.local python -m evals.speaker.voiceprint_agreement \
        --task-id <video_parse_task_id> --out outputs/verification/<run>/lines.jsonl
"""

from __future__ import annotations

import argparse
import json
import math
import os
import subprocess
import tempfile
from pathlib import Path
from urllib.parse import unquote

from sqlalchemy import create_engine, text


def _unit(vector: list[float]) -> list[float]:
    norm = math.hypot(*vector)
    return [value / norm for value in vector]


def _dot(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b))


def _scores(target: list[float], refs: dict[str, list[list[float]]]) -> dict[str, dict[str, float]]:
    out = {}
    for speaker, vectors in refs.items():
        cosines = [_dot(target, vector) for vector in vectors]
        centroid = [sum(values) / len(vectors) for values in zip(*vectors)]
        out[speaker] = {
            "centroid": _dot(target, _unit(centroid)),
            "mean": sum(cosines) / len(cosines),
            "max": max(cosines),
        }
    return out


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--artifact-root", default=os.environ.get("ANIVAG_ARTIFACT_ROOT"))
    args = parser.parse_args()

    from gateway.config import load_config
    from gateway.model_factory import GatewayClient

    engine = create_engine(os.environ["ANIVAG_DATABASE_URL"])
    with engine.connect() as connection:
        task = connection.execute(text(
            "select t.detail, v.source_uri, v.duration_ms from video_parse_task t "
            "join video v on v.id = t.video_id where t.id = :id"), {"id": args.task_id}).one()
        timelines = connection.execute(text(
            "select c.content from chunk_parse_content c join chunk_parse_task t on t.id = c.chunk_task_id "
            "where t.video_task_id = :id"), {"id": args.task_id}).all()
    detail = json.loads(task.detail)
    references: list[dict] = []
    for person in detail["voiceprint"]["voiceprintList"]:
        for ref in person["references"]:
            references.append({"speaker": person["speaker"], "start_ms": ref["start_ms"],
                               "end_ms": ref["end_ms"], "vector": _unit(ref["vector"])})
    relative = unquote(task.source_uri.split("://", 1)[1])
    source = Path(args.artifact_root) / relative
    if not source.is_file():
        raise SystemExit(f"source video not found: {source}")

    lines = []
    for (content,) in timelines:
        for event in json.loads(content)["timeline"]:
            if event.get("event_type") != "DIALOGUE":
                continue
            original = next((item["text"] for item in event.get("text_variants", [])
                             if item.get("role") == "ORIGINAL"), "")
            lines.append({"start_ms": event["start_seconds"], "end_ms": event["end_seconds"],
                          "label": event.get("speaker"), "text": original})
    lines.sort(key=lambda item: item["start_ms"])

    gateway = GatewayClient(load_config(os.environ.get("ANIVAG_MODEL_CONFIG", "config.yaml")))
    encoder = gateway.create_speaker_embedding_model("speaker")
    args.out.parent.mkdir(parents=True, exist_ok=True)
    try:
        with tempfile.TemporaryDirectory() as workdir, args.out.open("w", encoding="utf-8") as output:
            for index, line in enumerate(lines):
                wav = Path(workdir) / f"{index}.wav"
                subprocess.run([
                    "ffmpeg", "-v", "error", "-y", "-ss", f"{line['start_ms'] / 1000:.3f}",
                    "-to", f"{line['end_ms'] / 1000:.3f}", "-i", str(source),
                    "-map", "0:a:0", "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(wav),
                ], check=True)
                eligible: dict[str, list[list[float]]] = {}
                for ref in references:
                    if ref["start_ms"] < line["end_ms"] and line["start_ms"] < ref["end_ms"]:
                        continue
                    eligible.setdefault(ref["speaker"], []).append(ref["vector"])
                try:
                    target = _unit(encoder.embed_audio(wav).vector)
                except Exception as exc:  # short or silent clips
                    output.write(json.dumps({**line, "error": type(exc).__name__}, ensure_ascii=False) + "\n")
                    continue
                scores = _scores(target, eligible)
                output.write(json.dumps({**line, "scores": scores}, ensure_ascii=False) + "\n")
    finally:
        gateway.close()


if __name__ == "__main__":
    main()
