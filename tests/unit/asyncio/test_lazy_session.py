"""The awaited `session_factory()` opens nothing until the session is used.

The other blocks stay eager, as `engine.connect()` is, and these tests pin
that down as well.
"""

from collections.abc import AsyncIterator

import pytest
import sqlalchemy as sa
import sqlalchemy.event
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from sqlakit import MissingConnectionError
from sqlakit.asyncio import Database


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "lazy_users"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str]


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
async def db() -> AsyncIterator[Database]:
    db = Database("sqlite+aiosqlite://", engine_args={"poolclass": sa.StaticPool})
    async with db.transaction() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield db
    await db.dispose()


@pytest.fixture
def checkouts(db: Database) -> list[object]:
    taken: list[object] = []
    sa.event.listen(db.engine.sync_engine, "checkout", lambda *args: taken.append(args))
    return taken


@pytest.mark.anyio
async def test_an_unused_block_creates_no_engine() -> None:
    db = Database("sqlite+aiosqlite://")

    async with db.session_factory():
        pass

    assert db._engine is None


@pytest.mark.anyio
async def test_entering_checks_nothing_out(
    db: Database, checkouts: list[object]
) -> None:
    async with db.session_factory():
        assert checkouts == []

    assert checkouts == []


@pytest.mark.anyio
async def test_add_alone_checks_nothing_out(
    db: Database, checkouts: list[object]
) -> None:
    async with db.session_factory() as session:
        session.add(User(name="ada"))
        assert checkouts == []
        await session.flush()
        assert len(checkouts) == 1


@pytest.mark.anyio
async def test_the_first_query_checks_out_exactly_one(
    db: Database, checkouts: list[object]
) -> None:
    async with db.session_factory() as session:
        count = sa.select(sa.func.count()).select_from(User)
        assert await session.scalar(count) == 0
        assert len(checkouts) == 1
        assert await session.scalar(count) == 0
        assert len(checkouts) == 1


@pytest.mark.anyio
async def test_a_commit_still_writes(db: Database) -> None:
    async with db.session_factory() as session:
        session.add(User(name="ada"))
        await session.commit()

    async with db.connect():
        assert await db.session.scalar(sa.select(User.name)) == "ada"


@pytest.mark.anyio
async def test_the_connection_raises_until_the_first_use(db: Database) -> None:
    async with db.session_factory() as session:
        with pytest.raises(MissingConnectionError, match="session_factory"):
            _ = db.connection

        # An empty flush is a no-op, so give the session something to write.
        session.add(User(name="ada"))
        await session.flush()
        assert db.connection is not None


@pytest.mark.anyio
async def test_a_nested_connect_shares_the_lazy_connection(
    db: Database, checkouts: list[object]
) -> None:
    async with db.session_factory() as session:
        async with db.connect() as conn:
            # The nested block performs the checkout the session then reuses.
            assert len(checkouts) == 1
            assert await conn.scalar(sa.text("select 1")) == 1
        await session.flush()
        assert session.get_bind() is db.connection.sync_connection
        assert len(checkouts) == 1


@pytest.mark.anyio
async def test_sql_runs_inside_a_lazy_block(
    db: Database, checkouts: list[object]
) -> None:
    async with db.session_factory():
        statement = sa.text("select 41 + 1")
        assert await db.sql.from_statement(statement).scalars().one() == 42
        assert len(checkouts) == 1


@pytest.mark.anyio
async def test_the_other_blocks_stay_eager(
    db: Database, checkouts: list[object]
) -> None:
    async with db.connect():
        assert len(checkouts) == 1
    async with db.transaction():
        assert len(checkouts) == 2
    async with db.autocommit():
        assert len(checkouts) == 3


@pytest.mark.anyio
async def test_inside_another_block_it_reuses_the_connection(
    db: Database, checkouts: list[object]
) -> None:
    async with db.transaction() as conn:
        async with db.session_factory() as session:
            assert session.get_bind() is conn.sync_connection
        assert len(checkouts) == 1
