from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta


@dataclass(frozen=True, slots=True)
class UploadServiceConfig:
    preferred_segment_size_bytes: int = 8 * 1024 * 1024
    maximum_upload_size_bytes: int = 10 * 1024 * 1024 * 1024
    session_ttl: timedelta = timedelta(hours=24)
    completion_stale_after: timedelta = timedelta(minutes=15)
    pipeline_version: str = "m2-seconds"
    max_job_attempts: int = 3

    def __post_init__(self) -> None:
        if self.preferred_segment_size_bytes <= 0:
            raise ValueError("preferred segment size must be greater than zero")
        if self.maximum_upload_size_bytes < self.preferred_segment_size_bytes:
            raise ValueError("maximum upload size cannot be smaller than a segment")
        if self.session_ttl <= timedelta(0):
            raise ValueError("upload session TTL must be positive")
        if self.completion_stale_after <= timedelta(0):
            raise ValueError("completion stale timeout must be positive")
        if not self.pipeline_version.strip():
            raise ValueError("pipeline version must not be empty")
        if self.max_job_attempts <= 0:
            raise ValueError("max job attempts must be greater than zero")
