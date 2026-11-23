import json
import sqlite3
import sys

import pytest

from engine.common.media_contract import FFMPEG_PREPROCESSING, PYAV_PREPROCESSING
from engine.rag import EventIndexingContract
from server.config.parse_profile import index_contract_fingerprint
from server.storage.index_contract import IndexSchemaError
from tests.server.test_desktop_pipeline import _fixed_model_responses, _fixture_movie, _wait_for_completion


@pytest.mark.skipif(sys.platform != "darwin", reason="desktop video preprocessing requires VideoToolbox")
def test_desktop_freezes_and_rebuilds_its_visual_inputs(tmp_path, monkeypatch):
    pytest.importorskip("av")
    pytest.importorskip("sqlite_vec")
    from agent.video_parse_v2.input import ParseConfig as VideoParseV2Config
    from server.desktop.runtime import open_library
    from server.service import video_parse_index_coordinator as coordinator

    revisions = []
    manifest_type = coordinator.VideoClipManifest

    def record_clip(**kw):
        revisions.append(kw["preprocess_revision"])
        return manifest_type(**kw)

    monkeypatch.setattr(coordinator, "VideoClipManifest", record_clip)
    calls = _fixed_model_responses(monkeypatch)
    source, root = tmp_path / "input.mp4", tmp_path / "library"
    _fixture_movie(source)
    with open_library(root) as library:
        series = library.create_series("预处理版本")
        upload = library.import_file(source, series_id=series.series_id, episode_order=1, idempotency_key="one")
        task_id = upload.video_parse_task_id
        _wait_for_completion(library, task_id, calls)
        assert library.rebuild_index(task_id)
        _wait_for_completion(library, task_id, calls)
        assert revisions == [PYAV_PREPROCESSING.clip, PYAV_PREPROCESSING.clip]
        assert library.search("红旗", series_ids=[series.series_id], keywords_only=True).hits

    with sqlite3.connect(root / "library.sqlite3") as db:
        config_json, detail_json = db.execute("SELECT parse_config,detail FROM video_parse_task WHERE id=?", (task_id,)).fetchone()
        config = VideoParseV2Config.model_validate_json(config_json)
        accepted = EventIndexingContract.model_validate(json.loads(detail_json)["_event_index_target_v1"]["contract"])
        assert accepted.capture.preprocess_revision == PYAV_PREPROCESSING.jpeg
        options = dict(provider=accepted.semantic_encoder.provider, encoder_revision=accepted.semantic_encoder.encoder_revision,
                       max_height=config.visual_input.max_height)
        assert config.indexing.contract_fingerprint == index_contract_fingerprint(config.models.embedding, preprocessing=PYAV_PREPROCESSING, **options)
        assert config.indexing.contract_fingerprint != index_contract_fingerprint(config.models.embedding, **options)
        receipt = json.loads(db.execute("SELECT detail FROM chunk_parse_content").fetchone()[0])["_index_embeddings_v2"]["receipt"]
        assert receipt["events"]["contract_id"] == accepted.contract_id
        assert db.execute("SELECT contract_id FROM local_index_contract").fetchone()[0] == accepted.contract_id

        old = accepted.model_copy(update={"capture": accepted.capture.model_copy(update={
            "preprocess_revision": FFMPEG_PREPROCESSING.jpeg,
        })})
        assert old.contract_id != accepted.contract_id
        db.execute("UPDATE local_index_contract SET contract_id=?", (old.contract_id,))
    with pytest.raises(IndexSchemaError, match="encoder contract differs"):
        open_library(root)


@pytest.mark.skipif(sys.platform != "darwin", reason="desktop video preprocessing requires VideoToolbox")
def test_delete_import_invalidates_playback_and_removes_media_and_index(tmp_path, monkeypatch):
    pytest.importorskip("av")
    pytest.importorskip("sqlite_vec")
    from server.desktop.runtime import open_library
    from server.service.mcp_evidence import StaleEvidenceError

    calls = _fixed_model_responses(monkeypatch)
    source, root = tmp_path / "input.mp4", tmp_path / "library"
    _fixture_movie(source)
    with open_library(root) as library:
        series = library.create_series("删除素材")
        upload = library.import_file(source, series_id=series.series_id, episode_order=1, idempotency_key="one")
        _wait_for_completion(library, upload.video_parse_task_id, calls)
        (hit,) = library.search("红旗", series_ids=[series.series_id], keywords_only=True).hits
        original = library.open_result(hit.source_ref).path
        assert original != source and original.is_file()
        deleted = library.delete_import(upload.session.upload_session_id)
        assert deleted.upload_session_id == upload.session.upload_session_id
        assert source.is_file() and not original.exists()
        with pytest.raises(StaleEvidenceError):
            library.open_result(hit.source_ref)
    with sqlite3.connect(root / "library.sqlite3") as db:
        for table in ("local_chunks", "local_events", "local_event_vectors", "local_event_fields", "local_event_fts"):
            assert db.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0
    with open_library(root) as reopened, pytest.raises(StaleEvidenceError):
        reopened.open_result(hit.source_ref)
