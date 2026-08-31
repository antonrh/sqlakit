"""The same behaviour, on every database the suite can reach.

SQLite answers most of this in `tests/unit`. What it cannot answer is whether
a real server agrees: savepoints, locks, expanding parameters, and the way each
dialect quotes a name.
"""

from collections.abc import Iterator
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa
import sqlalchemy.exc
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from sqlakit import Database, TransactionRolledBackError
from sqlakit.asyncio import Database as AsyncDatabase
from sqlakit.asyncio.orm import ModelMixin as AsyncModelMixin
from sqlakit.orm import ModelMixin, SoftDeletes

QUOTES = {"postgres": '"', "mysql": "`", "mariadb": "`", "oracle": '"'}
"""What each dialect wraps an identifier in, when one has to be wrapped."""

NAMES = {
    "postgres": "postgresql",
    "mysql": "mysql",
    "mariadb": "mysql",
    "oracle": "oracle",
}
"""What each dialect calls itself, which a template branches on."""


class Base(ModelMixin, DeclarativeBase):
    pass


class Event(Base, SoftDeletes):
    __tablename__ = "events"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(sa.String(50))
    at: Mapped[datetime]


class AsyncBase(AsyncModelMixin, DeclarativeBase):
    pass


class Note(AsyncBase, SoftDeletes):
    __tablename__ = "notes"

    id: Mapped[int] = mapped_column(primary_key=True)
    text: Mapped[str] = mapped_column(sa.String(50))


@pytest.fixture(autouse=True)
def _events(db: Database) -> Iterator[None]:
    """Five rows in a table of their own, dropped when the test ends."""
    Base.set_db(db)
    with db.transaction() as conn:
        Base.metadata.drop_all(conn)
        Base.metadata.create_all(conn)
    with db.transaction():
        start = datetime(2026, 1, 1)
        for index in range(1, 6):
            Event(id=index, name=f"e{index}", at=start + timedelta(hours=index)).save()
    yield
    with db.transaction() as conn:
        Base.metadata.drop_all(conn)


def _names(db: Database, statement: str) -> list[Any]:
    with db.connect() as conn:
        return list(conn.scalars(sa.text(statement)))


# blocks


def test_a_savepoint_undoes_only_its_own_block(db: Database) -> None:
    with db.transaction():
        Event(id=6, name="kept", at=datetime(2026, 2, 1)).save()

        with pytest.raises(ZeroDivisionError), db.transaction(savepoint=True):
            Event(id=7, name="undone", at=datetime(2026, 2, 1)).save()
            raise ZeroDivisionError

    assert _names(db, "SELECT name FROM events WHERE id > 5") == ["kept"]


def test_a_nested_session_that_rolls_back_ends_the_transaction(
    db: Database,
) -> None:
    with pytest.raises(TransactionRolledBackError), db.transaction():
        Event(id=6, name="gone", at=datetime(2026, 2, 1)).save()
        db.session.rollback()

    assert _names(db, "SELECT name FROM events WHERE id = 6") == []


def test_a_rolled_back_block_leaves_nothing_behind(db: Database) -> None:
    with db.transaction(rollback=True):
        Event(id=6, name="temporary", at=datetime(2026, 2, 1)).save()

        assert Event.query.count() == 6

    assert _names(db, "SELECT name FROM events WHERE id = 6") == []


def test_a_locked_row_is_locked(db: Database, url: str) -> None:
    other = Database(url)

    with db.transaction():
        Event.query.with_for_update().get_one(1)

        with pytest.raises(sa.exc.DBAPIError), other.connect() as conn:
            conn.execute(
                sa.text("SELECT id FROM events WHERE id = 1 FOR UPDATE NOWAIT")
            )

    other.dispose()


# reading


def test_a_list_reaches_the_driver_as_a_list(db: Database) -> None:
    with db.connect():
        rows = Event.query.where(Event.id.in_([1, 3])).order_by("id").all()

        assert [event.name for event in rows] == ["e1", "e3"]


