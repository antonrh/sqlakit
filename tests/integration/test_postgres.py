"""What SQLite cannot answer: real savepoints, locks, and a second dialect."""

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa
import sqlalchemy.exc
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from sqlakit import CASE_INSENSITIVE_COLLATIONS, Database
from sqlakit.asyncio.orm import ModelMixin as AsyncModelMixin
from sqlakit.orm import ModelMixin, SoftDeletes


class Base(ModelMixin, DeclarativeBase):
    pass


class Event(Base, SoftDeletes):
    __tablename__ = "events"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str]
    at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True))
    payload: Mapped[dict[str, Any] | None] = mapped_column(sa.JSON, default=None)


@pytest.fixture
def events(postgres_db: Database) -> Iterator[Database]:
    Base.set_db(postgres_db)
    with postgres_db.transaction() as conn:
        Base.metadata.drop_all(conn)
        Base.metadata.create_all(conn)
    with postgres_db.transaction():
        start = datetime(2026, 1, 1, tzinfo=UTC)
        for index in range(1, 6):
            Event(id=index, name=f"e{index}", at=start + timedelta(hours=index)).save()
    yield postgres_db
    with postgres_db.transaction() as conn:
        Base.metadata.drop_all(conn)


def _names(db: Database, statement: str) -> list[Any]:
    with db.connect() as conn:
        return list(conn.scalars(sa.text(statement)))


def test_a_statement_no_transaction_may_hold(events: Database) -> None:
    with events.autocommit() as conn:
        conn.execute(sa.text("VACUUM events"))

    # Inside a transaction it joins that transaction, where VACUUM cannot run.
    with (
        pytest.raises(sa.exc.DBAPIError, match="transaction"),
        events.transaction() as (connection),
    ):
        connection.execute(sa.text("VACUUM events"))


def test_a_cursor_carries_an_aware_datetime_back(events: Database) -> None:
    with events.connect():
        seen: list[str] = []
        cursor = None
        while True:
            page = Event.query.order_by(Event.at.desc()).cursor_page(
                limit=2, cursor=cursor
            )
            seen += [event.name for event in page.items]
            cursor = page.next_cursor
            if cursor is None:
                break

        assert seen == ["e5", "e4", "e3", "e2", "e1"]


@pytest.fixture
def collated(events: Database) -> Iterator[Database]:
    """A `name` that carries a collation ignoring both case and accents."""
    with events.transaction() as conn:
        conn.exec_driver_sql(
            """CREATE COLLATION "und-ci-ai"
               (provider = icu, locale = 'und-u-ks-level1', deterministic = false)"""
        )
        conn.exec_driver_sql(
            'ALTER TABLE events ALTER COLUMN name TYPE text COLLATE "und-ci-ai"'
        )
    CASE_INSENSITIVE_COLLATIONS["postgresql"] = "und-ci-ai"
    yield events
    del CASE_INSENSITIVE_COLLATIONS["postgresql"]
    with events.transaction() as conn:
        # The collation cannot go while the column still refers to it.
        conn.exec_driver_sql(
            'ALTER TABLE events ALTER COLUMN name TYPE text COLLATE "default"'
        )
        conn.exec_driver_sql('DROP COLLATION IF EXISTS "und-ci-ai"')


def test_a_named_collation_is_what_ignore_case_orders_by(collated: Database) -> None:
    with collated.transaction():
        for index, name in enumerate(["resume", "Résumé", "RESUME", "apple"], 1):
            Event.query.get_one(index).name = name

    with collated.connect():
        query = (
            Event.query.where(Event.id <= 4)
            .order_by("name", ignore_case=True)
            .order_by("id")
        )

        assert 'COLLATE "und-ci-ai"' in str(query.select.compile(collated.engine))
        # The collation ignores the accents too, so the three tie and `id` breaks it.
        assert [event.id for event in query.all()] == [4, 1, 2, 3]


def test_a_soft_delete_is_stamped_by_the_database(events: Database) -> None:
    with events.transaction():
        Event.query.get_one(1).delete()

    with events.connect():
        marked = Event.query.only_deleted().one()

        assert marked.deleted_at is not None
        assert marked.deleted_at.tzinfo is not None  # timestamptz, not a string


# templates against a second dialect


@pytest.fixture
def templated(events: Database, tmp_path: Path) -> Database:
    (tmp_path / "events").mkdir()
    (tmp_path / "events" / "named.sql").write_text(
        "SELECT name FROM events WHERE id IN {{ ids }} ORDER BY {{ column | identifier }}"
    )
    (tmp_path / "events" / "dialect.sql").write_text("SELECT {{ dialect }} AS dialect")
    (tmp_path / "events" / "payload.sql").write_text(
        "UPDATE events SET payload = {{ payload }} WHERE id = {{ id }}"
    )
    events.templates = tmp_path
    return events


def test_a_parameter_carries_the_type_the_driver_needs(templated: Database) -> None:
    with templated.transaction():
        payload = sa.bindparam("payload", {"a": 1}, type_=sa.JSON())

        assert templated.sql("events/payload.sql", id=1, payload=payload).execute() == 1

    with templated.connect():
        assert Event.query.get_one(1).payload == {"a": 1}


# the asyncio twin, on the same database


class AsyncBase(AsyncModelMixin, DeclarativeBase):
    pass


class Note(AsyncBase, SoftDeletes):
    __tablename__ = "notes"

    id: Mapped[int] = mapped_column(primary_key=True)
    text: Mapped[str]
