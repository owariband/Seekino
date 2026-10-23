"""Snapshot the published MyGO source/Event metadata without mutating services."""
import json
import logging
import subprocess
import sys
from collections import Counter
from datetime import datetime, timezone
from fractions import Fraction
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT))

from sqlalchemy import select

from engine.parsev2.schemas import VideoParseResult
from engine.rag.indexing.events import compute_event_id
from scripts.local_services import configure
from server.caller.elasticsearch import open_elasticsearch_client
from server.caller.mysql import open_database_storage
from server.model.job import ChunkParseContent, ChunkParseTask
from server.model.video import Video, VideoChunk
from server.repository.video_parse import VideoParseRepository
from server.storage.artifact import LocalArtifactStore

OUT = Path(__file__).resolve().parent
SERIES = "836fa9f0-2bf4-50fb-9c64-75526fa1dea9"
TENANT = "local-default"


def main():
    logging.disable(logging.CRITICAL)
    for name in ("sources.json", "events.jsonl"):
        if (OUT / name).exists():
            raise RuntimeError(f"Refusing to replace existing {name}")
    settings = configure()
    artifacts = LocalArtifactStore(
        settings.artifact_root, environment=settings.environment,
        create_base_directory=False,
    )
    with open_database_storage(
        settings.database_url, environment=settings.environment,
        pool_recycle_seconds=3600,
    ) as (_, sessions), open_elasticsearch_client(settings.elasticsearch) as es:
        repo = VideoParseRepository(sessions)
        scope = repo.resolve_retrieval_scope([SERIES], tenant_id=TENANT)
        assert not scope.partial and len(scope.searched_video_ids) == 13
        index = settings.elasticsearch.event_index_for_environment(settings.environment)
        mapping = es.indices.get_mapping(index=index)[index]["mappings"]
        contract_id = mapping["_meta"]["event_contract_id"]
        published = set(repo.validate_event_coverage(
            scope, index_name=index, contract_id=contract_id,
        ))
        episodes, events = [], []
        with sessions() as db:
            videos = list(db.scalars(select(Video).where(
                Video.id.in_(scope.searched_video_ids),
            ).order_by(Video.episode_order)))
            by_video = {v.id: v for v in videos}
            assert [v.episode_order for v in videos] == list(range(1, 14))
            for video in videos:
                path = artifacts.resolve(video.source_uri)
                assert path.is_file(), path
                probe = json.loads(subprocess.check_output([
                    "ffprobe", "-v", "error", "-select_streams", "v:0",
                    "-show_entries", "stream=width,height,r_frame_rate,avg_frame_rate,nb_frames,duration:format=duration:chapter",
                    "-of", "json", str(path),
                ]))
                stream = probe["streams"][0]
                episodes.append(dict(
                    episode=video.episode_order, video_id=video.id,
                    parse_task_id=scope.parse_tasks_by_video_id[video.id],
                    path=str(path), source_uri=video.source_uri,
                    duration_ms=video.duration_ms, source_sha256=video.hash,
                    size_bytes=path.stat().st_size,
                    width=int(stream["width"]), height=int(stream["height"]),
                    fps=float(Fraction(stream["avg_frame_rate"])),
                    fps_fraction=stream["avg_frame_rate"],
                    frame_count=int(stream["nb_frames"]) if stream.get("nb_frames", "").isdigit() else None,
                    probe_duration_seconds=float(probe["format"]["duration"]),
                    chapters=probe.get("chapters", []),
                ))
            rows = db.execute(select(
                ChunkParseTask.video_task_id, VideoChunk.id, VideoChunk.video_id,
                VideoChunk.start_ms, VideoChunk.end_ms, ChunkParseContent.version,
                ChunkParseContent.content,
            ).join(VideoChunk, VideoChunk.id == ChunkParseTask.chunk_id).join(
                ChunkParseContent, ChunkParseContent.chunk_task_id == ChunkParseTask.id,
            ).where(ChunkParseTask.video_task_id.in_(tuple(scope.parse_tasks_by_video_id.values()))))
            for task_id, chunk_id, video_id, chunk_start, chunk_end, version, content in rows:
                assert (task_id, chunk_id, version) in published
                result = VideoParseResult.model_validate(content)
                for timeline_index, event in enumerate(result.timeline):
                    if event.start_seconds >= chunk_end or event.end_seconds <= chunk_start:
                        continue
                    events.append(dict(
                        event_id=compute_event_id(task_id, chunk_id, version, timeline_index),
                        episode=by_video[video_id].episode_order, video_id=video_id,
                        parse_task_id=task_id, video_chunk_id=chunk_id,
                        version=version, timeline_index=timeline_index,
                        start_ms=event.start_seconds, end_ms=event.end_seconds,
                        capture_start_ms=max(chunk_start, event.start_seconds),
                        capture_end_ms=min(chunk_end, event.end_seconds),
                        chunk_start_ms=chunk_start, chunk_end_ms=chunk_end,
                        event_type=event.event_type, description=event.description,
                    ))
        response = es.search(
            index=index, size=10000, track_total_hits=True,
            query={"terms": {"parse_task_id": list(scope.parse_tasks_by_video_id.values())}},
            source_includes=["parse_task_id", "video_chunk_id", "version", "timeline_index", "event_type"],
        )
        assert not response.get("timed_out") and response["_shards"]["failed"] == 0
        hits = response["hits"]["hits"]
        assert response["hits"]["total"] == {"value": len(hits), "relation": "eq"}
        actual = {hit["_id"]: hit["_source"] for hit in hits}
        expected = {row["event_id"]: row for row in events}
        assert len(expected) == len(events) and actual.keys() == expected.keys()
        assert len(events) == 7103, f"Expected current 7103 Event publication, got {len(events)}"
        for event_id, source in actual.items():
            assert all(expected[event_id][key] == value for key, value in source.items())
        assert published == set(repo.validate_event_coverage(
            scope, index_name=index, contract_id=contract_id,
        )), "Publication changed while taking snapshot"

    events.sort(key=lambda row: (row["episode"], row["start_ms"], row["end_ms"], row["event_id"]))
    counts = Counter(row["episode"] for row in events)
    for episode in episodes:
        episode["event_count"] = counts[episode["episode"]]
    snapshot = dict(
        created_at=datetime.now(timezone.utc).isoformat(), series_id=SERIES,
        tenant_id=TENANT, capture_height=scope.parse_config.visual_input.max_height,
        event_count=len(events), chunk_count=len(published), event_index_name=index,
        event_contract_id=contract_id, index_generation=scope.index_generation,
        time_unit="milliseconds", interval_convention="half-open [start_ms, end_ms)",
        event_verification="Exact deterministic IDs and source identity fields reconciled against Elasticsearch; publication checked before and after snapshot.",
        episodes=episodes,
    )
    (OUT / "events.jsonl").write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in events))
    (OUT / "sources.json").write_text(json.dumps(snapshot, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(dict(episodes=len(episodes), events=len(events), chunks=len(published), capture_height=snapshot["capture_height"], events_per_episode=counts), ensure_ascii=False))


if __name__ == "__main__":
    main()
