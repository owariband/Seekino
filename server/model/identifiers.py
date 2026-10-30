from __future__ import annotations

from uuid import NAMESPACE_URL, uuid5

# Seekino uses its own UUID namespace. Existing libraries require an explicit
# identity migration; keep the historical video-segment entity label stable.
_VIDEO_CHUNK_ID_SEED = "seekino:video-segment"


def derive_series_id(tenant_id: str, series_name: str) -> str:
    return str(
        uuid5(
            NAMESPACE_URL,
            f"seekino:series:{tenant_id.strip()}:{series_name.strip().casefold()}",
        )
    )


def derive_video_id(series_id: str, content_hash: str) -> str:
    return str(uuid5(NAMESPACE_URL, f"seekino:video:{series_id}:{content_hash}"))


def derive_video_chunk_id(video_id: str, sequence_no: int) -> str:
    return str(
        uuid5(
            NAMESPACE_URL,
            f"{_VIDEO_CHUNK_ID_SEED}:{video_id}:{sequence_no}",
        )
    )


def derive_context_snapshot_id(video_id: str, sequence_no: int) -> str:
    return str(
        uuid5(
            NAMESPACE_URL,
            f"seekino:context-snapshot:{video_id}:{sequence_no}",
        )
    )


def derive_parse_job_id(video_id: str, pipeline_version: str) -> str:
    return str(
        uuid5(
            NAMESPACE_URL,
            f"seekino:series-parse-job:{video_id}:{pipeline_version.strip()}",
        )
    )
