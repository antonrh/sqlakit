from collections.abc import Callable

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from sqlalchemy.orm.exc import NoResultFound

import sqlakit
import sqlakit.asyncio.orm
import sqlakit.orm
from sqlakit import Database, DetachedInstanceError, UnknownFieldError
from sqlakit.orm import ModelMixin


class Base(ModelMixin, DeclarativeBase):
    pass


class Team(Base):
    __tablename__ = "teams"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str]


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str]
    nickname: Mapped[str | None] = mapped_column(default=None)
    team_id: Mapped[int | None] = mapped_column(sa.ForeignKey("teams.id"), default=None)
    team: Mapped[Team | None] = relationship(lazy="raise")


class Event(Base):
    __tablename__ = "events"
    __db__ = "warehouse"

    id: Mapped[int] = mapped_column(primary_key=True)
    what: Mapped[str]


@pytest.fixture
def db() -> Database:
    db = Database("sqlite://", engine_args={"poolclass": sa.StaticPool})
    Base.set_db(db)
    with db.transaction() as conn:
        Base.metadata.create_all(conn)
    return db


def test_save_and_get(db: Database) -> None:
    with db.transaction():
        user = User(name="ada").save()

        assert user.is_persisted
        assert User.query.get(user.id) is user
        assert User.query.get(user.id + 1) is None


def test_get_one(db: Database) -> None:
    with db.transaction():
        user = User(name="ada").save()

        assert User.query.get_one(user.id) is user

        with pytest.raises(NoResultFound):
            User.query.get_one(user.id + 1)


def test_save_outside_a_transaction_commits(db: Database) -> None:
    with db.connect():
        User(name="ada").save()

    with db.connect() as conn:
        assert conn.scalar(sa.select(sa.func.count(User.id))) == 1


def test_save_inside_a_transaction_only_flushes(db: Database) -> None:
    with pytest.raises(ZeroDivisionError), db.transaction():
        user = User(name="ada").save()

        # Flushed, so the queries that follow see it...
        assert User.query.get(user.id) is not None
        1 / 0

    with db.connect() as conn:
        # ...but the transaction is what decides whether it stays.
        assert conn.scalar(sa.select(sa.func.count(User.id))) == 0


def test_delete(db: Database) -> None:
    with db.transaction():
        user = User(name="ada").save()
        user.delete()

        assert user.was_deleted
        assert User.query.get(user.id) is None


def test_refresh(db: Database) -> None:
    with db.transaction() as conn:
        user = User(name="ada").save()
        conn.execute(sa.update(User).values(name="grace"))

        assert user.name == "ada"

        user.refresh()

        assert user.name == "grace"


def test_modified_fields(db: Database) -> None:
    with db.transaction():
        user = User(name="ada").save()

        assert user.is_modified is False
        assert user.modified_fields == set()

        user.name = "grace"

        assert user.is_modified is True
        assert user.modified_fields == {"name"}


def test_saving_a_detached_instance_is_refused(db: Database) -> None:
    with db.transaction():
        user = User(name="ada").save()

    with pytest.raises(DetachedInstanceError), db.transaction():
        user.save()


def test_queries_are_plain_sqlalchemy(db: Database) -> None:
    with db.transaction():
        User(name="ada").save()
        User(name="grace").save()

        found = db.session.scalars(
            sa.select(User).where(User.name.startswith("a"))
        ).all()

    assert [user.name for user in found] == ["ada"]


# where the database comes from


def test_a_model_takes_the_database_set_on_its_base(db: Database) -> None:
    assert User.db is db
    assert User(name="ada").db is db


def test_a_model_can_name_an_alias_of_the_registry() -> None:
    sqlakit.db.configure(
        {
            "default": {"url": "sqlite://"},
            "warehouse": {"url": "sqlite://"},
        }
    )
    Event.set_db("warehouse")

    assert Event.db is sqlakit.db["warehouse"]

    sqlakit.db.dispose()


def test_models_can_sit_on_different_databases(db: Database) -> None:
    warehouse = Database("sqlite://", engine_args={"poolclass": sa.StaticPool})
    Event.set_db(warehouse)

    assert User.db is db
    assert Event.db is warehouse

    Event.set_db("warehouse")  # back to the alias


def test_merge_brings_a_detached_instance_back(db: Database) -> None:
    with db.transaction():
        user = User(name="ada").save()

    with db.transaction():
        user.name = "grace"
        merged = user.merge().save()

        assert merged is not user
        assert User.query.get_one(user.id).name == "grace"


def test_update_sets_the_fields_it_is_given(db: Database) -> None:
    with db.transaction():
        user = User(name="ada").save()

        user.update({"name": "grace"}).save()

        user.refresh()

        assert user.name == "grace"


def test_update_clears_a_field_when_the_value_is_none(db: Database) -> None:
    with db.transaction():
        user = User(name="ada", nickname="the countess").save()

        user.update({"nickname": None}).save()

        user.refresh()

        assert user.nickname is None


def test_update_refuses_a_field_the_model_does_not_have(db: Database) -> None:
    with db.transaction():
        user = User(name="ada").save()

        with pytest.raises(UnknownFieldError) as caught:
            user.update({"nmae": "grace"})  # codespell:ignore

        assert caught.value.field == "nmae"  # codespell:ignore
        assert user.name == "ada"


def test_set_loaded_answers_for_a_relationship_nobody_loaded(
    db: Database,
) -> None:
    with db.transaction():
        team = Team(name="red").save()
        user = User(name="ada", team_id=team.id).save()

        user.set_loaded("team", team)

        assert user.team is team
        assert user not in db.session.dirty


def test_set_loaded_survives_the_block_that_loaded_it(db: Database) -> None:
    with db.transaction():
        team = Team(name="red").save()
        user = User(name="ada", team_id=team.id).save()
        user.set_loaded("team", team)

    # The session is gone; nothing can be loaded, and nothing needs to be.
    assert user.team is team


def test_set_loaded_refuses_a_field_the_model_does_not_have(
    db: Database,
) -> None:
    with db.transaction():
        user = User(name="ada").save()

        with pytest.raises(UnknownFieldError):
            user.set_loaded("teem", None)


@pytest.mark.parametrize(
    ("sync", "asynchronous"),
    [
        (sqlakit.orm.ModelMixin, sqlakit.asyncio.orm.ModelMixin),
        (sqlakit.orm.SoftDeletes, sqlakit.asyncio.orm.SoftDeletes),
    ],
    ids=lambda cls: cls.__name__,
)
def test_the_async_model_mirrors_this_one(
    sync: type, asynchronous: type, mirrors: Callable[[type, type], None]
) -> None:
    mirrors(sync, asynchronous)
