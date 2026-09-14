from __future__ import annotations

from dataclasses import dataclass

from ..identity import canonical_uuid
from ..repository.job import (
    ParseJobRepository,
    ParseJobState,
    VideoChunkCheckpointState,
)


@dataclass(frozen=True, slots=True)
class ParseJobDetail:
    job: ParseJobState
    checkpoints: tuple[VideoChunkCheckpointState, ...]


class ParseJobService:
    def __init__(self, repository: ParseJobRepository) -> None:
        self._repository = repository

    def get(self, job_id: str) -> ParseJobDetail:
        canonical_job_id = canonical_uuid(job_id, "job_id")
        return ParseJobDetail(
            job=self._repository.get(canonical_job_id),
            checkpoints=tuple(
                self._repository.list_checkpoints(canonical_job_id)
            ),
        )

    def list(
        self,
        *,
        workspace_id: str,
        limit: int = 100,
    ) -> list[ParseJobDetail]:
        jobs = self._repository.list_for_workspace(
            workspace_id,
            limit=limit,
        )
        checkpoints = self._repository.list_checkpoints_for_jobs(
            [item.job_id for item in jobs]
        )
        return [
            ParseJobDetail(
                job=item,
                checkpoints=tuple(checkpoints[item.job_id]),
            )
            for item in jobs
        ]
