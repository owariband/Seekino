"""Prepare and diarize the complete historical EP04 failure intervals offline.

    python -m evals.speaker.speaker_diarization --source-video <EP04.mp4> \
        --output-dir outputs/verification/<run> --prepare-only
    python -m evals.speaker.speaker_diarization --source-video <EP04.mp4> \
        --output-dir outputs/verification/<run> --config <local.yaml> --model-ref diarization

No server, database, ASR, Understanding or B0 scoring is invoked. Anonymous
speaker IDs belong to one call only. Predictions need independent human review.
"""
from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import html
import json
from pathlib import Path
import subprocess
import time
import wave


# Full input ranges from docs/dev/problems.md B12, rather than target sentences.
_SOURCE_SHA256 = "50e981e49a3d74e8c0aca7571ffae8265b88f6388f1d34849876b61ad8bf5814"
_CASES = (
    ("v15_soyo", 35400, 48700, "historical_merged_input"),
    ("v15_anon", 291300, 299000, "historical_merged_input"),
    ("v17_soyo", 45200, 48800, "historical_merged_input"),
    ("v17_anon", 289600, 296800, "historical_merged_input"),
    ("v18_soyo", 35300, 48500, "historical_merged_input"),
    ("v18_anon", 284300, 295700, "historical_merged_input"),
    ("v12_soyo", 47400, 48700, "original_short_input"),
    ("v12_anon", 293500, 296000, "original_short_input"),
    # Prior tail-crop protocol candidates: not human-confirmed single speakers.
    ("control_soyo_candidate", 567800, 570200, "unverified_control_candidate"),
    ("control_anon_candidate", 1010000, 1012500, "unverified_control_candidate"),
)


def _write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _activity(turns: list[dict]) -> dict:
    boundaries = sorted({turn[key] for turn in turns for key in ("start_ms", "end_ms")})
    intervals = []
    for start, end in zip(boundaries, boundaries[1:]):
        speakers = sorted({turn["speaker"] for turn in turns
                           if turn["start_ms"] < end and start < turn["end_ms"]})
        if intervals and intervals[-1]["speakers"] == speakers:
            intervals[-1]["end_ms"] = end
        else:
            intervals.append({"start_ms": start, "end_ms": end, "speakers": speakers})
    changes, sequence = [], []
    previous = None
    for interval in intervals:
        if len(interval["speakers"]) != 1:
            continue
        speaker = interval["speakers"][0]
        if previous and previous["speakers"][0] != speaker:
            changes.append({"start_ms": previous["end_ms"], "end_ms": interval["start_ms"],
                            "from_speaker": previous["speakers"][0], "to_speaker": speaker})
        if not sequence or sequence[-1] != speaker:
            sequence.append(speaker)
        previous = interval
    return {"speaker_count": len({turn["speaker"] for turn in turns}),
            "activity_intervals": intervals, "single_speaker_sequence": sequence,
            "change_regions": changes,
            "overlap_intervals": [row for row in intervals if len(row["speakers"]) > 1]}


