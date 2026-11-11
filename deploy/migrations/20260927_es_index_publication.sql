-- Seekino controlled migration: immutable ES build snapshots and publication.
-- HISTORICAL ONLY: these draft tables are retired by
-- 20260928_task_indexing_contract.sql. Do not apply this script to new databases.
-- The forward migration also works when none of these three tables exists.
-- Run only after 20260927_video_parse_v2_tasks.sql on a backed-up, quiescent
-- MySQL database. This creates no builds, changes no published video, and does
-- not remove Qdrant/V1 data. Application rollout and data cutover are separate.
-- Build rows intentionally have no FK to video/parse tasks: their snapshots and
-- deletion tombstone must survive physical source-row deletion until ES cleanup.
SET time_zone = '+00:00';

DELIMITER $$
DROP PROCEDURE IF EXISTS assert_es_index_publication_source$$
CREATE PROCEDURE assert_es_index_publication_source()
BEGIN
    DECLARE source_count INT DEFAULT 0;
    DECLARE target_count INT DEFAULT 0;
    SELECT COUNT(*) INTO source_count
    FROM information_schema.tables
    WHERE table_schema = DATABASE()
      AND table_name IN ('video', 'video_chunk', 'video_parse_task',
                         'chunk_parse_task', 'chunk_parse_content');
    SELECT COUNT(*) INTO target_count
    FROM information_schema.tables
    WHERE table_schema = DATABASE()
      AND table_name IN ('es_index_build', 'es_index_build_chunk',
                         'es_video_publication');
    IF source_count <> 5 OR target_count <> 0 THEN
        SIGNAL SQLSTATE '45000'
            SET MESSAGE_TEXT = 'ES publication migration requires V2 source tables and no partial ES publication tables';
    END IF;
END$$
CALL assert_es_index_publication_source()$$
DROP PROCEDURE assert_es_index_publication_source$$
DELIMITER ;

CREATE TABLE es_index_build (
    id VARCHAR(36) NOT NULL,
    workspace_id VARCHAR(64) NOT NULL,
    series_id VARCHAR(36) NOT NULL,
    video_id VARCHAR(36) NOT NULL,
    video_parse_task_id VARCHAR(36) NOT NULL,
    parse_task_version INTEGER NOT NULL,
    chunk_count INTEGER NOT NULL,
    index_name VARCHAR(255) NOT NULL,
    index_contract_version VARCHAR(128) NOT NULL,
    retrieval_contract_version VARCHAR(128) NOT NULL,
    created_at TIMESTAMP(3) NOT NULL,
    published_at TIMESTAMP(3) NULL,
    PRIMARY KEY (id),
    KEY ix_es_index_build_video (video_id, created_at),
    CONSTRAINT ck_es_index_build_parse_version CHECK (parse_task_version > 0),
    CONSTRAINT ck_es_index_build_chunk_count CHECK (chunk_count > 0)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE es_index_build_chunk (
    build_id VARCHAR(36) NOT NULL,
    video_chunk_id VARCHAR(36) NOT NULL,
    sequence_no INTEGER NOT NULL,
    start_seconds BIGINT NOT NULL,
    end_seconds BIGINT NOT NULL,
    chunk_parse_task_id VARCHAR(36) NOT NULL,
    content_version INTEGER NOT NULL,
    chunk_content_sha256 VARCHAR(64) NOT NULL,
    fact_snapshot JSON NOT NULL,
    fact_sha256 VARCHAR(64) NOT NULL,
    projection_snapshot JSON NOT NULL,
    projection_revision VARCHAR(64) NOT NULL,
    source_revision VARCHAR(64) NOT NULL,
    projection_version VARCHAR(128) NOT NULL,
    encoding_contract JSON NOT NULL,
    text_vector JSON NULL,
    video_vector JSON NOT NULL,
    text_vector_sha256 VARCHAR(64) NULL,
    video_vector_sha256 VARCHAR(64) NOT NULL,
    status VARCHAR(16) NOT NULL,
    generation INTEGER NOT NULL,
    attempt_count INTEGER NOT NULL,
    available_at TIMESTAMP(3) NOT NULL,
    lease_expires_at TIMESTAMP(3) NULL,
    query_proof JSON NULL,
    ready_at TIMESTAMP(3) NULL,
    last_error LONGTEXT NULL,
    created_at TIMESTAMP(3) NOT NULL,
    updated_at TIMESTAMP(3) NOT NULL,
    PRIMARY KEY (build_id, video_chunk_id),
    UNIQUE KEY uq_es_build_chunk_sequence (build_id, sequence_no),
    KEY ix_es_build_chunk_due (status, available_at),
    CONSTRAINT fk_es_build_chunk_build FOREIGN KEY (build_id)
        REFERENCES es_index_build(id) ON DELETE RESTRICT,
    CONSTRAINT ck_es_build_chunk_sequence CHECK (sequence_no >= 0),
    CONSTRAINT ck_es_build_chunk_range CHECK (start_seconds >= 0 AND end_seconds > start_seconds),
    CONSTRAINT ck_es_build_chunk_content_version CHECK (content_version > 0),
    CONSTRAINT ck_es_build_chunk_generation CHECK (generation >= 0),
    CONSTRAINT ck_es_build_chunk_attempts CHECK (attempt_count >= 0),
    CONSTRAINT ck_es_build_chunk_text_vector_pair CHECK (
        (text_vector IS NULL AND text_vector_sha256 IS NULL)
        OR (text_vector IS NOT NULL AND text_vector_sha256 IS NOT NULL)
    ),
    CONSTRAINT ck_es_build_chunk_status CHECK (status IN ('PENDING', 'WRITING', 'RETRY_WAIT', 'READY')),
    CONSTRAINT ck_es_build_chunk_ready_proof CHECK (
        (status = 'WRITING' AND lease_expires_at IS NOT NULL
         AND query_proof IS NULL AND ready_at IS NULL)
        OR (status = 'READY' AND lease_expires_at IS NULL
            AND query_proof IS NOT NULL AND ready_at IS NOT NULL)
        OR (status IN ('PENDING', 'RETRY_WAIT') AND lease_expires_at IS NULL
            AND query_proof IS NULL AND ready_at IS NULL)
    )
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE es_video_publication (
    video_id VARCHAR(36) NOT NULL,
    desired_build_id VARCHAR(36) NULL,
    published_build_id VARCHAR(36) NULL,
    fence BIGINT NOT NULL,
    is_deleted BOOLEAN NOT NULL,
    published_at TIMESTAMP(3) NULL,
    created_at TIMESTAMP(3) NOT NULL,
    updated_at TIMESTAMP(3) NOT NULL,
    PRIMARY KEY (video_id),
    KEY ix_es_video_publication_desired (desired_build_id),
    KEY ix_es_video_publication_published (published_build_id),
    CONSTRAINT fk_es_video_publication_desired FOREIGN KEY (desired_build_id)
        REFERENCES es_index_build(id) ON DELETE RESTRICT,
    CONSTRAINT fk_es_video_publication_published FOREIGN KEY (published_build_id)
        REFERENCES es_index_build(id) ON DELETE RESTRICT,
    CONSTRAINT ck_es_video_publication_fence CHECK (fence > 0),
    CONSTRAINT ck_es_video_publication_tombstone CHECK (
        (is_deleted = 1 AND desired_build_id IS NULL AND published_build_id IS NULL)
        OR (is_deleted = 0 AND desired_build_id IS NOT NULL)
    )
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
