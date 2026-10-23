"""Freeze existing EP04 audio cohorts and CAM++ vectors without model inference."""
from __future__ import annotations

from datetime import UTC, datetime
import hashlib
import json
import math
from pathlib import Path
import shutil
import subprocess
import time
import wave


OUTPUT = Path(__file__).resolve().parent
ROOT = OUTPUT.parents[3]
V11 = ROOT / "engine/experiment/voiceprint-agent/20261018-mygo-ep04-three-quarter-sampling/voiceprint-trace-20261018T002342.json"
BALANCED = ROOT / "engine/experiment/campplus/20261017-balanced-multiref"


def sha(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def pcm_info(path: Path) -> dict:
    with wave.open(str(path), "rb") as audio:
        assert (audio.getframerate(), audio.getnchannels(), audio.getsampwidth()) == (16000, 1, 2)
        frames = audio.getnframes()
        pcm = audio.readframes(frames)
    assert frames > 0 and len(pcm) == frames * 2
    return {"pcm_sha256": hashlib.sha256(pcm).hexdigest(), "frames": frames,
            "duration_ms": frames * 1000 / 16000, "wav_sha256": sha(path)}


def main() -> None:
    started = time.perf_counter()
    input_paths = {"v11_snapshot": V11, "balanced_manifest": BALANCED / "manifest.json",
                   "balanced_embeddings": BALANCED / "run1/embeddings.json"}
    data, inputs = {}, {}
    for name, path in input_paths.items():
        content = path.read_bytes()
        data[name] = json.loads(content)
        inputs[name] = {"path": str(path.relative_to(ROOT)),
                        "sha256": hashlib.sha256(content).hexdigest()}
    v11, balanced = data["v11_snapshot"], data["balanced_manifest"]
    old_vectors = {item["id"]: item for item in data["balanced_embeddings"]}
    assert v11["task"]["id"] == "0f681291-4f52-4254-a3be-91c1ee835c3c"
    assert v11["task"]["video_id"] == balanced["video_id"]
    source = ROOT / balanced["video_path"]
    video_hash = sha(source)  # Hash the full video once; per-sample checks use decoded PCM.
    assert video_hash == balanced["video_sha256"]
    (OUTPUT / "audio").mkdir(exist_ok=True)

    samples, embeddings, checks = [], [], []
    known_v11 = {(47500, 48800): "长崎爽世", (293500, 296000): "千早爱音"}
    for group_index, group in enumerate(v11["task"]["detail"]["voiceprint"]["voiceprintList"]):
        for ref_index, ref in enumerate(group["references"]):
            sample_id = f"v11_{len(samples):02d}"
            sample = {"id": sample_id, "cohort": "v11", "speaker": group["speaker"],
                      "target_role": "reference", "start_ms": ref["start_ms"], "end_ms": ref["end_ms"],
                      "audio": f"audio/{sample_id}.wav", "pcm_sha256": ref["pcm_sha256"],
                      "known_actual_speaker": known_v11.get((ref["start_ms"], ref["end_ms"])),
                      "source_ref": f'{ref["chunk_id"]}:{ref["event_fingerprint"]}',
                      "source": {"input": "v11_snapshot", "video_parse_task_id": v11["task"]["id"],
                                 "registry_group_index": group_index, "registry_reference_index": ref_index,
                                 **{key: value for key, value in ref.items() if key != "vector"}}}
            samples.append(sample)
            embeddings.append({"id": sample_id, "cohort": "v11", "model": group["model"]["name"],
                               "encoder_revision": group["model"]["encoder_revision"], "vector": ref["vector"],
                               "source_input": "v11_snapshot", "source_pointer":
                               f"task.detail.voiceprint.voiceprintList[{group_index}].references[{ref_index}].vector"})
    assert len(samples) == 31

    for original in [*balanced["references"], balanced["target"]]:
        original_id = original["id"]
        vector = old_vectors[original_id]
        assert (vector["start_ms"], vector["end_ms"]) == (original["start_ms"], original["end_ms"])
        is_probe = original_id == balanced["target"]["id"]
        expected = original.get("expected_pcm_sha256", vector["pcm_sha256"])
        assert expected == vector["pcm_sha256"]
        sample_id = f"balanced_{original_id}"
        samples.append({"id": sample_id, "cohort": "balanced",
                        "speaker": original.get("speaker", original.get("speaker_in_timeline")),
                        "target_role": "probe" if is_probe else "reference",
                        "start_ms": original["start_ms"], "end_ms": original["end_ms"],
                        "audio": f"audio/{sample_id}.wav", "pcm_sha256": expected,
                        "known_actual_speaker": original.get("user_confirmed_speaker") if is_probe else None,
                        "source": {"input": "balanced_manifest", "video_parse_task_id": balanced["video_parse_task_id"],
                                   "original_id": original_id, "original_audio":
                                   str((BALANCED / "run1" / vector["audio"]).relative_to(ROOT)),
                                   "original_audio_sha256": sha(BALANCED / "run1" / vector["audio"]),
                                   "original_record": original}})
        embeddings.append({"id": sample_id, "cohort": "balanced", "model": vector["model"],
                           "encoder_revision": vector["encoder_revision"], "vector": vector["vector"],
                           "source_input": "balanced_embeddings", "source_original_id": original_id})
    assert len(samples) == len(embeddings) == 47 and len({item["id"] for item in samples}) == 47

    for sample, embedding in zip(samples, embeddings, strict=True):
        audio_path = OUTPUT / sample["audio"]
        if sample["cohort"] == "v11":
            command = ["ffmpeg", "-v", "error", "-ss", f'{sample["start_ms"] / 1000:.3f}',
                       "-i", str(source), "-t", f'{(sample["end_ms"] - sample["start_ms"]) / 1000:.3f}',
                       "-map", "0:a:0", "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le",
                       "-y", str(audio_path)]
            if not audio_path.exists():
                subprocess.run(command, check=True, capture_output=True, timeout=30)
            method = "host_equivalent_ffmpeg"
        else:
            original_audio = ROOT / sample["source"]["original_audio"]
            if not audio_path.exists():
                shutil.copyfile(original_audio, audio_path)
            assert sha(audio_path) == sample["source"]["original_audio_sha256"]
            method = "copy_frozen_full_wav"
        actual = pcm_info(audio_path)
        assert actual["pcm_sha256"] == sample["pcm_sha256"], f'PCM differs for {sample["id"]}'
        assert actual["duration_ms"] == sample["end_ms"] - sample["start_ms"]
        vector = embedding["vector"]
        assert len(vector) == 192 and all(math.isfinite(value) for value in vector)
        assert abs(math.hypot(*vector) - 1) < 1e-6
        sample.update({"duration_ms": actual["duration_ms"], "frames": actual["frames"],
                       "wav_sha256": actual["wav_sha256"]})
        embedding.update({"pcm_sha256": sample["pcm_sha256"], "dimension": 192,
                          "duration_ms": actual["duration_ms"]})
        checks.append({"id": sample["id"], "method": method, "pcm_matches_frozen_source": True,
                       **actual})

    created = datetime.now(UTC).isoformat()
    manifest = {"created_at": created, "inputs": inputs,
                "video": {"id": balanced["video_id"], "path": balanced["video_path"], "sha256": video_hash},
                "audio_format": {"sample_rate_hz": 16000, "channels": 1, "codec": "pcm_s16le"},
                "cohort_policy": "Evaluate v11 and balanced separately; do not mix reference pools. Exclude self, overlapping audio and duplicate PCM. Existing labels are not global accuracy ground truth.",
                "known_identity_policy": "Only the two user-confirmed v11 mistakes and the balanced null target have known_actual_speaker; all other reference identities remain unverified.",
                "samples": samples}
    write_json(OUTPUT / "manifest.json", manifest)
    baseline_dir = OUTPUT / "campp_baseline"
    baseline_dir.mkdir(exist_ok=True)
    write_json(baseline_dir / "embeddings.json", {"created_at": created, "inference_performed": False,
               "provenance": inputs, "embeddings": embeddings})
    preparation = {"completed_at": created, "elapsed_seconds": time.perf_counter() - started,
                   "sample_count": len(samples), "v11_count": 31, "balanced_reference_count": 15,
                   "balanced_probe_count": 1, "source_video_hash_scans": 1,
                   "source_video_sha256": video_hash, "inputs": inputs,
                   "manifest_sha256": sha(OUTPUT / "manifest.json"),
                   "campp_baseline_sha256": sha(baseline_dir / "embeddings.json"),
                   "prepare_script_sha256": sha(Path(__file__)),
                   "ffmpeg_version": subprocess.check_output(["ffmpeg", "-version"], text=True).splitlines()[0],
                   "checks": checks, "new_model_inference_calls": 0,
                   "limits": "Experiment audio is intentionally retained. No production, service, database or registry writes. Frozen vectors were copied, not re-extracted."}
    write_json(OUTPUT / "preparation.json", preparation)
    print(json.dumps({"samples": len(samples), "v11_pcm_matches": 31, "copied_balanced_wav": 16,
                      "elapsed_seconds": preparation["elapsed_seconds"]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
