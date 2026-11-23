"""Rebuildable desktop search projections in the library's own SQLite database."""

from __future__ import annotations

import json
import re
import sqlite3
import struct
from collections.abc import Mapping, Sequence
from typing import Literal

from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from engine.rag import VideoChunkIndexDocument
from engine.rag.encoding import validate_embedding_vector
from engine.rag.indexing.events import EventIndexDocument, EventIndexingContract, EventIndexReceipt, compute_event_id
from engine.rag.retrieval.contracts import EventFilters, EventIndexNotReadyError, RawEventHit

from ..utils.validation import canonical_uuid, require_nonempty_text
from .index_contract import IndexDocumentConsistencyError, IndexSchemaError


_SCHEMA = (
    """CREATE TABLE IF NOT EXISTS local_index_contract (
        singleton INTEGER PRIMARY KEY CHECK(singleton=1), name TEXT NOT NULL, contract_id TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS local_chunks (
        task TEXT NOT NULL, chunk TEXT NOT NULL, version INTEGER NOT NULL, generation INTEGER NOT NULL,
        event_count INTEGER NOT NULL, document TEXT NOT NULL, PRIMARY KEY(task,chunk)
    )""",
    """CREATE TABLE IF NOT EXISTS local_events (
        id TEXT PRIMARY KEY NOT NULL, task TEXT NOT NULL, chunk TEXT NOT NULL, version INTEGER NOT NULL,
        timeline_index INTEGER NOT NULL, event_type TEXT NOT NULL, speaker TEXT, lexical_fields TEXT NOT NULL,
        FOREIGN KEY(task,chunk) REFERENCES local_chunks(task,chunk) ON DELETE CASCADE
    )""",
    "CREATE INDEX IF NOT EXISTS local_event_source ON local_events(task,chunk,version)",
    """CREATE TABLE IF NOT EXISTS local_event_vectors (
        event_id TEXT NOT NULL REFERENCES local_events(id) ON DELETE CASCADE, channel TEXT NOT NULL,
        position INTEGER NOT NULL, vector BLOB NOT NULL, PRIMARY KEY(event_id,channel,position)
    )""",
    """CREATE TABLE IF NOT EXISTS local_event_fields (
        rowid INTEGER PRIMARY KEY, event_id TEXT NOT NULL REFERENCES local_events(id) ON DELETE CASCADE,
        position INTEGER NOT NULL, tokens TEXT NOT NULL, UNIQUE(event_id,position)
    )""",
    """CREATE VIRTUAL TABLE IF NOT EXISTS local_event_fts USING fts5(
        tokens, content='local_event_fields', content_rowid='rowid', tokenize='unicode61'
    )""",
    """CREATE TRIGGER IF NOT EXISTS local_field_insert AFTER INSERT ON local_event_fields BEGIN
        INSERT INTO local_event_fts(rowid,tokens) VALUES (new.rowid,new.tokens);
    END""",
    """CREATE TRIGGER IF NOT EXISTS local_field_delete AFTER DELETE ON local_event_fields BEGIN
        INSERT INTO local_event_fts(local_event_fts,rowid,tokens) VALUES ('delete',old.rowid,old.tokens);
    END""",
)

# unicode61 already preserves word boundaries in Latin text. Separate Han
# characters so a quoted FTS phrase also matches a short name inside a sentence.
_HAN = re.compile(r"([\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\U00020000-\U0002fa1f])")


def _tokens(value: str) -> str:
    return _HAN.sub(r" \1 ", value)


def _vector_blob(vector: Sequence[float], dimensions: int) -> bytes:
    values = validate_embedding_vector(vector, dimensions=dimensions)
    return struct.pack(f"<{dimensions}f", *values)


def _keys(keys: Sequence[tuple[str, str]]) -> str:
    return json.dumps([(canonical_uuid(task, "parse_task_id"), canonical_uuid(chunk, "video_chunk_id"))
                       for task, chunk in keys])


_KEY_ROWS = "SELECT json_extract(value,'$[0]'),json_extract(value,'$[1]') FROM json_each(:keys)"


