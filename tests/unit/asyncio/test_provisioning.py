"""Creating the tables a test session needs, awaited."""

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from sqlakit.asyncio import Database
from sqlakit.asyncio.orm import ModelMixin


class Base(ModelMixin, DeclarativeBase):
    pass


class Recording(Base):
    __tablename__ = "recordings"

    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str]


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
def db() -> Database:
    db = Database("sqlite+aiosqlite://", engine_args={"poolclass": sa.StaticPool})
    Base.set_db(db)
    return db


async def _tables(db: Database) -> set[str]:
    async with db.connect() as conn:
        return set(await conn.run_sync(lambda sync: sa.inspect(sync).get_table_names()))


@pytest.mark.anyio
async def test_the_tables_are_there_inside_the_block_and_gone_after(
    db: Database,
) -> None:
    async with Base.provisioned_tables():
        assert await _tables(db) == {"recordings"}
        async with db.transaction():
            await Recording(id=1, title="a").save()
            assert await Recording.query.count() == 1

    assert await _tables(db) == set()

    await db.dispose()
