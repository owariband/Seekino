"""Balanced three-speaker reference aggregation against one fixed EP04 target."""
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
    return sum(x * y for x, y in zip(a, b)) / math.hypot(*a) / math.hypot(*b)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    manifest_path = Path(__file__).with_name("manifest.json")
    manifest = json.loads(manifest_path.read_text())
    source, checkpoint = ROOT / manifest["video_path"], ROOT / manifest["checkpoint"]
    assert sha(source) == manifest["video_sha256"] and sha(checkpoint) == manifest["checkpoint_sha256"]
    upstream = ROOT / manifest["model_home"]
    assert subprocess.check_output(["git", "-C", str(upstream), "rev-parse", "HEAD"], text=True).strip() == manifest["upstream_commit"]
    for ref in manifest["references"]:
        path = ROOT / ref["source_path"]
        assert sha(path) == ref["source_sha256"]
        snapshot = json.loads(path.read_text())
        assert snapshot["version"] == 1 and snapshot["task_id"] == manifest["video_parse_task_id"]
        assert ref["initial_event"] in snapshot["content"]["timeline"]
        assert ref["initial_event"]["speaker"] == ref["speaker"]
    args.output.mkdir(parents=True, exist_ok=False)
    (args.output / "audio").mkdir()
    sys.path.insert(0, str(upstream))
    from gateway.config import CampPlusModelSourceConfig, ModelBinding
    from gateway.speaker_embedding import build_speaker_embedding_model
    from gateway.usage import capture_model_usage

    baselines = {r["speaker"]: r for r in manifest["registered_baselines"]}
    protocol = manifest["protocol"]
    control_ms = protocol["length_control_ms"]
    binding = ModelBinding(source="campplus_local", kind="speaker_embedding",
                           model=next(iter(baselines.values()))["model"]["name"],
                           request_timeout_seconds=protocol["request_timeout_seconds"])
    encoder = build_speaker_embedding_model(binding, CampPlusModelSourceConfig(
        checkpoint_path=str(checkpoint), cpu_threads=protocol["cpu_threads"]))
    calls, embeddings, rows = [], [], []

    def usage(call):
        calls.append({"id": call.call_id, "status": call.status, "operation": call.operation,
                      "started_at": call.started_at, "ended_at": call.ended_at})
        write(args.output / "model-calls.json", calls)

    def embed(name, start, end):
        wav = args.output / "audio" / f"{name}.wav"
        subprocess.run(["ffmpeg", "-v", "error", "-nostdin", "-ss", f"{start / 1000:.3f}", "-i", str(source),
                        "-t", f"{(end-start) / 1000:.3f}", "-map", "0:a:0", "-vn", "-ac", "1", "-ar", "16000",
                        "-c:a", "pcm_s16le", "-n", str(wav)], check=True, timeout=30, capture_output=True)
        with wave.open(str(wav), "rb") as stream:
            assert (stream.getframerate(), stream.getsampwidth(), stream.getnchannels()) == (16000, 2, 1)
            pcm = stream.readframes(stream.getnframes())
        started = time.perf_counter()
        result = encoder.embed_audio(wav)
        assert result.dimension == 192 and abs(math.hypot(*result.vector)-1) < 1e-6
        assert result.encoder_revision == next(iter(baselines.values()))["model"]["encoder_revision"]
        record = {"id": name, "start_ms": start, "end_ms": end, "pcm_sha256": hashlib.sha256(pcm).hexdigest(),
                  "audio": str(wav.relative_to(args.output)), "embedding_ms": (time.perf_counter()-started)*1000,
                  **result.model_dump(mode="json")}
        embeddings.append(record)
        write(args.output / "embeddings.json", embeddings)
        print(json.dumps({k: record[k] for k in ("id", "duration_ms", "embedding_ms")}), flush=True)
        return record

    try:
        with capture_model_usage(usage):
            target = manifest["target"]
            probe = embed("null_target", target["start_ms"], target["end_ms"])
            assert probe["pcm_sha256"] == target["expected_pcm_sha256"]
            for ref in manifest["references"]:
                assert ref["end_ms"] <= target["start_ms"] or ref["start_ms"] >= target["end_ms"]
                full = embed(ref["id"], ref["start_ms"], ref["end_ms"])
                short = full if full["duration_ms"] == control_ms else embed(
                    ref["id"]+"-first1500", ref["start_ms"], ref["start_ms"]+control_ms)
                if ref["baseline"]:
                    old = baselines[ref["speaker"]]["references"][0]
                    assert full["pcm_sha256"] == old["pcm_sha256"] and cosine(full["vector"], old["vector"]) > 0.999999
                rows.append({"id": ref["id"], "speaker": ref["speaker"], "baseline": ref["baseline"],
                             "full": full, "first1500": short})
    finally:
        encoder.close()
    assert len({probe["pcm_sha256"], *(r["full"]["pcm_sha256"] for r in rows)}) == len(rows)+1
    summaries, details, methods = [], [], ["mean_pairwise_cosine", "cosine_of_normalized_centroid", "capped_duration_weighted_mean_cosine"]
    for mode in ("full", "first1500"):
        scores = {name: {} for name in methods}
        for speaker in baselines:
            selected = [row for row in rows if row["speaker"] == speaker]
            assert len(selected) == manifest["reference_count_per_speaker"]
            vectors = [row[mode]["vector"] for row in selected]
            individual = [cosine(probe["vector"], vector) for vector in vectors]
            mean = [sum(xs)/len(vectors) for xs in zip(*vectors)]
            weights = [min(row[mode]["duration_ms"], manifest["protocol"]["duration_weight_ms_cap"]) for row in selected]
            avg = sum(individual)/len(individual)
            scores[methods[0]][speaker] = avg
            scores[methods[1]][speaker] = cosine(probe["vector"], mean)
            scores[methods[2]][speaker] = sum(w*s for w,s in zip(weights,individual))/sum(weights)
            assert abs(scores[methods[1]][speaker]-avg/math.hypot(*mean)) < 1e-10
            if mode == "first1500":
                assert abs(scores[methods[2]][speaker]-avg) < 1e-10
            details.append({"mode": mode, "speaker": speaker, "centroid_norm": math.hypot(*mean),
                            "references": [{"id": row["id"], "cosine": score, "weight": weight/sum(weights),
                                            "audio": row[mode]["audio"], "duration_ms": row[mode]["duration_ms"]}
                                           for row,score,weight in zip(selected,individual,weights)]})
        if mode == "full":
            scores["original_single_baseline"] = {row["speaker"]: cosine(probe["vector"],row["full"]["vector"]) for row in rows if row["baseline"]}
        for method, values in scores.items():
            ranking = sorted(values.items(), key=lambda x:-x[1])
            summaries.append({"mode": mode, "method": method, "scores": values, "ranking": ranking,
                              "top1_top2_margin": ranking[0][1]-ranking[1][1]})
    result = {"completed_at": datetime.now(UTC).isoformat(), "manifest_sha256": sha(manifest_path),
              "target_pcm_sha256": probe["pcm_sha256"], "summaries": summaries, "details": details,
              "model_calls": len(calls), "limits": manifest["protocol"]["interpretation"]}
    write(args.output / "results.json", result)
    audio_url = lambda path: "data:audio/wav;base64,"+base64.b64encode((args.output/path).read_bytes()).decode()
    header = '<!doctype html><meta charset="utf-8"><title>三人多参考声纹比较</title><style>body{font:16px system-ui;margin:32px}td,th{border:1px solid #ccc;padding:10px}table{border-collapse:collapse;margin-bottom:24px}audio{width:240px}</style>'
    body = '<h1>三人各5条参考，固定同一个null目标</h1><p>目标47.700–48.700秒。相似度不是概率；参考身份与状态来自既有解析证据。</p>'+f'<audio controls src="{audio_url(probe["audio"])}"></audio>'
    body += '<table><tr><th>片段</th><th>方式</th><th>爽世</th><th>灯</th><th>立希</th><th>前两名差</th></tr>'
    for s in summaries:
        body += f'<tr><td>{s["mode"]}</td><td>{s["method"]}</td>'+''.join(f'<td>{s["scores"][name]:.6f}</td>' for name in ("长崎爽世","高松灯","椎名立希"))+f'<td>{s["top1_top2_margin"]:.6f}</td></tr>'
    body += '</table><h2>参考与试听</h2><table><tr><th>人物/样本</th><th>台词</th><th>完整片段</th><th>首1.5秒</th></tr>'
    for row,ref in zip(rows,manifest["references"]):
        text = ' / '.join(t["text"] for t in ref["initial_event"]["text_variants"])
        body += f'<tr><td>{ref["speaker"]}<br>{ref["id"]}</td><td>{html.escape(text)}</td>'+''.join(f'<td><audio controls preload="none" src="{audio_url(row[mode]["audio"])}"></audio></td>' for mode in ("full","first1500"))+'</tr>'
    (args.output/"report.html").write_text(header+body+'</table><p>'+html.escape(result["limits"])+"</p>")
    print(json.dumps(summaries,ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
