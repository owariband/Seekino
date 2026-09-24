from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from engine.parsev1.analysis_windows import probe_video_duration_seconds
from engine.parsev1.schemas import VideoTimeline

_MAX_PROMPT_CHARACTERS = 8000
_MAX_TARGET_CHARACTERS = 8000


@dataclass(frozen=True, slots=True)
class ValidationReport:
    sample_count: int
    train_count: int
    validation_count: int
    errors: tuple[str, ...]

    @property
    def valid(self) -> bool:
        return not self.errors


def validate_dataset(
    dataset_directory: Path,
    *,
    probe_videos: bool = False,
) -> ValidationReport:
    root = dataset_directory.expanduser().resolve()
    errors: list[str] = []
    split_records: dict[str, list[dict[str, Any]]] = {}
    for split in ("train", "validation"):
        path = root / f"{split}.jsonl"
        try:
            split_records[split] = _read_jsonl(path)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            errors.append(f"{path.name}: {exc}")
            split_records[split] = []

    manifest_path = root / "manifest.jsonl"
    try:
        manifest = _read_jsonl(manifest_path)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        errors.append(f"{manifest_path.name}: {exc}")
        manifest = []

    manifest_by_video = _validate_manifest(manifest, split_records, errors)
    all_video_references: list[str] = []
    for split, records in split_records.items():
        if not records:
            errors.append(f"{split}.jsonl: split must not be empty")
        for index, record in enumerate(records, start=1):
            prefix = f"{split}.jsonl:{index}"
            video_reference, target = _validate_swift_record(record, prefix, errors)
            if video_reference is None:
                continue
            all_video_references.append(video_reference)
            metadata = manifest_by_video.get(video_reference)
            if metadata is None:
                errors.append(f"{prefix}: no matching manifest row")
            elif target is not None:
                expected_duration_seconds = metadata.get("analysis_duration_seconds")
                if isinstance(expected_duration_seconds, int):
                    for event_index, event in enumerate(target.events):
                        if event.end_seconds > expected_duration_seconds:
                            errors.append(
                                f"{prefix}: event {event_index} exceeds "
                                f"analysis duration {expected_duration_seconds} seconds"
                            )
            video_path = root / video_reference
            if not video_path.is_file() or video_path.stat().st_size == 0:
                errors.append(f"{prefix}: missing or empty video {video_reference!r}")
                continue
            if probe_videos:
                try:
                    actual_duration_seconds = probe_video_duration_seconds(video_path)
                except (OSError, RuntimeError, ValueError) as exc:
                    errors.append(f"{prefix}: video probe failed: {exc}")
                else:
                    expected_duration_seconds = (
                        metadata.get("analysis_duration_seconds")
                        if metadata is not None
                        else None
                    )
                    if (
                        isinstance(expected_duration_seconds, int)
                        and abs(actual_duration_seconds - expected_duration_seconds) > 1
                    ):
                        errors.append(
                            f"{prefix}: video duration {actual_duration_seconds} seconds "
                            f"differs from manifest {expected_duration_seconds} seconds"
                        )

    if len(all_video_references) != len(set(all_video_references)):
        errors.append("the same materialized video is referenced more than once")
    return ValidationReport(
        sample_count=sum(len(records) for records in split_records.values()),
        train_count=len(split_records["train"]),
        validation_count=len(split_records["validation"]),
        errors=tuple(errors),
    )


def _validate_swift_record(
    record: dict[str, Any],
    prefix: str,
    errors: list[str],
) -> tuple[str | None, VideoTimeline | None]:
    messages = record.get("messages")
    videos = record.get("videos")
    if not isinstance(messages, list) or len(messages) != 2:
        errors.append(f"{prefix}: messages must contain one user and one assistant")
        return None, None
    if [message.get("role") for message in messages if isinstance(message, dict)] != [
        "user",
        "assistant",
    ]:
        errors.append(f"{prefix}: message roles must be user, assistant")
        return None, None
    user_content = messages[0].get("content")
    assistant_content = messages[1].get("content")
    if not isinstance(user_content, str) or user_content.count("<video>") != 1:
        errors.append(f"{prefix}: user content must contain exactly one <video>")
    elif len(user_content) > _MAX_PROMPT_CHARACTERS:
        errors.append(
            f"{prefix}: prompt has {len(user_content)} characters; compact the context"
        )
    target = None
    if not isinstance(assistant_content, str):
        errors.append(f"{prefix}: assistant content must be a JSON string")
    else:
        if len(assistant_content) > _MAX_TARGET_CHARACTERS:
            errors.append(
                f"{prefix}: target has {len(assistant_content)} characters"
            )
        try:
            target = VideoTimeline.model_validate_json(assistant_content)
        except ValueError as exc:
            errors.append(f"{prefix}: invalid VideoTimeline target: {exc}")
    if not isinstance(videos, list) or len(videos) != 1:
        errors.append(f"{prefix}: videos must contain exactly one path")
        return None, target
    video_reference = videos[0]
    if not isinstance(video_reference, str) or not video_reference.strip():
        errors.append(f"{prefix}: video path must be a non-empty string")
        return None, target
    if Path(video_reference).is_absolute() or ".." in Path(video_reference).parts:
        errors.append(f"{prefix}: video path must stay inside the dataset directory")
        return None, target
    return video_reference, target


