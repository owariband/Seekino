"""Measure local Event search on repeatable synthetic data in a temporary library."""

import json
import sqlite3
from pathlib import Path
from statistics import median
from tempfile import TemporaryDirectory
from time import perf_counter
from uuid import UUID

from engine.rag import EncoderContract, VideoChunkIndexDocument
from engine.rag.indexing.events import EventIndexDocument, EventIndexingContract, build_event_index_receipt
from engine.rag.indexing.projection import ChunkIndexProjection
from engine.rag.retrieval.contracts import EventFilters
from server.desktop.database import open_desktop_database
from server.storage.sqlite_index import SQLiteSearchIndex

DIMENSIONS = 512


def identifier(number):
    return str(UUID(int=number + 1))


def seed(index, sessions, contract):
    sources = []
    for video in range(16):
        with sessions.begin() as session:
            for number in range(64):
                task = identifier(video)
                chunk_id = identifier(video * 64 + number + 100)
                sources.append((task, chunk_id, 1))
                vector = [0.] * DIMENSIONS
                vector[(video * 64 + number) % DIMENSIONS] = 1.
                chunk = VideoChunkIndexDocument(
                    tenant_id=identifier(0), series_id=identifier(1), video_id=identifier(video + 2),
                    video_chunk_id=chunk_id, parse_task_id=task, content_version=1, index_generation=1,
                    chunk_start_seconds=0, chunk_end_seconds=10000,
                    projection=ChunkIndexProjection(lexical_fields=[], semantic_text="蓝色杯子"),
                    semantic_vector=tuple(vector), vision_vector=None,
                )
                events = [EventIndexDocument(
                    parse_task_id=task, video_chunk_id=chunk_id, version=1, timeline_index=i, event_type="ACTION",
                    lexical_fields=[{"field": "event_description", "raw_text": "她把蓝色杯子放在了桌上"}],
                    semantic_vectors=[{"source": "description", "vector": vector}], vision_vector=None,
                    detail={"capture": {"frames": []}},
                ) for i in range(4)]
                index.publish(session, chunk, events, build_event_index_receipt(index.index_name, contract, events))
    return sources


def measure(call):
    call()
    timings = []
    for _ in range(7):
        start = perf_counter()
        call()
        timings.append((perf_counter() - start) * 1000)
    return {"median_ms": round(median(timings), 3), "samples_ms": [round(value, 3) for value in timings]}


def run(library):
    sessions = library.session_factory
    index = SQLiteSearchIndex(sessions, semantic_dimensions=DIMENSIONS, vision_dimensions=DIMENSIONS, vision_enabled=False)
    contract = EventIndexingContract(semantic_encoder=EncoderContract("benchmark", "fixed", "v1", DIMENSIONS),
                                     vision_encoder=None, capture=None)
    index.bind_contract(contract, create=True)
    sources = seed(index, sessions, contract)
    query_vector = [0.] * (DIMENSIONS - 1) + [1.]
    samples = {}
    for scope in (sources[:64], sources):
        options = dict(sources=scope, filters=EventFilters(), limit=20)
        versions = dict.fromkeys(scope, 1)
        samples[len(scope)] = {
            "coverage": measure(lambda: index.require_coverage(versions)),
            "lexical": measure(lambda: index.search_lexical("杯子", **options)),
            "vector": measure(lambda: index.search_vector(query_vector, channel="semantic", **options)),
        }
    return {"sqlite": sqlite3.sqlite_version, "videos": 16, "chunks": 1024, "events": 4096,
            "dimensions": DIMENSIONS, "top_k": 20, "queries_by_scope_size": samples}


if __name__ == "__main__":
    with TemporaryDirectory() as root:
        library = open_desktop_database(Path(root) / "library.sqlite3")
        try:
            print(json.dumps(run(library), indent=2))
        finally:
            library.close()
