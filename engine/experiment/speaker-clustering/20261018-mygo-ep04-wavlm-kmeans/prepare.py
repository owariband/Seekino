"""Freeze every saved EP04 ASR cue for an offline speaker-clustering experiment."""
from __future__ import annotations

from array import array
from collections import Counter
from datetime import UTC, datetime
import hashlib
import json
import math
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import wave


OUTPUT = Path(__file__).resolve().parent
ROOT = OUTPUT.parents[3]
SNAPSHOT = ROOT / "engine/experiment/voiceprint-agent/20261017-mygo-ep04-visual-overlap-submit/voiceprint-source-audit-20261017T135330Z.json"
SNAPSHOT_SHA = "284e4a3292c4ded88ff4a625455e22599708bb0d5c3bf3e637ba8e0846061447"
FROZEN = ROOT / "engine/experiment/speaker-encoder/20261018-mygo-ep04-model-comparison/manifest.json"
FROZEN_SHA = "c766dceabd12aa5bd524614f9cf537f0641bf1b09852a9821dd4fe6a0419b9a4"
HOLDOUT_IDS = ("v11_02", "v11_27", "balanced_null_target")
HOLDOUT_RANGES = ((47500, 48800), (293500, 296000))
MUSIC_RANGES = {"op_range": (86000, 175600), "ed_range": (1260000, 1350500)}
RATE = 16000