def _validate_manifest(
    manifest: list[dict[str, Any]],
    split_records: dict[str, list[dict[str, Any]]],
    errors: list[str],
) -> dict[str, dict[str, Any]]:
    expected_count = sum(len(records) for records in split_records.values())
    if len(manifest) != expected_count:
        errors.append(
            f"manifest row count {len(manifest)} does not match dataset row count {expected_count}"
        )
    sample_ids: set[str] = set()
    manifest_by_video: dict[str, dict[str, Any]] = {}
    split_video_ids: dict[str, set[str]] = {"train": set(), "validation": set()}
    for index, record in enumerate(manifest, start=1):
        prefix = f"manifest.jsonl:{index}"
        sample_id = record.get("sample_id")
        split = record.get("split")
        video_id = record.get("video_id")
        video_reference = record.get("video")
        if not isinstance(sample_id, str) or not sample_id:
            errors.append(f"{prefix}: sample_id is required")
        elif sample_id in sample_ids:
            errors.append(f"{prefix}: duplicate sample_id {sample_id!r}")
        else:
            sample_ids.add(sample_id)
        if split not in split_video_ids:
            errors.append(f"{prefix}: split must be train or validation")
        elif not isinstance(video_id, str) or not video_id:
            errors.append(f"{prefix}: video_id is required")
        else:
            split_video_ids[split].add(video_id)
        if record.get("teacher_provider") != "doubao":
            errors.append(f"{prefix}: teacher_provider must be doubao")
        if record.get("teacher_input_mode") != "video":
            errors.append(f"{prefix}: teacher_input_mode must be video")
        if record.get("is_fallback") is not False:
            errors.append(f"{prefix}: fallback samples are not accepted")
        if not isinstance(video_reference, str) or not video_reference:
            errors.append(f"{prefix}: video is required")
        elif video_reference in manifest_by_video:
            errors.append(f"{prefix}: duplicate video {video_reference!r}")
        else:
            manifest_by_video[video_reference] = record
        duration_seconds = record.get("analysis_duration_seconds")
        if (
            isinstance(duration_seconds, bool)
            or not isinstance(duration_seconds, int)
            or duration_seconds <= 0
        ):
            errors.append(f"{prefix}: analysis_duration_seconds must be positive")

    leaked_video_ids = split_video_ids["train"].intersection(
        split_video_ids["validation"]
    )
    if leaked_video_ids:
        errors.append(
            "episode leakage across train and validation: "
            + ", ".join(sorted(leaked_video_ids))
        )
    return manifest_by_video


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as stream:
        for line_number, line in enumerate(stream, start=1):
            if not line.strip():
                continue
            value = json.loads(line)
            if not isinstance(value, dict):
                raise ValueError(f"line {line_number} must be a JSON object")
            records.append(value)
    return records


def main() -> None:
    parser = argparse.ArgumentParser(description="Validate a Qwen3-VL dataset bundle.")
    parser.add_argument("dataset_directory", type=Path)
    parser.add_argument("--probe-videos", action="store_true")
    args = parser.parse_args()
    report = validate_dataset(
        args.dataset_directory,
        probe_videos=args.probe_videos,
    )
    print(
        json.dumps(
            {
                "valid": report.valid,
                "sample_count": report.sample_count,
                "train_count": report.train_count,
                "validation_count": report.validation_count,
                "errors": report.errors,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    if not report.valid:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
