"""Fixed-target Soyo sample comparison, using the deployed Gateway CAM++ adapter."""
from __future__ import annotations

import argparse
import base64
from datetime import UTC, datetime
import hashlib
import html
import json
import math
from pathlib import Path
import subprocess
import sys
import time
import wave

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT))


def sha(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def write(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def cosine(a, b):
    return max(-1.0, min(1.0, sum(x * y for x, y in zip(a, b)) / math.hypot(*a) / math.hypot(*b)))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    home = Path(__file__).resolve().parent
    manifest_path = home / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    source = ROOT / manifest["video_path"]
    checkpoint = ROOT / manifest["checkpoint"]
    assert sha(source) == manifest["video_sha256"]
    assert sha(checkpoint) == manifest["checkpoint_sha256"]
    upstream = ROOT / manifest["model_home"]
    assert subprocess.check_output(["git", "-C", str(upstream), "rev-parse", "HEAD"], text=True).strip() == manifest["upstream_commit"]
    for ref in manifest["references"]:
        path = ROOT / ref["initial_understanding_source"]
        assert sha(path) == ref["initial_understanding_sha256"]
        snapshot = json.loads(path.read_text())
        assert snapshot["task_id"] == manifest["video_parse_task_id"]
        assert ref["initial_event"] in snapshot["content"]["timeline"]
        assert ref["initial_event"]["speaker"] == "长崎爽世"
    args.output.mkdir(parents=True, exist_ok=False)
    audio = args.output / "audio"
    audio.mkdir()
    sys.path.insert(0, str(upstream))
    from gateway.config import CampPlusModelSourceConfig, ModelBinding
    from gateway.speaker_embedding import build_speaker_embedding_model
    from gateway.usage import capture_model_usage

    protocol = manifest["protocol"]
    binding = ModelBinding(source="campplus_local", model=manifest["registered_baseline"]["model"]["name"],
                           kind="speaker_embedding", request_timeout_seconds=protocol["request_timeout_seconds"])
    encoder = build_speaker_embedding_model(binding, CampPlusModelSourceConfig(
        checkpoint_path=str(checkpoint), cpu_threads=protocol["cpu_threads"]))
    calls, records = [], []

    def usage(call):
        calls.append({"id": call.call_id, "operation": call.operation, "status": call.status,
                      "started_at": call.started_at, "ended_at": call.ended_at})
        write(args.output / "model-calls.json", calls)

    def embed(name, start, end):
        target = manifest["target"]
        if name != target["id"]:
            assert end <= target["start_ms"] or start >= target["end_ms"]
        wav = audio / f"{name}.wav"
        subprocess.run(["ffmpeg", "-v", "error", "-nostdin", "-ss", f"{start / 1000:.3f}",
                        "-i", str(source), "-t", f"{(end - start) / 1000:.3f}", "-map", "0:a:0",
                        "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", "-n", str(wav)],
                       check=True, timeout=30, capture_output=True)
        with wave.open(str(wav), "rb") as stream:
            assert (stream.getnchannels(), stream.getframerate(), stream.getsampwidth()) == (1, 16000, 2)
            pcm = stream.readframes(stream.getnframes())
        started = time.perf_counter()
        result = encoder.embed_audio(wav)
        assert result.dimension == 192 and math.isclose(math.hypot(*result.vector), 1, abs_tol=1e-6)
        assert result.encoder_revision == manifest["registered_baseline"]["model"]["encoder_revision"]
        record = {"id": name, "start_ms": start, "end_ms": end, "pcm_sha256": hashlib.sha256(pcm).hexdigest(),
                  "audio": str(wav.relative_to(args.output)), "embedding_ms": (time.perf_counter() - started) * 1000,
                  **result.model_dump(mode="json")}
        records.append(record)
        write(args.output / "embeddings.json", records)
        print(json.dumps({k: record[k] for k in ("id", "duration_ms", "embedding_ms")}), flush=True)
        return record

    rows = []
    try:
        with capture_model_usage(usage):
            target = manifest["target"]
            probe = embed(target["id"], target["start_ms"], target["end_ms"])
            assert probe["pcm_sha256"] == target["expected_pcm_sha256"]
            for ref in manifest["references"]:
                full = embed(ref["id"], ref["start_ms"], ref["end_ms"])
                short = full if full["duration_ms"] == 1500 else embed(
                    ref["id"] + "-first1500", ref["start_ms"], ref["start_ms"] + 1500)
                row = {"id": ref["id"], "state": ref["state_inferred_from_parse"],
                       "start_ms": ref["start_ms"], "end_ms": ref["end_ms"],
                       "duration_ms": full["duration_ms"], "full_cosine": cosine(probe["vector"], full["vector"]),
                       "first1500_cosine": cosine(probe["vector"], short["vector"]),
                       "full_audio": full["audio"], "first1500_audio": short["audio"],
                       "translation": next((t["text"] for t in ref["text_variants"] if t["role"] == "TRANSLATION"), ""),
                       "full_vector": full["vector"], "short_vector": short["vector"]}
                if ref["id"] == "baseline":
                    original = manifest["registered_baseline"]["references"][0]
                    assert full["pcm_sha256"] == original["pcm_sha256"]
                    assert cosine(full["vector"], original["vector"]) > 0.999999
                rows.append(row)
    finally:
        encoder.close()
    centroids = {}
    for group, selected in (("all", rows), ("cross_scene", [r for r in rows if r["id"] != "calm_near_target"])):
        for name, key in (("full", "full_vector"), ("first1500", "short_vector")):
            mean = [sum(xs) / len(selected) for xs in zip(*(r[key] for r in selected))]
            centroids[f"{group}_{name}"] = cosine(probe["vector"], mean)
    other = {r["speaker"]: cosine(probe["vector"], r["references"][0]["vector"])
             for r in manifest["other_registered_speakers"]}
    rows = [{k: v for k, v in row.items() if k not in {"full_vector", "short_vector"}} for row in rows]
    result = {"completed_at": datetime.now(UTC).isoformat(), "manifest_sha256": sha(manifest_path),
              "target": {k: v for k, v in probe.items() if k != "vector"}, "samples": rows,
              "centroids": centroids, "fixed_other_registered_speakers": other, "model_calls": len(calls),
              "limits": protocol["interpretation"]}
    write(args.output / "results.json", result)
    escape = html.escape
    def audio_url(relative):
        return "data:audio/wav;base64," + base64.b64encode((args.output / relative).read_bytes()).decode()
    body = "".join(f'<tr><td>{escape(r["id"])}</td><td>{escape(r["state"])}</td>'
                   f'<td>{r["start_ms"]/1000:.3f}–{r["end_ms"]/1000:.3f}s<br>{escape(r["translation"])}</td>'
                   f'<td>{r["full_cosine"]:.6f}<br><audio controls preload="none" src="{audio_url(r["full_audio"])}"></audio></td>'
                   f'<td>{r["first1500_cosine"]:.6f}<br><audio controls preload="none" src="{audio_url(r["first1500_audio"])}"></audio></td></tr>'
                   for r in sorted(rows, key=lambda x: -x["full_cosine"]))
    (args.output / "report.html").write_text('<!doctype html><meta charset="utf-8"><title>爽世声音对照</title>'
        '<style>body{font:16px system-ui;margin:32px;color:#202530}table{border-collapse:collapse;width:100%}'
        'td,th{border:1px solid #ccd2da;padding:12px;text-align:left}audio{width:240px}p{max-width:1000px}</style>'
        '<h1>爽世不同说话状态与固定 null 片段比对</h1><p>目标固定为 47.700–48.700 秒。'
        '状态由解析描述推定，尚未人工试听标注。右列统一取每段开头1.5秒；相似度不是概率。</p>'
        f'<audio controls src="{audio_url(probe["audio"])}"></audio><table><tr><th>样本</th><th>状态</th><th>来源与台词</th>'
        f'<th>完整片段</th><th>首1.5秒</th></tr>{body}</table><p>' + escape(result["limits"]) + '</p>')
    print(json.dumps({"centroids": centroids, "fixed_others": other, "model_calls": len(calls)}), flush=True)


if __name__ == "__main__":
    main()
