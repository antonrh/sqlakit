"""Routing, awaited."""

from collections.abc import AsyncIterator

import pytest
import sqlalchemy as sa
import sqlalchemy.exc
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from sqlakit.asyncio import db
from sqlakit.asyncio.orm import ModelMixin


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
async def databases() -> AsyncIterator[None]:
    db.configure(
        {
            "default": {"url": "sqlite+aiosqlite://"},
            "replica": {"url": "sqlite+aiosqlite://"},
        }
    )
    for alias, text in (("default", "primary"), ("replica", "replica")):
        async with db[alias].transaction() as conn:
            await conn.run_sync(Base.metadata.create_all)
            await conn.execute(
                sa.text(f"INSERT INTO notes VALUES (1, '{text}')")  # noqa: S608
            )
    yield
    await db.dispose()


@pytest.mark.anyio
async def test_a_block_of_another_database_stands_in_for_the_default(
    databases: None,
) -> None:
    async with db.using("replica").connect():
        assert Note.db is db["replica"]
        assert (await Note.query.one()).text == "replica"

    assert Note.db is db["default"]


@pytest.mark.anyio
async def test_the_redirection_ends_with_a_block_that_failed(
    databases: None,
) -> None:
    with pytest.raises(ZeroDivisionError):
        async with db.using("replica").transaction():
            _ = 1 / 0

    assert Note.db is db["default"]


@pytest.mark.anyio
async def test_a_query_may_name_its_own_database(databases: None) -> None:
    async with db["default"].transaction(), db["replica"].connect():
        assert (await Note.query.using("replica").one()).text == "replica"
        assert (await Note.query.one()).text == "primary"


@pytest.mark.anyio
async def test_a_block_that_cannot_open_leaves_nothing_behind() -> None:
    db.configure(
        {
            "default": {"url": "sqlite+aiosqlite://"},
            "broken": {"url": "sqlite+aiosqlite:////nowhere/at/all.db"},
        }
    )

    with pytest.raises(sa.exc.OperationalError):
        async with db.using("broken").connect():
            pass  # pragma: no cover

    assert Note.db is db["default"]

    await db.dispose()
