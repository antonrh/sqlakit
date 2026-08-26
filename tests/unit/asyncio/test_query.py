from collections.abc import AsyncIterator
from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import DeclarativeBase, Mapped, defer, mapped_column
from sqlalchemy.orm.exc import NoResultFound

from sqlakit import (
    InstanceNotFoundError,
    InvalidCursorError,
    KeyLookupError,
    MultipleInstancesFoundError,
    UnorderedPageError,
)
from sqlakit.asyncio import Database
from sqlakit.asyncio.orm import ModelMixin, Query
from sqlakit.testing import assert_queries


class Base(ModelMixin, DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str]


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
        for index, name in enumerate("abcde"):
            await User(id=index + 1, name=name).save()
    yield db
    await db.dispose()


@pytest.mark.anyio
async def test_reading(db: Database) -> None:
    async with db.connect():
        assert await User.query.count() == 5
        assert await User.query.where(User.name == "zz").exists() is False
        assert (await User.query.where(User.name == "c").one()).id == 3
        assert [u.name for u in await User.query.order_by(User.id).all()] == list(
            "abcde"
        )


@pytest.mark.anyio
async def test_page(db: Database) -> None:
    async with db.connect():
        page = await User.query.order_by(User.id).page(limit=2, offset=2)

        assert [user.name for user in page.items] == ["c", "d"]
        assert page.total == 5
        assert page.has_next is True


@pytest.mark.anyio
async def test_cursor_page(db: Database) -> None:
    async with db.connect():
        page = await User.query.order_by(User.id.desc()).cursor_page(limit=2)

        assert [user.name for user in page.items] == ["e", "d"]

        following = await User.query.order_by(User.id.desc()).cursor_page(
            limit=2, cursor=page.next_cursor
        )

        assert [user.name for user in following.items] == ["c", "b"]


@pytest.mark.anyio
async def test_page_without_the_count(db: Database) -> None:
    with assert_queries(1, using=db):
        async with db.connect():
            page = await User.query.order_by(User.id).page(
                limit=2, offset=2, total=False
            )

            assert [user.name for user in page.items] == ["c", "d"]
            assert page.total is None
            assert page.has_next is True


@pytest.mark.anyio
async def test_cursor_page_reads_backwards(db: Database) -> None:
    async with db.connect():
        second = await User.query.order_by(User.id).cursor_page(
            limit=2,
            cursor=(
                await User.query.order_by(User.id).cursor_page(limit=2)
            ).next_cursor,
        )

        assert [user.name for user in second.items] == ["c", "d"]
        assert second.has_previous is True

        back = await User.query.order_by(User.id).cursor_page(
            limit=2, cursor=second.previous_cursor
        )

        assert [user.name for user in back.items] == ["a", "b"]
        assert back.has_next is True
        assert back.has_previous is False


@pytest.mark.anyio
async def test_chunks_walk_the_whole_table(db: Database) -> None:
    async with db.connect():
        batches = [
            [user.name for user in batch]
            async for batch in User.query.order_by(User.id).chunks(2)
        ]

        assert batches == [["a", "b"], ["c", "d"], ["e"]]


@pytest.mark.anyio
async def test_a_cursor_from_elsewhere_is_refused(db: Database) -> None:
    async with db.connect():
        with pytest.raises(InvalidCursorError):
            await User.query.order_by(User.id).cursor_page(limit=2, cursor="nonsense")


@pytest.mark.anyio
async def test_from_statement(db: Database) -> None:
    async with db.connect():
        users = await User.query.from_statement(
            sa.text("SELECT * FROM users ORDER BY id LIMIT 2")
        ).all()

        assert [user.name for user in users] == ["a", "b"]

        # `first()` is a read, not a build, so a raw statement answers it too.
        first = await User.query.from_statement(
            sa.text("SELECT * FROM users ORDER BY id")
        ).first()

        assert first is not None
        assert first.name == "a"


@pytest.mark.anyio
async def test_first_and_one_or_none(db: Database) -> None:
    async with db.connect():
        first = await User.query.order_by(User.id).first()
        assert first is not None
        assert first.name == "a"
        assert await User.query.where(User.name == "zz").first() is None
        assert await User.query.where(User.name == "zz").one_or_none() is None
        found = await User.query.where(User.name == "c").one_or_none()
        assert found is not None
        assert found.id == 3


@pytest.mark.anyio
async def test_page_past_the_end(db: Database) -> None:
    async with db.connect():
        page = await User.query.order_by(User.id).page(limit=2, offset=99)

        assert page.items == []
        assert page.total == 5
        assert page.has_next is False


@pytest.mark.anyio
async def test_unfiltered(db: Database) -> None:
    async with db.connect():
        assert await User.query.unfiltered().count() == 5


@pytest.mark.anyio
async def test_only_columns(db: Database) -> None:
    async with db.connect():
        query = User.query.order_by(User.id)

        assert await query.only_columns(User.name).all() == list("abcde")
        assert await query.only_columns(User.id, User.name).first() == (1, "a")
        assert await query.only_columns(User.name).where(User.id == 3).one() == "c"

        with pytest.raises(InstanceNotFoundError):
            await query.only_columns(User.name).where(User.id == 99).one()
        with pytest.raises(MultipleInstancesFoundError):
            await User.query.only_columns(User.name).one()


@pytest.mark.anyio
async def test_create_and_create_many(db: Database) -> None:
    async with db.transaction():
        user = await User.query.create(id=6, name="f")

        assert (user.id, user.name) == (6, "f")
        assert await User.query.create_many([{"id": 7, "name": "g"}]) == 1
        assert await User.query.create_many([]) == 0

    async with db.connect():
        assert await User.query.count() == 7