def sha(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n")


def overlaps(start: int, end: int, other_start: int, other_end: int) -> bool:
    return start < other_end and other_start < end


def audio_info(path: Path) -> dict:
    with wave.open(str(path), "rb") as stream:
        assert (stream.getframerate(), stream.getnchannels(), stream.getsampwidth()) == (RATE, 1, 2)
        frames = stream.getnframes()
        pcm = stream.readframes(frames)
    assert frames > 0 and len(pcm) == frames * 2
    values = array("h", pcm)
    if sys.byteorder != "little":
        values.byteswap()
    rms = math.sqrt(sum(value * value for value in values) / frames) / 32768
    return {"frames": frames, "duration_ms": frames * 1000 / RATE,
            "pcm_sha256": hashlib.sha256(pcm).hexdigest(), "wav_sha256": sha(path),
            "rms_dbfs": max(-120.0, 20 * math.log10(rms)) if rms else -120.0}


def main() -> None:
    started = time.perf_counter()
    assert sha(SNAPSHOT) == SNAPSHOT_SHA and sha(FROZEN) == FROZEN_SHA
    snapshot, frozen = json.loads(SNAPSHOT.read_text()), json.loads(FROZEN.read_text())
    task = snapshot["task"]
    assert task["id"] == "56498828-8926-4ad7-beb4-b8a22d2b0b49" and task["version"] == 9
    assert task["video_id"] == frozen["video"]["id"]
    source = ROOT / frozen["video"]["path"]
    assert sha(source) == frozen["video"]["sha256"]
    chunks = sorted(snapshot["chunks"], key=lambda chunk: chunk["sequence_no"])
    assert [chunk["sequence_no"] for chunk in chunks] == list(range(24))
    cues, dialogues = [], []
    for chunk in chunks:
        evidence = chunk["detail"]["evidence"]
        assert evidence["media"]["video_id"] == task["video_id"]
        dialogues.extend(event for event in chunk["content"]["timeline"] if event["event_type"] == "DIALOGUE")
        for index, cue in enumerate(evidence["asr"]["transcript"]):
            start, end = cue["start_seconds"], cue["end_seconds"]
            assert isinstance(start, int) and isinstance(end, int)
            assert evidence["media"]["start_ms"] <= start < end <= evidence["media"]["end_ms"]
            cues.append({"cue_id": f"asr_c{chunk['sequence_no']:02d}_i{index:03d}",
                         "chunk_sequence": chunk["sequence_no"], "chunk_id": chunk["chunk_id"],
                         "content_version": chunk["version"], "asr_index": index,
                         "asr_speaker_id": cue["speaker_id"], "cue_start_ms": start,
                         "cue_end_ms": end, "text": cue["text"]})
    assert len(cues) == 378
    (OUTPUT / "audio").mkdir(exist_ok=True)
    samples, seen_pcm, cue_partition_checks = [], {}, []

    def finish_sample(sample: dict) -> None:
        sample["weak_speakers"] = sorted({event["speaker"] for event in dialogues
            if event.get("speaker") and overlaps(sample["start_ms"], sample["end_ms"],
                                                 event["start_seconds"], event["end_seconds"])})
        sample.update(audio_info(OUTPUT / sample["audio"]))
        assert sample["duration_ms"] == sample["end_ms"] - sample["start_ms"]
        reasons = sample["exclusion_reasons"]
        if sample["rms_dbfs"] < -50:
            reasons.append("silence_rms_below_minus50_dbfs")
        duplicate = seen_pcm.get(sample["pcm_sha256"])
        if duplicate is not None:
            reasons.append("duplicate_pcm")
            sample["duplicate_of_sample_id"] = duplicate
        else:
            seen_pcm[sample["pcm_sha256"]] = sample["id"]
        sample["fit_eligible"] = not reasons
        samples.append(sample)

    # Decode once so every ASR sample uses the same resampling context and integer PCM offsets.
    with tempfile.TemporaryDirectory(prefix="anivag-ep04-asr-") as temporary:
        decoded = Path(temporary) / "episode.s16le"
        command = ["ffmpeg", "-v", "error", "-nostdin", "-threads", "2", "-i", str(source),
                   "-map", "0:a:0", "-vn", "-ac", "1", "-ar", str(RATE), "-c:a", "pcm_s16le",
                   "-f", "s16le", "-y", str(decoded)]
        subprocess.run(command, check=True, capture_output=True, timeout=180)
        decoded_bytes = decoded.stat().st_size
        assert decoded_bytes % 2 == 0
        with decoded.open("rb") as stream:
            for cue in cues:
                start, end = cue["cue_start_ms"], cue["cue_end_ms"]
                duration = end - start
                parts = math.ceil(duration / 3000)
                boundaries = [start + duration * part // parts for part in range(parts + 1)]
                cue_reasons = []
                if any(overlaps(start, end, *interval) for interval in HOLDOUT_RANGES):
                    cue_reasons.append("holdout_related_cue")
                if any(other is not cue and overlaps(start, end, other["cue_start_ms"], other["cue_end_ms"])
                       for other in cues):
                    cue_reasons.append("cue_overlaps_other_asr")
                for part, (left, right) in enumerate(zip(boundaries, boundaries[1:])):
                    sample_id = f"{cue['cue_id']}_p{part:02d}"
                    relative_audio = f"audio/{sample_id}.wav"
                    reasons = list(cue_reasons)
                    if right - left < 1500:
                        reasons.append("shorter_than_1500_ms")
                    reasons.extend(name for name, interval in MUSIC_RANGES.items() if overlaps(left, right, *interval))
                    stream.seek(left * RATE // 1000 * 2)
                    pcm = stream.read((right - left) * RATE // 1000 * 2)
                    assert len(pcm) == (right - left) * 32 and 0 < right - left <= 3000
                    with wave.open(str(OUTPUT / relative_audio), "wb") as audio:
                        audio.setnchannels(1)
                        audio.setsampwidth(2)
                        audio.setframerate(RATE)
                        audio.writeframes(pcm)
                    finish_sample({"id": sample_id, "role": "asr", "audio": relative_audio,
                                   **cue, "part_index": part, "part_count": parts,
                                   "start_ms": left, "end_ms": right, "exclusion_reasons": reasons})
                assert boundaries[0] == start and boundaries[-1] == end
                cue_partition_checks.append({"cue_id": cue["cue_id"], "parts": parts,
                                             "duration_ms": duration, "complete_nonoverlapping_partition": True})
    assert not Path(temporary).exists()

    frozen_samples = {sample["id"]: sample for sample in frozen["samples"]}
    for identifier in HOLDOUT_IDS:
        original = frozen_samples[identifier]
        origin = FROZEN.parent / original["audio"]
        assert sha(origin) == original["wav_sha256"]
        relative_audio = f"audio/{identifier}.wav"
        shutil.copyfile(origin, OUTPUT / relative_audio)
        finish_sample({"id": identifier, "role": "holdout", "audio": relative_audio,
                       "cue_id": None, "chunk_sequence": None, "chunk_id": None, "asr_index": None,
                       "asr_speaker_id": None, "start_ms": original["start_ms"], "end_ms": original["end_ms"],
                       "cue_start_ms": original["start_ms"], "cue_end_ms": original["end_ms"],
                       "text": None, "known_actual_speaker": original["known_actual_speaker"],
                       "original_registry_speaker": original["speaker"], "exclusion_reasons": ["holdout"],
                       "source": {"manifest": str(FROZEN.relative_to(ROOT)), "id": identifier,
                                  "audio": str(origin.relative_to(ROOT)), "wav_sha256": original["wav_sha256"]}})
        assert samples[-1]["pcm_sha256"] == original["pcm_sha256"]
        assert samples[-1]["wav_sha256"] == original["wav_sha256"]

    inputs = {"asr_snapshot": {"path": str(SNAPSHOT.relative_to(ROOT)), "sha256": SNAPSHOT_SHA},
              "frozen_holdout_manifest": {"path": str(FROZEN.relative_to(ROOT)), "sha256": FROZEN_SHA}}
    rules = {"timestamp_units": "Global integer source-video milliseconds; legacy field names are unchanged.",
             "segmentation": "Each cue <=3000ms stays whole; longer cues use ceil(duration/3000) equal integer-ms partitions with floor boundaries.",
             "minimum_fit_duration_ms": 1500, "maximum_asr_sample_duration_ms": 3000,
             "music_exclusion_ranges_ms": MUSIC_RANGES, "holdout_ranges_ms": HOLDOUT_RANGES,
             "holdout_exclusion": "Every subsegment of any complete ASR cue overlapping either holdout range is excluded from fit.",
             "overlap_exclusion": "Every subsegment of a cue overlapping any other ASR cue is excluded from fit.",
             "silence_exclusion_rms_dbfs_below": -50, "rms_dbfs_floor": -120,
             "duplicates": "Only the first occurrence of identical PCM can be fit-eligible; later occurrences record duplicate_of_sample_id.",
             "weak_speakers": "Overlapping non-null Understanding DIALOGUE labels, metadata only; never used to select fit samples.",
             "all_samples_encoded": "Short, music, silence, overlap and holdout-related ASR samples remain in the manifest for encoding; fit_eligible controls fitting and representative sources only."}
    created = datetime.now(UTC).isoformat()
    summary = {"chunk_count": len(chunks), "asr_cue_count": len(cues),
               "asr_sample_count": sum(sample["role"] == "asr" for sample in samples),
               "holdout_count": len(HOLDOUT_IDS), "sample_count": len(samples),
               "fit_sample_count": sum(sample["fit_eligible"] for sample in samples),
               "exclusion_reason_counts": dict(Counter(reason for sample in samples for reason in sample["exclusion_reasons"])),
               "asr_cue_total_duration_ms": sum(cue["cue_end_ms"] - cue["cue_start_ms"] for cue in cues),
               "asr_sample_total_duration_ms": sum(sample["duration_ms"] for sample in samples if sample["role"] == "asr")}
    assert summary["asr_cue_total_duration_ms"] == summary["asr_sample_total_duration_ms"]
    manifest = {"created_at": created, "inputs": inputs, "video": frozen["video"],
                "source_task": {"id": task["id"], "version": task["version"], "status_at_capture": task["status"]},
                "audio_format": {"sample_rate_hz": RATE, "channels": 1, "codec": "pcm_s16le"},
                "protocol": rules, "summary": summary, "samples": samples}
    write_json(OUTPUT / "manifest.json", manifest)
    preparation = {"completed_at": created, "elapsed_seconds": time.perf_counter() - started,
                   "inputs": inputs, "summary": summary, "rules": rules,
                   "manifest_sha256": sha(OUTPUT / "manifest.json"), "prepare_script_sha256": sha(Path(__file__)),
                   "ffmpeg_version": subprocess.check_output(["ffmpeg", "-version"], text=True).splitlines()[0],
                   "decode_method": "One full-video raw PCM decode followed by exact 16-frame-per-ms slicing; holdouts are byte-for-byte copies of frozen WAVs.",
                   "decoded_pcm_bytes": decoded_bytes, "decode_invocations": 1, "temporary_audio_cleaned": True,
                   "cue_partition_checks": cue_partition_checks, "holdout_wav_and_pcm_hash_matches": len(HOLDOUT_IDS),
                   "new_model_inference_calls": 0, "limits": "Offline preparation only; no production, database, service or registry changes. Existing speaker labels are unverified metadata, not accuracy truth."}
    write_json(OUTPUT / "preparation.json", preparation)
    print(json.dumps({**summary, "elapsed_seconds": preparation["elapsed_seconds"],
                      "temporary_audio_cleaned": True, "model_inference_calls": 0}, ensure_ascii=False))


if __name__ == "__main__":
    main()
