from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import create_engine, inspect, text

from scripts.migrate_edit_template_series import migrate
from server.database import SchemaUpgradeRequiredError, create_schema, create_session_factory
from server.repository.editing_template import EditingTemplateRepository
from server.repository.library import LibraryRepository
from server.service.library import LibraryService


def test_template_series_upgrade_preserves_unassigned_rows(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'old.sqlite'}")
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE edit_template (id VARCHAR(36) PRIMARY KEY, tenant_id VARCHAR(64))"))
        connection.execute(text("INSERT INTO edit_template VALUES ('private-code', 'original-owner')"))
    assert migrate(engine) == {"apply":False, "add_series_id":True, "unassigned_templates":1}
    with pytest.raises(SchemaUpgradeRequiredError, match="migrate_edit_template_series"):
        create_schema(engine)
    with pytest.raises(ValueError, match="writers-stopped"):
        migrate(engine, apply=True)
    assert migrate(engine, apply=True, writers_stopped=True)["unassigned_templates"] == 1
    assert migrate(engine, apply=True, writers_stopped=True)["add_series_id"] is False
    with engine.connect() as connection:
        assert connection.execute(text("SELECT id, tenant_id, series_id FROM edit_template")).one() == (
            "private-code", "original-owner", None)
    assert any(index["column_names"] == ["series_id"] for index in inspect(engine).get_indexes("edit_template"))
    engine.dispose()


def test_series_shared_versions_preserve_authors_and_concurrent_retries(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'shared.sqlite'}", connect_args={"timeout":30})
    create_schema(engine)
    sessions = create_session_factory(engine)
    library = LibraryService(LibraryRepository(sessions))
    series = library.create_series(tenant_id="test-series", name="Shared edits")
    other = library.create_series(tenant_id="test-series", name="Another series")
    repository = EditingTemplateRepository(sessions)
    scope = (series.tenant_id, series.series_id)
    args = dict(template_id=None, new_template={"name":"红色切入"}, parent_version_id=None,
                content={"files":{"cut.js":"export default 1"},"entry":"cut.js","runtime":{},
                         "spec":{},"visual_requirements":{},"slots":[],"params_schema":{}},
                evaluation={"context":"first render"}, check=None, source_refs=[], resolve_source=None,
                request_id="same-key")
    with ThreadPoolExecutor(max_workers=8) as pool:
        receipts = list(pool.map(lambda _: repository.save(*scope, author="alice", **args), range(8)))
    first = receipts[0]
    assert all(value == first for value in receipts)
    assert len(repository.versions(*scope, first["template_id"])["versions"]) == 1
    second = repository.save(*scope, author="bob", **args)
    assert first["version_id"] != second["version_id"]
    assert len(repository.search([scope], "红色")) == 2
    assert repository.search([(series.tenant_id, other.series_id)], "红色") == []
    with pytest.raises(ValueError, match="TEMPLATE_NOT_FOUND"):
        repository.read(series.tenant_id, other.series_id, first["template_id"], first["version_id"])
    with pytest.raises(ValueError, match="TEMPLATE_NOT_FOUND"):
        repository.scope(template_id=first["template_id"], version_id=second["version_id"])
    note = dict(version_id=first["version_id"], kind="check", entry={"what":"render","result":"pass"}, request_id="same-note")
    for author in ("alice", "bob"):
        assert repository.append(*scope, author=author, **note) == repository.append(*scope, author=author, **note)
    chosen = dict(template_id=first["template_id"], version_id=first["version_id"], reason="reviewed", request_id="same-default")
    assert repository.set_default(*scope, author="alice", revision=0, **chosen)["revision"] == 1
    assert repository.set_default(*scope, author="alice", revision=0, **chosen)["revision"] == 1
    assert repository.set_default(*scope, author="bob", revision=1, **chosen)["revision"] == 2
    full = repository.read(*scope, first["template_id"])["version"]
    assert full["author"] == "alice"
    assert [item["author"] for item in full["check"]] == ["alice", "bob"]
    assert len([item for item in full["evaluation"] if item["kind"] == "default_selection"]) == 2
    with pytest.raises(ValueError, match="TEMPLATE_CONFLICT"):
        repository.set_default(*scope, author="charlie", revision=0, **chosen)
    with sessions.begin() as session:
        session.execute(text("UPDATE edit_template SET series_id=NULL WHERE id=:id"), {"id":second["template_id"]})
    with pytest.raises(ValueError, match="TEMPLATE_NOT_FOUND"):
        repository.scope(template_id=second["template_id"])
    assert len(repository.search([scope], "红色")) == 1
    engine.dispose()