@pytest.mark.anyio
async def test_a_write_outside_a_transaction_stays_written(db: Database) -> None:
    async with db.connect():
        await User.query.create(id=8, name="h")
        await User.query.where(User.name == "a").update({"name": "z"})
        await User.query.where(User.name == "b").delete()

    async with db.connect():
        names = await User.query.order_by("id").only_columns(User.name).all()

        assert names == ["z", "c", "d", "e", "h"]


@pytest.mark.anyio
async def test_bulk_update_and_delete(db: Database) -> None:
    async with db.transaction():
        assert await User.query.where(User.name == "a").update({"name": "z"}) == 1
        assert await User.query.filter_by(name="z").count() == 1
        assert await User.query.where(User.name == "z").delete() == 1
        assert await User.query.count() == 4


@pytest.mark.anyio
async def test_page_mapping(db: Database) -> None:
    async with db.connect():
        page = await User.query.order_by(User.id).page(limit=2)

        assert page.map(lambda user: user.name).items == ["a", "b"]

        # What an awaited transform does: map the items, keep the counts.
        names = [user.name.upper() for user in page.items]

        assert page.with_items(names).total == page.total


@pytest.mark.anyio
async def test_column_query_building(db: Database) -> None:
    async with db.connect():
        query = User.query.only_columns(User.name)

        assert await query.where(User.id == 1).all() == ["a"]
        assert await query.order_by(User.id.desc()).limit(1).all() == ["e"]
        assert await query.offset(4).order_by(User.id).all() == ["e"]
        assert await query.distinct().order_by(User.name).all() == list("abcde")
        assert await query.where(User.id == 99).one_or_none() is None


@pytest.mark.anyio
async def test_pagination_needs_an_order(db: Database) -> None:
    async with db.connect():
        with pytest.raises(UnorderedPageError):
            await User.query.page(limit=2)


# extending the query


class NamedQuery(Query[Any]):
    def named(self, name: str) -> "NamedQuery":
        return self.where(self.model.name == name)


class ScopedBase(ModelMixin, DeclarativeBase):
    query = NamedQuery.as_descriptor()


class Player(ScopedBase):
    __tablename__ = "players"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str]
    hidden: Mapped[bool] = mapped_column(default=False)

    @classmethod
    def __query_filter__(cls) -> Any:
        return cls.hidden.is_(False)


@pytest.fixture
async def scoped(db: Database) -> Database:
    ScopedBase.set_db(db)
    async with db.transaction() as conn:
        await conn.run_sync(ScopedBase.metadata.create_all)
    async with db.transaction():
        await Player(id=1, name="ada").save()
        await Player(id=2, name="grace", hidden=True).save()
    return db


@pytest.mark.anyio
async def test_a_base_hands_its_query_class_to_every_model(scoped: Database) -> None:
    async with scoped.transaction():
        assert isinstance(Player.query, NamedQuery)
        assert await Player.query.named("ada").count() == 1
        assert await Player.query.named("grace").count() == 0


@pytest.mark.anyio
async def test_the_filter_reaches_both_kinds_of_page(scoped: Database) -> None:
    async with scoped.transaction():
        page = await Player.query.order_by(Player.id).page(limit=10)
        cursor_page = await Player.query.order_by(Player.id).cursor_page(limit=10)

        assert [member.id for member in page.items] == [1]
        assert page.total == 1
        assert [member.id for member in cursor_page.items] == [1]
        assert await Player.query.unfiltered().count() == 2


@pytest.mark.anyio
async def test_latest_and_earliest(db: Database) -> None:
    async with db.transaction():
        latest = await User.query.latest(User.id)
        earliest = await User.query.earliest(User.id)

        assert latest is not None
        assert earliest is not None
        assert (latest.id, earliest.id) == (5, 1)


@pytest.mark.anyio
async def test_query_get(db: Database) -> None:
    async with db.transaction():
        user = await User.query.get(1)

        assert user is not None
        assert await User.query.get(1) is user
        assert await User.query.get(99) is None
        assert (await User.query.get_one(1)) is user

        with pytest.raises(NoResultFound):
            await User.query.get_one(99)

        with pytest.raises(KeyLookupError):
            await User.query.where(User.name == "a").get(1)


@pytest.mark.anyio
async def test_get_hides_what_the_model_hides(scoped: Database) -> None:
    async with scoped.transaction():
        assert await Player.query.get(2) is None
        assert await Player.query.unfiltered().get(2) is not None


@pytest.mark.anyio
async def test_get_loads_options_onto_a_row_already_in_the_session(
    scoped: Database,
) -> None:
    async with scoped.transaction():
        first = await Player.query.get(1)
        again = await Player.query.options(defer(Player.name)).get_one(1)

        assert again is first


@pytest.mark.anyio
async def test_a_page_is_refused_without_an_order_even_when_empty(
    db: Database,
) -> None:
    async with db.transaction():
        with pytest.raises(UnorderedPageError):
            await User.query.where(User.id > 100).page(limit=10)


@pytest.mark.anyio
async def test_missing_and_ambiguous_rows_name_the_model(db: Database) -> None:
    async with db.transaction():
        with pytest.raises(InstanceNotFoundError) as missing:
            await User.query.get_one(99)

        assert missing.value.model == "User"

        with pytest.raises(MultipleInstancesFoundError):
            await User.query.one()

        with pytest.raises(InstanceNotFoundError):
            await User.query.where(User.id > 100).one()

        with pytest.raises(MultipleInstancesFoundError):
            await User.query.one_or_none()
