"""`session_factory()` opens nothing until the session is first used.

The other blocks stay eager, as `engine.connect()` is, and these tests pin
that down as well.
"""

from collections.abc import Iterator

import pytest
import sqlalchemy as sa
import sqlalchemy.event
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from sqlakit import Database


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "lazy_users"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str]


@pytest.fixture
def db() -> Iterator[Database]:
    db = Database("sqlite://", engine_args={"poolclass": sa.StaticPool})
    with db.transaction() as conn:
        Base.metadata.create_all(conn)
    yield db
    db.dispose()


@pytest.fixture
def checkouts(db: Database) -> list[object]:
    taken: list[object] = []
    sa.event.listen(db.engine, "checkout", lambda *args: taken.append(args))
    return taken


def test_an_unused_block_creates_no_engine() -> None:
    db = Database("sqlite://")

    with db.session_factory():
        pass

    assert db._engine is None


def test_entering_checks_nothing_out(db: Database, checkouts: list[object]) -> None:
    with db.session_factory():
        assert checkouts == []

    assert checkouts == []


def test_add_alone_checks_nothing_out(db: Database, checkouts: list[object]) -> None:
    with db.session_factory() as session:
        session.add(User(name="ada"))
        assert checkouts == []
        session.flush()
        assert len(checkouts) == 1


def test_the_first_query_checks_out_exactly_one(
    db: Database, checkouts: list[object]
) -> None:
    with db.session_factory() as session:
        assert session.scalar(sa.select(sa.func.count()).select_from(User)) == 0
        assert len(checkouts) == 1
        assert session.scalar(sa.select(sa.func.count()).select_from(User)) == 0
        assert len(checkouts) == 1


def test_a_commit_still_writes(db: Database) -> None:
    with db.session_factory() as session:
        session.add(User(name="ada"))
        session.commit()

    with db.connect():
        assert db.session.scalar(sa.select(User.name)) == "ada"


def test_a_nested_connect_shares_the_lazy_connection(
    db: Database, checkouts: list[object]
) -> None:
    with db.session_factory() as session:
        with db.connect() as conn:
            # The nested block performs the checkout the session then reuses.
            assert len(checkouts) == 1
            assert conn.scalar(sa.text("select 1")) == 1
        assert session.get_bind() is db.connection
        assert len(checkouts) == 1


def test_the_session_first_and_a_nested_connect_after_share_too(
    db: Database, checkouts: list[object]
) -> None:
    with db.session_factory() as session:
        session.add(User(name="ada"))
        session.flush()
        with db.connect() as conn:
            assert conn is db.connection
            assert session.get_bind() is conn
        assert len(checkouts) == 1


def test_reading_the_connection_performs_the_checkout(
    db: Database, checkouts: list[object]
) -> None:
    with db.session_factory():
        connection = db.connection
        assert isinstance(connection, sa.Connection)
        assert len(checkouts) == 1


def test_the_other_blocks_stay_eager(db: Database, checkouts: list[object]) -> None:
    with db.connect():
        assert len(checkouts) == 1
    with db.transaction():
        assert len(checkouts) == 2
    with db.autocommit():
        assert len(checkouts) == 3


def test_inside_another_block_it_reuses_the_connection(
    db: Database, checkouts: list[object]
) -> None:
    with db.transaction() as conn:
        with db.session_factory() as session:
            assert session.get_bind() is conn
        assert len(checkouts) == 1
