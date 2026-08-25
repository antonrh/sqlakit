"""`examples/sqlmodel_activerecord.py`, run against a database."""

from collections.abc import Iterator
from datetime import UTC, datetime
from types import ModuleType

import anyio
import pytest
import sqlalchemy as sa
from sqlmodel import SQLModel

from examples import sqlmodel_activerecord
from sqlakit.asyncio import Database


@pytest.fixture
def example() -> Iterator[ModuleType]:
    """The Active Record example, on a database of its own."""
    db = Database("sqlite+aiosqlite://", engine_args={"poolclass": sa.StaticPool})
    sqlmodel_activerecord.Base.set_db(db)

    async def build() -> None:
        async with db.transaction() as conn:
            await conn.run_sync(SQLModel.metadata.create_all)

    anyio.run(build)
    yield sqlmodel_activerecord
    anyio.run(db.dispose)


@pytest.mark.anyio
async def test_a_model_saves_and_reads_itself(example: ModuleType) -> None:
    async with example.Base.db.transaction():
        author = await example.Writer(name="ada").save()
        for index, title in enumerate(("alpha", "beta", "gamma")):
            book = await example.write_a_novel(title, author)
            book.published_at = datetime(2026, 1, index + 1, tzinfo=UTC)

        assert [book.title for book in await example.novels_by(author)] == [
            "alpha",
            "beta",
            "gamma",
        ]

        page = await example.page_of_novels(sort="title", limit=2)
        feed = await example.novel_feed(limit=2)

        assert [book.title for book in page.items] == ["alpha", "beta"]
        assert page.total == 3
        assert [book.title for book in feed.items] == ["gamma", "beta"]


@pytest.mark.anyio
async def test_a_query_of_its_own_narrows_the_reads(example: ModuleType) -> None:
    async with example.Base.db.transaction():
        author = await example.Writer(name="grace").save()
        await example.write_a_novel("alpha", author)
        unpublished = await example.write_a_novel("later", author)
        unpublished.published_at = datetime(2099, 1, 1, tzinfo=UTC)

        page = await example.page_of_novels(sort="title", limit=10)

        assert [book.title for book in page.items] == ["alpha"]
        assert await example.Novel.query.by_writer(author).count() == 2
