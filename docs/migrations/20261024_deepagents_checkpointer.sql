-- Deep Agents / LangGraph checkpoint schema for langgraph-checkpoint-mysql 3.0.0.
-- Fresh installation only. Do not run against an existing checkpoint_migrations table.
-- Repeatable upgrades should use `python -m server.cli init-db`, which calls PyMySQLSaver.setup().
-- This script is not executed by the application automatically.

CREATE TABLE IF NOT EXISTS checkpoint_migrations (
    v INTEGER PRIMARY KEY
);

CREATE TABLE IF NOT EXISTS checkpoints (
    thread_id VARCHAR(150) NOT NULL,
    checkpoint_ns VARCHAR(2000) NOT NULL DEFAULT '',
    checkpoint_id VARCHAR(150) NOT NULL,
    parent_checkpoint_id VARCHAR(150),
    type VARCHAR(150),
    checkpoint JSON NOT NULL,
    metadata JSON NOT NULL DEFAULT ('{}'),
    checkpoint_ns_hash BINARY(16) AS (UNHEX(MD5(checkpoint_ns))) STORED,
    PRIMARY KEY (thread_id, checkpoint_ns_hash, checkpoint_id)
);

CREATE TABLE IF NOT EXISTS checkpoint_blobs (
    thread_id VARCHAR(150) NOT NULL,
    checkpoint_ns VARCHAR(2000) NOT NULL DEFAULT '',
    channel VARCHAR(150) NOT NULL,
    version VARCHAR(150) NOT NULL,
    type VARCHAR(150) NOT NULL,
    `blob` LONGBLOB,
    checkpoint_ns_hash BINARY(16) AS (UNHEX(MD5(checkpoint_ns))) STORED,
    PRIMARY KEY (thread_id, checkpoint_ns_hash, channel, version)
);

CREATE TABLE IF NOT EXISTS checkpoint_writes (
    thread_id VARCHAR(150) NOT NULL,
    checkpoint_ns VARCHAR(2000) NOT NULL DEFAULT '',
    checkpoint_id VARCHAR(150) NOT NULL,
    task_id VARCHAR(150) NOT NULL,
    idx INTEGER NOT NULL,
    channel VARCHAR(150) NOT NULL,
    type VARCHAR(150),
    `blob` LONGBLOB NOT NULL,
    task_path VARCHAR(2000) NOT NULL DEFAULT '',
    checkpoint_ns_hash BINARY(16) AS (UNHEX(MD5(checkpoint_ns))) STORED,
    PRIMARY KEY (thread_id, checkpoint_ns_hash, checkpoint_id, task_id, idx)
);

CREATE INDEX checkpoints_thread_id_idx ON checkpoints (thread_id);
CREATE INDEX checkpoint_blobs_thread_id_idx ON checkpoint_blobs (thread_id);
CREATE INDEX checkpoint_writes_thread_id_idx ON checkpoint_writes (thread_id);
CREATE INDEX checkpoints_checkpoint_id_idx ON checkpoints (checkpoint_id);

-- Mark all upstream migrations through version 21 as applied.
INSERT INTO checkpoint_migrations (v) VALUES
    (0), (1), (2), (3), (4), (5), (6), (7), (8), (9), (10),
    (11), (12), (13), (14), (15), (16), (17), (18), (19), (20), (21);
