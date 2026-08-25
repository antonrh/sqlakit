"""Recording and asserting, awaited."""

from collections.abc import AsyncIterator

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

import sqlakit.asyncio
from sqlakit.asyncio import Database
from sqlakit.asyncio.orm import ModelMixin
from sqlakit.testing import assert_queries


class Base(ModelMixin, DeclarativeBase):
    pass


class Note(Base):
    __tablename__ = "notes"

    id: Mapped[int] = mapped_column(primary_key=True)
    text: Mapped[str]


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
async def db() -> AsyncIterator[Database]:
    db = Database("sqlite+aiosqlite://", engine_args={"poolclass": sa.StaticPool})
    Base.set_db(db)
    async with db.transaction() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with db.transaction():
        await Note(id=1, text="a").save()
    yield db
    await db.dispose()


@pytest.mark.anyio
async def test_a_recording_counts_awaited_work(db: Database) -> None:
    with db.recording() as sql:
        async with db.connect():
            await Note.query.count()
            await Note.query.order_by("id").all()

    assert sql.count == 2


@pytest.mark.anyio
async def test_assert_queries_counts_awaited_work(db: Database) -> None:
    with assert_queries(1, using=db):
        async with db.connect():
            await Note.query.count()

    with pytest.raises(AssertionError, match="expected 1"):
        with assert_queries(1, using=db):
            async with db.connect():
                await Note.query.count()
                await Note.query.count()


@pytest.fixture
def registry() -> None:
    sqlakit.asyncio.db.configure(
        {
            "default": {"url": "sqlite+aiosqlite://"},
            "replica": {"url": "sqlite+aiosqlite://"},
        }
    )


@pytest.mark.anyio
async def test_assert_queries_watches_the_asyncio_registry_by_default(
    registry: None,
) -> None:
    with assert_queries(2) as sql:
        async with sqlakit.asyncio.db.connect() as conn:
            await conn.execute(sa.text("SELECT 1"))
        async with sqlakit.asyncio.db["replica"].connect() as conn:
            await conn.execute(sa.text("SELECT 1"))

    assert sql.databases == ("default", "replica")

    await sqlakit.asyncio.db.dispose()


@pytest.mark.anyio
async def test_a_transaction_on_every_database_at_once(registry: None) -> None:
    async with sqlakit.asyncio.db.transactions(rollback=True):
        assert sqlakit.asyncio.db.in_transaction()
        assert sqlakit.asyncio.db["replica"].in_transaction()

    assert not sqlakit.asyncio.db.in_transaction()

    await sqlakit.asyncio.db.dispose()
