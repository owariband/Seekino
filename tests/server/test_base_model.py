from datetime import UTC, datetime, timedelta, timezone

import pytest
from sqlalchemy import BigInteger, create_engine, insert, select, update
from sqlalchemy.dialects import mysql
from sqlalchemy.orm import Session

from server.database import Base, create_schema
from server.model.base import BaseModel, UtcDateTime
from server.model.user import User
from server.utils.time import datetime_to_unix_time_ms, now_unix_time_ms, unix_time_ms_to_datetime


@pytest.fixture
def engine():
    value = create_engine("sqlite+pysqlite:///:memory:")
    create_schema(value)
    yield value
    value.dispose()


def test_all_business_tables_inherit_indexed_nullable_audit_columns(engine):
    assert len(Base.metadata.tables) == 24
    assert "base_model" not in Base.metadata.tables
    for mapper in Base.registry.mappers:
        assert issubclass(mapper.class_, BaseModel)
        columns = mapper.local_table.c
        indexed = {tuple(index.columns.keys()) for index in mapper.local_table.indexes}
        for name in ("create_time", "create_date", "update_time", "update_date"):
            assert columns[name].nullable
            assert (name,) in indexed
            if name.endswith("_time"):
                assert isinstance(columns[name].type, BigInteger)
            else:
                assert columns[name].type.compile(dialect=mysql.dialect()) == "DATETIME(3)"


def test_new_record_uses_one_creation_instant(engine):
    with Session(engine) as session:
        record = User(id="new-user")
        session.add(record)
        session.flush()
        assert record.create_time == record.update_time
        assert record.create_date == record.update_date
        assert datetime_to_unix_time_ms(record.create_date) == record.create_time
        session.expire_all()
        assert record.create_date.tzinfo == UTC
        assert datetime_to_unix_time_ms(record.update_date) == record.update_time


@pytest.mark.parametrize("mode", ["orm", "statement", "bulk_by_pk"])
def test_updates_preserve_creation_and_sync_both_update_columns(engine, mode):
    initial = 1_700_000_000_123
    with Session(engine) as session:
        record = User(id="updated-user", create_time=initial)
        session.add(record)
        session.flush()
        before_update = now_unix_time_ms()
        if mode == "orm":
            record.auth_version += 1
            session.flush()
        elif mode == "statement":
            session.execute(update(User).where(User.id == record.id).values(auth_version=2))
        else:
            session.execute(update(User), [{"id": record.id, "auth_version": 2}])
        after_update = now_unix_time_ms()
        session.expire_all()
        assert record.auth_version == 2
        assert record.create_time == initial
        assert record.create_date == unix_time_ms_to_datetime(initial)
        assert before_update <= record.update_time <= after_update
        assert record.update_date == unix_time_ms_to_datetime(record.update_time)


def test_explicit_update_clock_and_each_insert_row_remain_paired(engine):
    values = [1_700_000_000_001, 1_700_000_000_999]
    with Session(engine) as session:
        session.execute(insert(User), [
            {"id": f"row-{index}", "create_time": value}
            for index, value in enumerate(values)
        ])
        rows = session.scalars(select(User).order_by(User.id)).all()
        for row, value in zip(rows, values, strict=True):
            assert row.create_time == row.update_time == value
            assert row.create_date == row.update_date == unix_time_ms_to_datetime(value)
        changed = values[-1] + 5_000
        session.execute(update(User).where(User.id == rows[0].id).values(update_time=changed))
        session.refresh(rows[0])
        assert rows[0].update_date == unix_time_ms_to_datetime(changed)


def test_unknown_historical_times_can_roundtrip_as_null(engine):
    with engine.begin() as connection:
        connection.execute(User.__table__.insert().values(
            id="legacy-user", create_time=None, create_date=None,
            update_time=None, update_date=None,
        ))
    with Session(engine) as session:
        record = session.get(User, "legacy-user")
        assert record.create_time is record.create_date is record.update_time is record.update_date is None


@pytest.mark.parametrize("value", [1, 17_179_869_184_001, 253_402_300_799_999])
def test_audit_dates_roundtrip_without_floating_point_millisecond_loss(engine, value):
    with Session(engine) as session:
        record = User(id="precise-audit-user", create_time=value)
        session.add(record)
        session.flush()
        session.expire_all()
        assert record.create_time == record.update_time == value
        assert datetime_to_unix_time_ms(record.create_date) == value
        assert datetime_to_unix_time_ms(record.update_date) == value


def test_utc_date_type_preserves_milliseconds_and_requires_an_explicit_zone():
    value = datetime(2026, 10, 10, 12, 34, 56, 123_999, tzinfo=timezone(timedelta(hours=8)))
    date_type = UtcDateTime()
    stored = date_type.process_bind_param(value, mysql.dialect())
    assert stored == datetime(2026, 10, 10, 4, 34, 56, 123_000)
    assert date_type.process_result_value(stored, mysql.dialect()) == stored.replace(tzinfo=UTC)
    with pytest.raises(ValueError, match="timezone-aware"):
        date_type.process_bind_param(value.replace(tzinfo=None), mysql.dialect())