class SQLiteSearchIndex:
    """Publish both projections together and search the authorized Event sources."""

    def __init__(
        self, sessions: sessionmaker[Session], *, semantic_dimensions: int, vision_dimensions: int,
        vision_enabled: bool, index_name: str = "desktop_events",
    ) -> None:
        for dimensions in (semantic_dimensions, vision_dimensions):
            if type(dimensions) is not int or not 1 <= dimensions <= 4096:
                raise ValueError("index dimensions must be integers between 1 and 4096")
        self._sessions = sessions
        self.semantic_dimensions = semantic_dimensions
        self.vision_dimensions = vision_dimensions
        self.vision_enabled = vision_enabled
        self.index_name = require_nonempty_text(index_name, "index_name")
        self._contract: EventIndexingContract | None = None
        with sessions.begin() as session:
            self._load_vector_extension(session)
            if session.connection().exec_driver_sql("PRAGMA foreign_keys").scalar_one() != 1:
                raise IndexSchemaError("the desktop library connection requires SQLite foreign keys")
            for statement in _SCHEMA:
                session.connection().exec_driver_sql(statement)

    @staticmethod
    def _load_vector_extension(session: Session) -> None:
        connection = session.connection()
        if connection.info.get("seekino_sqlite_vec"):
            return
        try:
            import sqlite_vec
            db = connection.connection.driver_connection
            db.enable_load_extension(True)
            try:
                sqlite_vec.load(db)
            finally:
                db.enable_load_extension(False)
        except (ImportError, OSError, sqlite3.Error, AttributeError) as exc:
            raise IndexSchemaError("the desktop library requires the bundled sqlite-vec extension") from exc
        connection.info["seekino_sqlite_vec"] = True

    def bind_contract(self, contract: EventIndexingContract, *, create: bool = False) -> None:
        contract = EventIndexingContract.model_validate(contract)
        if (contract.semantic_encoder.dimensions != self.semantic_dimensions
                or (contract.vision_encoder is not None) != self.vision_enabled
                or contract.vision_encoder and contract.vision_encoder.dimensions != self.vision_dimensions):
            raise IndexSchemaError("the library's index and encoder dimensions or modes differ")
        with (self._sessions.begin() if create else self._sessions()) as session:
            if create:
                # An empty library can adopt the user's first model choice. An in-flight
                # upload has already frozen the contract, even before its Video exists.
                session.execute(text("""INSERT INTO local_index_contract(singleton,name,contract_id)
                    VALUES (1,:name,:contract)
                    ON CONFLICT(singleton) DO UPDATE SET contract_id=excluded.contract_id
                    WHERE name=excluded.name AND contract_id<>excluded.contract_id
                      AND NOT EXISTS (SELECT 1 FROM video)
                      AND NOT EXISTS (SELECT 1 FROM video_parse_task)
                      AND NOT EXISTS (SELECT 1 FROM local_chunks)
                      AND NOT EXISTS (SELECT 1 FROM upload_session WHERE status IN ('UPLOADING','COMPLETING'))
                    """), {"name": self.index_name, "contract": contract.contract_id})
            self._check_contract(session, contract.contract_id)
        self._contract = contract

    def ensure_index(self, contract: EventIndexingContract) -> None:
        self.bind_contract(contract, create=self._contract is None)

    def _check_contract(self, session: Session, contract_id: str) -> None:
        actual = session.execute(text("SELECT name,contract_id FROM local_index_contract WHERE singleton=1")).first()
        if actual is None:
            raise EventIndexNotReadyError("the library's Event index has not been initialized")
        if tuple(actual) != (self.index_name, contract_id):
            raise IndexSchemaError("the stored Event encoder contract differs from this library's configuration")

    def publish(
        self, session: Session, chunk: VideoChunkIndexDocument, events: Sequence[EventIndexDocument],
        receipt: EventIndexReceipt,
    ) -> None:
        """Replace one Chunk in the caller's fenced business transaction."""
        if receipt.index_name != self.index_name:
            raise IndexSchemaError("the publication targets another Event index")
        self._check_contract(session, receipt.contract_id)
        key = {"task": chunk.parse_task_id, "chunk": chunk.video_chunk_id}
        previous = session.execute(text(
            "SELECT generation,version FROM local_chunks WHERE task=:task AND chunk=:chunk"
        ), key).first()
        if previous is not None and tuple(previous) > (chunk.index_generation, chunk.content_version):
            raise IndexDocumentConsistencyError("a newer Chunk projection is already present")
        session.execute(text("DELETE FROM local_chunks WHERE task=:task AND chunk=:chunk"), key)
        document = {
            "tenant_id": chunk.tenant_id, "series_id": chunk.series_id, "video_id": chunk.video_id,
            "start_ms": chunk.chunk_start_seconds, "end_ms": chunk.chunk_end_seconds,
            "projection": chunk.projection.model_dump(mode="json"),
            "semantic_vector": chunk.semantic_vector, "vision_vector": chunk.vision_vector,
            "sparse_token_weights": chunk.sparse_token_weights,
        }
        session.execute(text("""INSERT INTO local_chunks(task,chunk,version,generation,event_count,document)
            VALUES(:task,:chunk,:version,:generation,:event_count,:document)"""), {
            **key, "version": chunk.content_version, "generation": chunk.index_generation,
            "event_count": receipt.event_count, "document": json.dumps(document, ensure_ascii=False, allow_nan=False),
        })
        for event in events:
            if ((event.parse_task_id, event.video_chunk_id, event.version) !=
                    (chunk.parse_task_id, chunk.video_chunk_id, chunk.content_version)):
                raise IndexDocumentConsistencyError("an Event does not belong to the published Chunk version")
            if self.vision_enabled != (event.vision_vector is not None):
                raise IndexSchemaError("Event vision mode differs from the index")
            fields = [field.model_dump(mode="json") for field in event.lexical_fields]
            session.execute(text("""INSERT INTO local_events(id,task,chunk,version,timeline_index,event_type,
                    speaker,lexical_fields) VALUES (:id,:task,:chunk,:version,:timeline_index,:event_type,
                    :speaker,:lexical_fields)"""), {
                **key, "id": event.event_id, "version": event.version, "event_type": event.event_type,
                "timeline_index": event.timeline_index, "speaker": event.speaker,
                "lexical_fields": json.dumps(fields, ensure_ascii=False),
            })
            session.execute(text("""INSERT INTO local_event_fields(event_id,position,tokens)
                VALUES (:id,:position,:tokens)"""), [
                    {"id": event.event_id, "position": i, "tokens": _tokens(field.raw_text)}
                    for i, field in enumerate(event.lexical_fields)
                ])
            vectors = [{"id": event.event_id, "channel": "semantic", "position": i,
                        "vector": _vector_blob(item.vector, self.semantic_dimensions)}
                       for i, item in enumerate(event.semantic_vectors)]
            if event.vision_vector is not None:
                vectors.append({"id": event.event_id, "channel": "vision", "position": 0,
                                "vector": _vector_blob(event.vision_vector, self.vision_dimensions)})
            session.execute(text("""INSERT INTO local_event_vectors(event_id,channel,position,vector)
                VALUES (:id,:channel,:position,:vector)"""), vectors)
        actual_ids = tuple(session.scalars(text(
            "SELECT id FROM local_events WHERE task=:task AND chunk=:chunk ORDER BY id"
        ), key))
        if actual_ids != receipt.event_ids:
            raise IndexDocumentConsistencyError("published Event IDs differ from the frozen receipt")

    def bulk_delete(self, keys: Sequence[tuple[str, str]], *, refresh=False) -> None:
        with self._sessions.begin() as session:
            session.execute(text(f"DELETE FROM local_chunks WHERE (task,chunk) IN ({_KEY_ROWS})"),
                            {"keys": _keys(keys)})

    def verify_absent(self, keys: Sequence[tuple[str, str]]) -> bool:
        with self._sessions() as session:
            return not session.scalar(text(
                f"SELECT EXISTS(SELECT 1 FROM local_chunks WHERE (task,chunk) IN ({_KEY_ROWS}))"
            ), {"keys": _keys(keys)})

    def require_coverage(self, source_generations: Mapping[tuple[str, str, int], int]) -> None:
        """Distinguish a missing projection from a valid search with no matches."""
        sources = json.dumps([(*source, generation) for source, generation in source_generations.items()])
        with self._sessions() as session:
            missing = session.scalar(text("""SELECT EXISTS(
                SELECT 1 FROM json_each(:sources) s LEFT JOIN local_chunks c
                    ON c.task=json_extract(s.value,'$[0]') AND c.chunk=json_extract(s.value,'$[1]')
                    AND c.version=json_extract(s.value,'$[2]') AND c.generation=json_extract(s.value,'$[3]')
                WHERE c.task IS NULL OR c.event_count != (
                    SELECT count(*) FROM local_events e WHERE e.task=c.task AND e.chunk=c.chunk AND e.version=c.version
                ))"""), {"sources": sources})
        if missing:
            raise EventIndexNotReadyError("the library's current Event projection needs to be rebuilt")

    def _scope(self, sources: Sequence[tuple[str, str, int]], filters: EventFilters, limit: int):
        if self._contract is None:
            raise EventIndexNotReadyError("Event search requires its frozen query contract")
        if type(limit) is not int or limit < 1:
            raise ValueError("limit must be a positive integer")
        allowed = []
        for task, chunk, version in sources:
            if type(version) is not int or version < 1:
                raise ValueError("Event source version must be a positive integer")
            allowed.append((canonical_uuid(task, "parse_task_id"), canonical_uuid(chunk, "video_chunk_id"), version))
        params = {"sources": json.dumps(allowed), "limit": limit}
        conditions = []
        if filters.speaker is not None:
            conditions.append("e.speaker=:speaker")
            params["speaker"] = filters.speaker
        for name, column, values in (("types", "event_type", filters.event_types),
                                     ("chunks", "chunk", filters.video_chunk_ids)):
            if values:
                conditions.append(f"e.{column} IN (SELECT value FROM json_each(:{name}))")
                params[name] = json.dumps(values)
        sql = """WITH scope AS MATERIALIZED (
            SELECT DISTINCT e.id FROM json_each(:sources) s CROSS JOIN local_events e
            ON e.task=json_extract(s.value,'$[0]') AND e.chunk=json_extract(s.value,'$[1]')
            AND e.version=json_extract(s.value,'$[2]')"""
        return sql + (" WHERE " + " AND ".join(conditions) if conditions else "") + ")", params

    def search_lexical(
        self, query: str, *, sources: Sequence[tuple[str, str, int]], filters: EventFilters, limit: int,
    ) -> tuple[RawEventHit, ...]:
        if not isinstance(query, str) or not query.strip():
            raise ValueError("query must be nonblank")
        scope, params = self._scope(sources, filters, limit)
        # Quote every input group. Operators, punctuation and user quotes cannot
        # become FTS syntax; several consecutive Han characters form one phrase.
        phrases = ['"' + _tokens(term) + '"' for term in re.findall(r"[^\W_]+", query)]
        if not phrases:
            return ()
        sql = scope + """, matches AS MATERIALIZED (
            SELECT f.event_id,f.position,local_event_fts.rank AS cost FROM local_event_fts
            JOIN local_event_fields f ON f.rowid=local_event_fts.rowid JOIN scope ON scope.id=f.event_id
            WHERE local_event_fts MATCH :query
        ), ranked AS (
            SELECT event_id,-MIN(cost) AS score,json_group_array(position) AS matched FROM matches
            GROUP BY event_id ORDER BY score DESC,event_id LIMIT :limit
        ) SELECT e.*,r.score,r.matched FROM ranked r JOIN local_events e ON e.id=r.event_id
          ORDER BY r.score DESC,e.id"""
        return self._read_hits(sql, {**params, "query": " OR ".join(phrases)}, "lexical")

    def search_vector(
        self, query_vector: Sequence[float], *, channel: Literal["semantic", "vision"],
        sources: Sequence[tuple[str, str, int]], filters: EventFilters, limit: int, exact: bool = False,
    ) -> tuple[RawEventHit, ...]:
        if channel not in ("semantic", "vision") or channel == "vision" and not self.vision_enabled:
            raise ValueError("unsupported Event vector channel")
        scope, params = self._scope(sources, filters, limit)
        vector = _vector_blob(query_vector, self.semantic_dimensions if channel == "semantic" else self.vision_dimensions)
        # sqlite-vec scalar distances run only over the authorized rows. Each
        # Event ranks once, by its best semantic representation or visual vector.
        sql = scope + """, ranked AS (
            SELECT v.event_id,2.0-MIN(vec_distance_cosine(v.vector,:query)) AS score FROM scope
            CROSS JOIN local_event_vectors v ON v.event_id=scope.id AND v.channel=:channel
            GROUP BY v.event_id ORDER BY score DESC,v.event_id LIMIT :limit
        ) SELECT e.*,r.score,NULL AS matched FROM ranked r JOIN local_events e ON e.id=r.event_id
          ORDER BY r.score DESC,e.id"""
        return self._read_hits(sql, {**params, "query": vector, "channel": channel}, channel)

    def _read_hits(
        self, sql: str, params: dict, channel: Literal["lexical", "semantic", "vision"],
    ) -> tuple[RawEventHit, ...]:
        hits = []
        with self._sessions() as session:
            self._check_contract(session, self._contract.contract_id)
            if channel != "lexical":
                self._load_vector_extension(session)
            for rank, row in enumerate(session.execute(text(sql), params).mappings(), 1):
                fields = json.loads(row["lexical_fields"])
                positions = sorted(json.loads(row["matched"])) if row["matched"] is not None else []
                if row["id"] != compute_event_id(row["task"], row["chunk"], row["version"], row["timeline_index"]):
                    raise IndexDocumentConsistencyError("local Event identity differs from its source")
                hits.append(RawEventHit(
                    event_id=row["id"], parse_task_id=row["task"], video_chunk_id=row["chunk"], version=row["version"],
                    timeline_index=row["timeline_index"], event_type=row["event_type"], speaker=row["speaker"],
                    channel=channel, rank=rank, score=row["score"], lexical_fields=fields,
                    matched_fields=[fields[i] for i in positions],
                ))
        return tuple(hits)
