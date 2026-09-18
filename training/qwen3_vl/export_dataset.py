from __future__ import annotations

import argparse
import json
import os
import shutil
import tempfile
from collections.abc import Iterable, Sequence
from pathlib import Path

from engine.parse.schemas import VideoChunkCheckpoint
from server.database import (
    DatabaseEnvironment,
    create_database_engine,
    create_session_factory,
)
from server.model.job import ParseJobStatus, VideoChunkCheckpointStage
from server.repository.job import ParseJobRepository
from server.storage.artifact import LocalArtifactStore

from .dataset import build_manifest_record, build_swift_record

_PARSED_STAGE_RANK = tuple(VideoChunkCheckpointStage).index(
    VideoChunkCheckpointStage.PARSED
)


def export_dataset(
    *,
    database_url: str,
    artifact_root: Path,
    environment: DatabaseEnvironment,
    train_job_ids: Sequence[str],
    validation_job_ids: Sequence[str],
    output_directory: Path,
    overwrite: bool = False,
) -> dict[str, int]:
    if not train_job_ids:
        raise ValueError("at least one train job is required")
    if not validation_job_ids:
        raise ValueError("at least one validation job is required")
    overlap = set(train_job_ids).intersection(validation_job_ids)
    if overlap:
        raise ValueError(
            "a parse job cannot appear in both splits: " + ", ".join(sorted(overlap))
        )

    destination = output_directory.expanduser().resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists() and not overwrite:
        raise FileExistsError(
            f"dataset directory already exists: {destination}; pass --overwrite"
        )

    engine = create_database_engine(database_url, environment=environment)
    repository = ParseJobRepository(create_session_factory(engine))
    artifacts = LocalArtifactStore(
        artifact_root,
        environment=environment,
        create_base_directory=False,
    )
    staging = Path(
        tempfile.mkdtemp(prefix=f".{destination.name}-", dir=destination.parent)
    )
    try:
        counts = {"train": 0, "validation": 0, "skipped": 0}
        manifest: list[dict[str, object]] = []
        for split, job_ids in (
            ("train", train_job_ids),
            ("validation", validation_job_ids),
        ):
            output = staging / f"{split}.jsonl"
            with output.open("w", encoding="utf-8") as stream:
                for job_id in job_ids:
                    records, metadata, skipped = _export_job(
                        repository,
                        artifacts,
                        job_id=job_id,
                        split=split,
                        dataset_directory=staging,
                    )
                    counts[split] += len(records)
                    counts["skipped"] += skipped
                    manifest.extend(metadata)
                    for record in records:
                        stream.write(json.dumps(record, ensure_ascii=False) + "\n")

        _write_jsonl(staging / "manifest.jsonl", manifest)
        (staging / "summary.json").write_text(
            json.dumps(counts, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        if destination.exists():
            shutil.rmtree(destination)
        staging.replace(destination)
        return counts
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    finally:
        engine.dispose()


def _export_job(
    repository: ParseJobRepository,
    artifacts: LocalArtifactStore,
    *,
    job_id: str,
    split: str,
    dataset_directory: Path,
) -> tuple[list[dict[str, object]], list[dict[str, object]], int]:
    job = repository.get(job_id)
    if job.status != ParseJobStatus.SUCCEEDED.value:
        raise ValueError(f"job {job_id} is not SUCCEEDED: {job.status}")

    checkpoints = repository.list_checkpoints(job_id)
    series_bible = _load_series_bible(artifacts.work_directory(job_id))
    records: list[dict[str, object]] = []
    manifest: list[dict[str, object]] = []
    skipped = 0
    for state in checkpoints:
        stage_rank = tuple(VideoChunkCheckpointStage).index(
            VideoChunkCheckpointStage(state.stage)
        )
        if stage_rank < _PARSED_STAGE_RANK:
            skipped += 1
            continue
        if not state.analysis_window_media_uri or not state.parse_artifact_uri:
            raise ValueError(
                f"job {job_id} checkpoint {state.sequence_no} has no committed media or parse artifact"
            )

        checkpoint = VideoChunkCheckpoint.model_validate_json(
            artifacts.resolve(state.parse_artifact_uri).read_text(encoding="utf-8")
        )
        if checkpoint.chunk.window.sequence_no != state.sequence_no:
            raise ValueError(
                f"job {job_id} checkpoint {state.sequence_no} artifact identity mismatch"
            )
        provenance = checkpoint.chunk.video_parse
        if (
            provenance is None
            or provenance.provider != "doubao"
            or provenance.input_mode != "video"
            or checkpoint.chunk.is_fallback
        ):
            skipped += 1
            continue

        source_video = artifacts.resolve(state.analysis_window_media_uri)
        extension = source_video.suffix.casefold() or ".mp4"
        relative_video = Path("videos") / job_id / (
            f"window-{state.sequence_no:04d}{extension}"
        )
        target_video = dataset_directory / relative_video
        _materialize_media(source_video, target_video)

        sample_id = f"{job.video_id}:{state.sequence_no:04d}"
        video_reference = relative_video.as_posix()
        fingerprint = artifacts.fingerprint(state.analysis_window_media_uri)
        records.append(
            build_swift_record(
                checkpoint,
                video_reference=video_reference,
                series_bible=series_bible,
            )
        )
        manifest.append(
            build_manifest_record(
                checkpoint,
                sample_id=sample_id,
                split=split,
                job_id=job_id,
                series_id=job.series_id,
                video_id=job.video_id,
                episode_order=job.episode_order,
                video_reference=video_reference,
                video_sha256=fingerprint.sha256,
                parse_artifact_uri=state.parse_artifact_uri,
            )
        )
    if not records:
        raise ValueError(f"job {job_id} produced no cloud-video training samples")
    return records, manifest, skipped


def _load_series_bible(work_directory: Path) -> str | None:
    snapshot = work_directory / "background.snapshot.json"
    if not snapshot.is_file():
        return None
    payload = json.loads(snapshot.read_text(encoding="utf-8"))
    value = payload.get("series_bible") if isinstance(payload, dict) else None
    if value is not None and not isinstance(value, str):
        raise ValueError(f"invalid series_bible in {snapshot}")
    return value


def _materialize_media(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.link(source, target)
    except OSError:
        shutil.copy2(source, target)


def _write_jsonl(path: Path, records: Iterable[dict[str, object]]) -> None:
    with path.open("w", encoding="utf-8") as stream:
        for record in records:
            stream.write(json.dumps(record, ensure_ascii=False) + "\n")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Export committed AniVAG parse checkpoints as Qwen3-VL SFT data."
    )
    parser.add_argument("--train-job", action="append", required=True)
    parser.add_argument("--validation-job", action="append", required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument(
        "--database-url",
        default=os.getenv("ANIVAG_DATABASE_URL"),
    )
    parser.add_argument(
        "--artifact-root",
        type=Path,
        default=Path(os.getenv("ANIVAG_ARTIFACT_ROOT", "outputs/server/artifacts")),
    )
    parser.add_argument(
        "--environment",
        type=DatabaseEnvironment,
        choices=tuple(DatabaseEnvironment),
        default=DatabaseEnvironment(
            os.getenv("ANIVAG_ENVIRONMENT", DatabaseEnvironment.DEVELOPMENT.value)
        ),
    )
    parser.add_argument("--overwrite", action="store_true")
    return parser


def main() -> None:
    args = _parser().parse_args()
    if not args.database_url:
        raise SystemExit("--database-url or ANIVAG_DATABASE_URL is required")
    counts = export_dataset(
        database_url=args.database_url,
        artifact_root=args.artifact_root,
        environment=args.environment,
        train_job_ids=args.train_job,
        validation_job_ids=args.validation_job,
        output_directory=args.output_dir,
        overwrite=args.overwrite,
    )
    print(json.dumps(counts, ensure_ascii=False))


if __name__ == "__main__":
    main()