def test_a_page_and_a_cursor_walk_the_same_rows(db: Database) -> None:
    with db.connect():
        page = Event.query.order_by(Event.at.desc()).page(limit=2)

        assert [event.name for event in page.items] == ["e5", "e4"]
        assert page.total == 5

        seen: list[str] = []
        cursor = None
        while True:
            cursor_page = Event.query.order_by(Event.at.desc()).cursor_page(
                limit=2, cursor=cursor
            )
            seen += [event.name for event in cursor_page.items]
            cursor = cursor_page.next_cursor
            if cursor is None:
                break

        assert seen == ["e5", "e4", "e3", "e2", "e1"]


def test_ignore_case_orders_without_regard_to_case(db: Database) -> None:
    with db.transaction():
        for index, name in enumerate(["B", "a", "C", "b"], 1):
            Event.query.get_one(index).name = name

    with db.connect():
        ordered = (
            Event.query.where(Event.id <= 4)
            .order_by("name", ignore_case=True)
            .order_by("id")
            .all()
        )

        assert [event.name for event in ordered] == ["a", "B", "b", "C"]


def test_chunks_walk_the_whole_table(db: Database) -> None:
    with db.connect():
        batches = [
            [event.name for event in batch]
            for batch in Event.query.order_by("id").chunks(2)
        ]

        assert batches == [["e1", "e2"], ["e3", "e4"], ["e5"]]


def test_a_soft_delete_is_stamped_by_the_database(db: Database) -> None:
    with db.transaction():
        Event.query.get_one(1).delete()

    with db.connect():
        assert Event.query.count() == 4
        assert Event.query.only_deleted().one().deleted_at is not None
        assert _names(db, "SELECT name FROM events WHERE id = 1") == ["e1"]


# templates


@pytest.fixture
def _templates(db: Database, tmp_path: Path) -> None:
    """Point the database at templates written for this test."""
    (tmp_path / "events").mkdir()
    (tmp_path / "events" / "named.sql").write_text(
        "SELECT name FROM events WHERE id IN {{ ids }} ORDER BY {{ column | identifier }}"
    )
    db.templates = tmp_path


@pytest.mark.usefixtures("_templates")
def test_a_template_binds_and_quotes_for_this_dialect(
    db: Database, dialect: str
) -> None:
    with db.connect():
        query = db.sql("events/named.sql", ids=[1, 2], column="name")
        quote = QUOTES[dialect]
        odd = db.sql("events/named.sql", ids=[1], column="Mixed Name")

        # A plain name is left alone, which is the only form Oracle accepts
        # for a column it created unquoted.
        assert "ORDER BY name" in str(query.statement)
        assert f"ORDER BY {quote}Mixed Name{quote}" in str(odd.statement)
        assert query.scalars().all() == ["e1", "e2"]
        assert db.sql("events/named.sql", ids=[], column="id").all() == []


@pytest.mark.usefixtures("_templates")
def test_a_template_knows_which_database_it_renders_for(
    db: Database, dialect: str
) -> None:
    # `events` stands in for a bare SELECT, which Oracle spells `FROM dual`.
    with db.connect():
        said = db.sql.from_string("SELECT {{ dialect }} AS d FROM events").scalars()

        assert said.first() == NAMES[dialect]


# the asyncio twin


@pytest.mark.anyio
async def test_the_async_twin_talks_to_this_database(async_db: AsyncDatabase) -> None:
    AsyncBase.set_db(async_db)
    async with async_db.transaction() as conn:
        await conn.run_sync(AsyncBase.metadata.drop_all)
        await conn.run_sync(AsyncBase.metadata.create_all)

    async with async_db.transaction():
        await Note(id=1, text="a").save()

    async with async_db.transaction(rollback=True):
        await Note(id=2, text="b").save()

        assert await Note.query.count() == 2

    async with async_db.transaction():
        await (await Note.query.get_one(1)).delete()

    async with async_db.connect():
        assert await Note.query.count() == 0
        assert await Note.query.only_deleted().count() == 1

    async with async_db.transaction() as conn:
        await conn.run_sync(AsyncBase.metadata.drop_all)
