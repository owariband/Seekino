-- AniVAG controlled migration: durable canonical VideoChunk ranges.
-- Run this before deploying code that requires the new checkpoint columns.

ALTER TABLE video_chunk_checkpoint
    ADD COLUMN chunk_start_ms BIGINT NULL AFTER stage,
    ADD COLUMN chunk_end_ms BIGINT NULL AFTER chunk_start_ms;

-- Persisted jobs already have an authoritative canonical range in video_chunk.
-- Match video_chunk_info.pipeline_version so an older Job cannot inherit the
-- ranges produced by a newer pipeline for the same Video.
-- Active jobs without video_chunk facts deliberately remain NULL and are
-- filled by the worker when their deterministic windows are loaded again.
UPDATE video_chunk_checkpoint AS checkpoint
JOIN series_parse_job AS job
  ON job.job_id = checkpoint.job_id
JOIN video_chunk AS chunk
  ON chunk.video_id = job.video_id
 AND chunk.sequence_no = checkpoint.sequence_no
JOIN video_chunk_info AS chunk_info
  ON chunk_info.video_chunk_id = chunk.video_chunk_id
 AND chunk_info.pipeline_version = job.pipeline_version
SET checkpoint.chunk_start_ms = chunk.start_ms,
    checkpoint.chunk_end_ms = chunk.end_ms
WHERE checkpoint.chunk_start_ms IS NULL
  AND checkpoint.chunk_end_ms IS NULL;

ALTER TABLE video_chunk_checkpoint
    ADD CONSTRAINT ck_video_chunk_checkpoint_range
    CHECK (
        (chunk_start_ms IS NULL AND chunk_end_ms IS NULL)
        OR (
            chunk_start_ms IS NOT NULL
            AND chunk_end_ms IS NOT NULL
            AND chunk_start_ms >= 0
            AND chunk_end_ms > chunk_start_ms
        )
    );

-- These verification queries must return no rows before application rollout.
SELECT job_id, sequence_no, chunk_start_ms, chunk_end_ms
FROM video_chunk_checkpoint
WHERE (chunk_start_ms IS NULL) <> (chunk_end_ms IS NULL)
   OR chunk_start_ms < 0
   OR chunk_end_ms <= chunk_start_ms;

WITH ordered_checkpoint AS (
    SELECT checkpoint.job_id,
           checkpoint.sequence_no,
           checkpoint.chunk_start_ms,
           checkpoint.chunk_end_ms,
           ROW_NUMBER() OVER (
               PARTITION BY checkpoint.job_id
               ORDER BY checkpoint.sequence_no
           ) - 1 AS expected_sequence_no,
           LAG(checkpoint.chunk_end_ms, 1, 0) OVER (
               PARTITION BY checkpoint.job_id
               ORDER BY checkpoint.sequence_no
           ) AS expected_start_ms
    FROM video_chunk_checkpoint AS checkpoint
    WHERE checkpoint.chunk_start_ms IS NOT NULL
      AND checkpoint.chunk_end_ms IS NOT NULL
)
SELECT job_id, sequence_no, chunk_start_ms, chunk_end_ms
FROM ordered_checkpoint
WHERE sequence_no <> expected_sequence_no
   OR chunk_start_ms <> expected_start_ms;

SELECT checkpoint.job_id,
       MIN(checkpoint.chunk_start_ms) AS first_start_ms,
       MAX(checkpoint.chunk_end_ms) AS last_end_ms,
       SUM(checkpoint.chunk_end_ms - checkpoint.chunk_start_ms) AS covered_ms,
       video.duration_ms
FROM video_chunk_checkpoint AS checkpoint
JOIN series_parse_job AS job
  ON job.job_id = checkpoint.job_id
JOIN video
  ON video.video_id = job.video_id
WHERE checkpoint.chunk_start_ms IS NOT NULL
  AND checkpoint.chunk_end_ms IS NOT NULL
GROUP BY checkpoint.job_id, video.duration_ms
HAVING first_start_ms <> 0
    OR last_end_ms <> video.duration_ms
    OR covered_ms <> video.duration_ms;