def _write_review(output: Path, manifest: dict, results: list[dict]) -> None:
    annotations_path = output / "annotations.json"
    template = {"source_sha256": manifest["source_sha256"], "samples": [
        {"id": row["id"], "pcm_sha256": row["pcm_sha256"], "status": "unverified",
         "turns": [], "uncertain_boundaries": [], "notes": ""}
        for row in manifest["samples"]]}
    # Never overwrite a listener's independent annotations on rerun.
    annotations = json.loads(annotations_path.read_text(encoding="utf-8")) if annotations_path.exists() else template
    if annotations["source_sha256"] != template["source_sha256"] or {
        (row["id"], row["pcm_sha256"]) for row in annotations["samples"]
    } != {(row["id"], row["pcm_sha256"]) for row in template["samples"]}:
        raise ValueError("Existing annotations belong to different source audio; use a new output directory")
    if not annotations_path.exists():
        _write_json(annotations_path, annotations)
    rows = []
    for sample, result in zip(manifest["samples"], results, strict=True):
        rows.append(f'<section><h2>{html.escape(sample["id"])}: '
                    f'{sample["start_ms"]/1000:.3f}–{sample["end_ms"]/1000:.3f}s</h2>'
                    f'<p>{html.escape(sample["category"])}</p>'
                    f'<audio controls preload="metadata" src="{sample["audio_path"]}"></audio>'
                    f'<pre>{html.escape(json.dumps(result, ensure_ascii=False, indent=2))}</pre></section>')
    payload = json.dumps(annotations, ensure_ascii=False).replace("</", "<\\/")
    (output / "listening.html").write_text("""<!doctype html><html lang="zh"><meta charset="utf-8">
<title>EP04 独立说话人分段复核</title><style>body{max-width:1000px;margin:2em auto;font:16px sans-serif}
pre,textarea{white-space:pre-wrap;overflow-wrap:anywhere}section{border-top:1px solid #aaa;padding:1em 0}
textarea{width:100%;height:35em}audio{width:100%}</style><h1>EP04 独立说话人分段复核</h1>
<p>播放器时间相对本片段；标题时间为全片位置。模型的匿名 ID 仅在各次调用内有效。
先独立听音标注，再比较预测。turns 使用相对毫秒 start_ms/end_ms、匿名 speaker，可另加 person；
多人同时发声请标交叠 turns。uncertain_boundaries 写时间范围和理由。
仅核实目标句人物，不意味着整段同人。普通对照目前也未人工确认；未标注时不计算准确率。
模型 change_regions 是相邻单人活动之间的范围，可能含停顿或重叠，不能当作已验证边界误差。</p>
""" + "\n".join(rows) + """<h2>独立人工标注</h2><p>完成后将 status 改为 reviewed，下载后替换 annotations.json。
保留本文件的 source_sha256 和每段 pcm_sha256。</p><textarea id="annotations"></textarea>
<button id="save">下载 annotations.json</button><script>
const editor=document.getElementById('annotations');editor.value=JSON.stringify(""" + payload + """,null,2);
document.getElementById('save').onclick=()=>{const value=JSON.parse(editor.value);
const url=URL.createObjectURL(new Blob([JSON.stringify(value,null,2)+'\\n'],{type:'application/json'}));
const a=document.createElement('a');a.href=url;a.download='annotations.json';a.click();URL.revokeObjectURL(url)};
</script></html>""", encoding="utf-8")
    lines = ["# EP04 本地说话人分段验证", "", f"运行状态：{dict(Counter(row['status'] for row in results))}",
             f"人工标注状态：{dict(Counter(row['status'] for row in annotations['samples']))}", "",
             "仅使用真实历史完整输入；模型预测与人工真值分开保存。本脚本展示预测，不计算准确率、漏检率、单人误报或边界误差；这些验收需要独立人工标注与逐项比较。",
             "目标短句的已确认人物只适用于该台词：47.4–48.7s 爽世；293.5–296.0s 爱音。",
             "A→B→A、短应答、重叠和普通单人对照均须独立核实。未检测到重叠不能证明没有依次换人。", "",
             "| 样本 | 全片范围(s) | 状态 | 匿名人数 | 预测变化区域 | 预测重叠区域 | 耗时(s) |",
             "|---|---|---|---:|---:|---:|---:|"]
    for sample, result in zip(manifest["samples"], results, strict=True):
        observed = result.get("observations", {})
        lines.append(f"| {sample['id']} | {sample['start_ms']/1000:.3f}–{sample['end_ms']/1000:.3f} | "
                     f"{result['status']} | {observed.get('speaker_count', '—')} | "
                     f"{len(observed['change_regions']) if observed else '—'} | "
                     f"{len(observed['overlap_intervals']) if observed else '—'} | {result.get('elapsed_seconds', 0):.3f} |")
    for result in results:
        if "error" in result:
            lines.extend(["", f"{result['id']}: `{result['error']}`"])
    lines.extend(["", "输入及 PCM 哈希见 manifest.json；相对时间原始预测及全片时间映射见 results.json。",
                  "首次推理耗时包括模型加载。listening.html 可离线播放并下载人工标注；annotations.json 在重跑时保留。",
                  "本次不改 B0/CAM++、参考池或生产任务；未进行人物排名改善验收。"])
    (output / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-video", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--config", type=Path)
    parser.add_argument("--model-ref", default="diarization")
    parser.add_argument("--prepare-only", action="store_true")
    args = parser.parse_args()
    if not args.prepare_only and args.config is None:
        parser.error("--config is required for inference")
    source, output = args.source_video.expanduser().resolve(), args.output_dir.expanduser().resolve()
    if not source.is_file():
        raise FileNotFoundError(source)
    audio_dir = output / "audio"
    audio_dir.mkdir(parents=True, exist_ok=True)
    manifest = {"video_id": "0019c16c-a7b7-5f6c-8275-5019869c1207", "source_video": str(source),
                "source_sha256": _sha256(source), "samples": [],
                "interval_evidence": "docs/dev/problems.md B12 2026-10-19 investigation tables",
                "control_evidence": "speaker-tail-crop-20261019/protocol.json; candidate selection, not human truth"}
    if manifest["source_sha256"] != _SOURCE_SHA256:
        raise ValueError("Source video differs from the fixed EP04 investigation baseline")
    if args.config is not None:
        manifest.update(config_path=str(args.config.resolve()), config_sha256=_sha256(args.config))
    for key, start, end, category in _CASES:
        path = audio_dir / f"{key}.wav"
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", f"{start/1000:.3f}", "-i", str(source),
                        "-t", f"{(end-start)/1000:.3f}", "-map", "0:a:0", "-vn", "-ac", "1",
                        "-ar", "16000", "-c:a", "pcm_s16le", str(path)], check=True)
        with wave.open(str(path), "rb") as wav:
            if (wav.getnchannels(), wav.getsampwidth(), wav.getframerate(), wav.getcomptype()) != (1, 2, 16000, "NONE"):
                raise ValueError(f"Unexpected WAV format: {key}")
            pcm = wav.readframes(wav.getnframes())
        if len(pcm) != (end-start)*32:
            raise ValueError(f"Audio duration differs from historical input: {key}")
        manifest["samples"].append({"id": key, "start_ms": start, "end_ms": end, "category": category,
                                    "audio_path": str(path.relative_to(output)), "wav_sha256": _sha256(path),
                                    "pcm_sha256": hashlib.sha256(pcm).hexdigest()})
    _write_json(output / "manifest.json", manifest)
    client, model, load_error = None, None, None
    results = []
    try:
        if not args.prepare_only:
            try:
                from gateway.config import load_config
                from gateway.model_factory import GatewayClient

                client = GatewayClient(load_config(args.config))
                model = client.create_speaker_diarization_model(args.model_ref)
            except Exception as exc:
                load_error = f"{type(exc).__name__}: {exc}"
        for sample in manifest["samples"]:
            row = {"id": sample["id"], "status": "prepared"}
            started = time.monotonic()
            if load_error:
                row.update(status="blocked", error=load_error)
            elif model is not None:
                try:
                    prediction = model.diarize_audio(output / sample["audio_path"]).model_dump(mode="json")
                    if prediction["duration_ms"] != sample["end_ms"]-sample["start_ms"]:
                        raise ValueError("Prediction duration differs from prepared input")
                    row.update(status="predicted", prediction=prediction, observations=_activity(prediction["turns"]),
                               absolute_turns=[{**turn, "start_ms": sample["start_ms"]+turn["start_ms"],
                                                "end_ms": sample["start_ms"]+turn["end_ms"]}
                                               for turn in prediction["turns"]])
                except Exception as exc:
                    row.update(status="blocked", error=f"{type(exc).__name__}: {exc}")
            row["elapsed_seconds"] = round(time.monotonic()-started, 6)
            results.append(row)
            _write_json(output / f"{sample['id']}-result.json", row)
    finally:
        if client is not None:
            client.close()
    _write_json(output / "results.json", results)
    _write_review(output, manifest, results)
    print(json.dumps({"output_dir": str(output), "statuses": dict(Counter(row["status"] for row in results))}))
    return 2 if any(row["status"] == "blocked" for row in results) else 0


if __name__ == "__main__":
    raise SystemExit(main())
