"""Creating the tables a test session needs, and dropping them after."""

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

import sqlakit
from sqlakit import Database
from sqlakit.orm import ModelMixin


class Base(ModelMixin, DeclarativeBase):
    pass


class Team(Base):
    __tablename__ = "teams"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str]


class Player(Base):
    __tablename__ = "players"

    id: Mapped[int] = mapped_column(primary_key=True)
    team_id: Mapped[int] = mapped_column(sa.ForeignKey("teams.id"))
    team: Mapped[Team] = relationship()


class Event(Base):
    __tablename__ = "events"
    __db__ = "warehouse"

    id: Mapped[int] = mapped_column(primary_key=True)


def _tables(db: Database) -> set[str]:
    with db.connect() as conn:
        return set(sa.inspect(conn).get_table_names())


@pytest.fixture
def db() -> Database:
    db = Database("sqlite://", engine_args={"poolclass": sa.StaticPool})
    Base.set_db(db)
    return db


def test_the_tables_are_there_inside_the_block_and_gone_after(
    db: Database,
) -> None:
    Event.set_db(db)  # one database for everything

    with Base.provisioned_tables():
        assert _tables(db) == {"teams", "players", "events"}

    assert _tables(db) == set()

    Event.__db__ = "warehouse"


def test_each_database_gets_the_tables_of_its_own_models() -> None:
    sqlakit.db.configure(
        {"default": {"url": "sqlite://"}, "warehouse": {"url": "sqlite://"}}
    )
    Base.set_db(sqlakit.db["default"])

    with Base.provisioned_tables(), Base.provisioned_tables("warehouse"):
        assert _tables(sqlakit.db["default"]) == {"teams", "players"}
        assert _tables(sqlakit.db["warehouse"]) == {"events"}

    assert _tables(sqlakit.db["default"]) == set()
    assert _tables(sqlakit.db["warehouse"]) == set()

    sqlakit.db.dispose()


def test_a_database_takes_a_metadata_of_its_own(db: Database) -> None:
    metadata = sa.MetaData()
    sa.Table("notes", metadata, sa.Column("id", sa.Integer, primary_key=True))

    with db.provisioned_tables(metadata):
        assert _tables(db) == {"notes"}

    assert _tables(db) == set()


# association tables, and classes that are not ours


class Shared(ModelMixin, DeclarativeBase):
    pass


membership = sa.Table(
    "membership",
    Shared.metadata,
    sa.Column("band_id", sa.ForeignKey("bands.id"), primary_key=True),
    sa.Column("artist_id", sa.ForeignKey("artists.id"), primary_key=True),
)


class Band(Shared):
    __tablename__ = "bands"

    id: Mapped[int] = mapped_column(primary_key=True)
    artists: Mapped[list["Artist"]] = relationship(secondary=membership)


class Artist(Shared):
    __tablename__ = "artists"

    id: Mapped[int] = mapped_column(primary_key=True)


class Ledger(Shared):
    __tablename__ = "ledgers"
    __db__ = "warehouse"

    id: Mapped[int] = mapped_column(primary_key=True)


@Shared.registry.mapped
class Legacy:
    """Mapped into the same registry, but no model of ours."""

    __tablename__ = "legacy"

    id = sa.Column(sa.Integer, primary_key=True)


def test_an_association_table_goes_with_the_rows_it_joins() -> None:
    sqlakit.db.configure(
        {"default": {"url": "sqlite://"}, "warehouse": {"url": "sqlite://"}}
    )
    Shared.set_db(sqlakit.db["default"])

    with Shared.provisioned_tables(), Shared.provisioned_tables("warehouse"):
        assert _tables(sqlakit.db["default"]) == {"bands", "artists", "membership"}
        assert _tables(sqlakit.db["warehouse"]) == {"ledgers"}

    sqlakit.db.dispose()


class Orphan(Shared):
    __tablename__ = "orphans"
    __db__ = "nowhere"

    id: Mapped[int] = mapped_column(primary_key=True)


def test_a_model_on_an_unconfigured_alias_is_left_alone(db: Database) -> None:
    Shared.set_db(db)

    with Shared.provisioned_tables():
        # `nowhere` is not configured, so its table is nobody's to create here.
        assert _tables(db) == {"bands", "artists", "membership"}

    assert _tables(db) == set()
