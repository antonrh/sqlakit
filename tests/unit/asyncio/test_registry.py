from collections.abc import AsyncIterator

import pytest
import sqlalchemy as sa

from sqlakit import DatabaseNotConfiguredError, UnknownDatabaseError
from sqlakit.asyncio import Databases


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
async def db() -> AsyncIterator[Databases]:
    databases = Databases()
    yield databases
    if databases.is_configured:
        await databases.dispose()


@pytest.mark.anyio
async def test_unconfigured_raises(db: Databases) -> None:
    assert db.is_configured is False

    with pytest.raises(DatabaseNotConfiguredError):
        _ = db.engine


@pytest.mark.anyio
async def test_configure_by_alias(db: Databases) -> None:
    db.configure(
        {
            "default": {
                "url": "sqlite+aiosqlite://",
                "engine_args": {"poolclass": sa.StaticPool},
            },
            "replica": {"url": "sqlite+aiosqlite://"},
        }
    )

    assert db.aliases == ("default", "replica")
    assert db["default"] is db

    async with db.transaction() as conn:
        assert await conn.scalar(sa.text("select 1")) == 1

        async with db["replica"].connect() as replica:
            assert replica is not conn

    await db.dispose()

    assert db["replica"]._engine is None


@pytest.mark.anyio
async def test_unknown_alias(db: Databases) -> None:
    db.configure("sqlite+aiosqlite://")

    with pytest.raises(UnknownDatabaseError):
        db["replica"]
