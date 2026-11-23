from contextlib import closing
from dataclasses import replace
from uuid import uuid4

import pytest
import yaml

from engine.rag.indexing.events import EventIndexingContract
from engine.rag.vision.event_frames import EventFrameContract
from server.config.storage import STORAGE_CONFIG
from server.desktop.database import open_desktop_database
from server.storage.index_contract import IndexSchemaError
from tests.server.test_sqlite_index import _add_video, _store
from tests.server.test_video_parse_index_coordinator import _contract
from tests.server.test_video_parse_queue import _task


def test_choose_a_different_embedding_model_after_first_open(tmp_path):
    pytest.importorskip("sqlite_vec")
    from server.desktop.runtime import open_library

    with open_library(tmp_path) as library:
        initial = library.create_series("Empty series")
    profile = STORAGE_CONFIG.parents[1] / "deploy/cloud/models.example.yaml"
    config = yaml.safe_load(profile.read_text())
    config["models"]["embedding"]["dimensions"] = 768
    (tmp_path / "models.yaml").write_text(yaml.safe_dump(config))
    with open_library(tmp_path) as reopened:
        assert [series.series_id for series in reopened.list_series()] == [initial.series_id]


@pytest.mark.parametrize("saved", ["queued_task", "published_projection"])
def test_reject_an_encoder_change_when_library_has_frozen_work(tmp_path, saved):
    pytest.importorskip("sqlite_vec")
    with closing(open_desktop_database(tmp_path / "saved.db")) as db:
        store = _store(db.session_factory)
        encoder = _contract().semantic_encoder
        initial = EventIndexingContract(semantic_encoder=encoder, vision_encoder=encoder,
                                        capture=EventFrameContract(max_height=720))
        store.bind_contract(initial, create=True)
        if saved == "queued_task":
            _task(db.session_factory, chunk_count=1, accepted=True)
        else:
            with db.session_factory.begin() as session:
                _add_video(store, session, initial, task=str(uuid4()), chunk_id=str(uuid4()),
                           tenant="local", series="first", vector=(1., 0.), vision=(0., 1.))
        replacement = replace(encoder, model="new-model")
        changed = EventIndexingContract(semantic_encoder=replacement, vision_encoder=replacement,
                                       capture=initial.capture)
        with pytest.raises(IndexSchemaError, match="differs"):
            _store(db.session_factory).bind_contract(changed, create=True)
        _store(db.session_factory).bind_contract(initial)
