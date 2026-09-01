from collections.abc import AsyncIterator

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.orm.exc import NoResultFound

from sqlakit import DetachedInstanceError, UnknownFieldError
from sqlakit.asyncio import Database
from sqlakit.asyncio.orm import ModelMixin, SoftDeletes


class Base(ModelMixin, DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str]


class Memo(Base, SoftDeletes):
    __tablename__ = "memos"

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
    yield db
    await db.dispose()


@pytest.mark.anyio
async def test_save_get_and_delete(db: Database) -> None:
    async with db.transaction():
        user = await User(name="ada").save()

        assert user.is_persisted
        assert await User.query.get(user.id) is user

        await user.delete()

        assert user.was_deleted
        assert await User.query.get(user.id) is None


@pytest.mark.anyio
async def test_save_outside_a_transaction_commits(db: Database) -> None:
    async with db.connect():
        await User(name="ada").save()

    async with db.connect() as conn:
        assert await conn.scalar(sa.select(sa.func.count(User.id))) == 1


@pytest.mark.anyio
async def test_save_inside_a_transaction_only_flushes(db: Database) -> None:
    with pytest.raises(ZeroDivisionError):
        async with db.transaction():
            user = await User(name="ada").save()

            assert await User.query.get(user.id) is not None
            1 / 0

    async with db.connect() as conn:
        assert await conn.scalar(sa.select(sa.func.count(User.id))) == 0


@pytest.mark.anyio
async def test_refresh(db: Database) -> None:
    async with db.transaction() as conn:
        user = await User(name="ada").save()
        await conn.execute(sa.update(User).values(name="grace"))

        await user.refresh()

        assert user.name == "grace"


@pytest.mark.anyio
async def test_saving_a_detached_instance_is_refused(db: Database) -> None:
    async with db.transaction():
        user = await User(name="ada").save()

    with pytest.raises(DetachedInstanceError):
        async with db.transaction():
            await user.save()


@pytest.mark.anyio
async def test_get_one(db: Database) -> None:
    async with db.transaction():
        user = await User(name="ada").save()

        assert await User.query.get_one(user.id) is user

        with pytest.raises(NoResultFound):
            await User.query.get_one(user.id + 1)


@pytest.mark.anyio
async def test_merge_brings_a_detached_instance_back(db: Database) -> None:
    async with db.transaction():
        user = await User(name="ada").save()

    async with db.transaction():
        user.name = "grace"
        merged = await (await user.merge()).save()

        assert merged is not user
        assert (await User.query.get_one(user.id)).name == "grace"


@pytest.mark.anyio
async def test_update_and_set_loaded(db: Database) -> None:
    async with db.transaction():
        user = await User(name="ada").save()

        await user.update({"name": "grace"}).save()
        await user.refresh()

        assert user.name == "grace"

        user.set_loaded("name", "read as loaded")

        assert user.name == "read as loaded"

        with pytest.raises(UnknownFieldError):
            user.update({"nmae": "x"})  # codespell:ignore


@pytest.mark.anyio
async def test_a_soft_delete_marks_the_row(db: Database) -> None:
    async with db.transaction():
        await Memo(id=1, text="a").save()
        await Memo(id=2, text="b").save()

    async with db.transaction():
        await (await Memo.query.get_one(1)).delete()

    async with db.connect():
        assert [memo.id for memo in await Memo.query.order_by("id").all()] == [2]
        assert [
            memo.id for memo in await Memo.query.with_deleted().order_by("id").all()
        ] == [1, 2]
        assert await Memo.query.only_deleted().count() == 1
        assert (
            await db.session.scalar(
                sa.select(sa.func.count()).select_from(Base.metadata.tables["memos"])
            )
            == 2
        )


@pytest.mark.anyio
async def test_restoring_and_forcing(db: Database) -> None:
    async with db.transaction():
        await Memo(id=1, text="a").save()
        await Memo(id=2, text="b").save()

    async with db.transaction():
        assert await Memo.query.where(Memo.id == 1).delete() == 1

    async with db.connect():
        await (await Memo.query.only_deleted().one()).restore()

    async with db.connect():
        assert await Memo.query.count() == 2

    async with db.transaction():
        await (await Memo.query.get_one(2)).delete(force=True)

    async with db.connect():
        assert await Memo.query.with_deleted().count() == 1
