import pytest
import sqlalchemy as sa

from sqlakit import (
    ConflictingDatabaseUrlError,
    Database,
    DatabaseAlreadyConfiguredError,
    DatabaseNotConfiguredError,
    Databases,
    DefaultAliasError,
    MissingConnectionError,
    MissingDatabaseUrlError,
    MissingDefaultDatabaseError,
    UnknownDatabaseError,
)
from sqlakit._base import DATABASE_STATE


@pytest.fixture
def db() -> Databases:
    return Databases()


@pytest.fixture
def aliased(db: Databases) -> Databases:
    db.configure(
        {
            "default": {
                "url": "sqlite://",
                "engine_args": {"poolclass": sa.StaticPool},
            },
            "replica": {"url": "sqlite://", "session_args": {"autoflush": False}},
        }
    )
    return db


def test_unconfigured_raises(db: Databases) -> None:
    assert db.is_configured is False
    assert repr(db) == "Databases(unconfigured)"

    with pytest.raises(DatabaseNotConfiguredError):
        _ = db.engine

    with pytest.raises(DatabaseNotConfiguredError):
        _ = db.session


def test_configure(db: Databases) -> None:
    db.configure("sqlite://", engine_args={"poolclass": sa.StaticPool})

    assert db.is_configured is True
    assert repr(db) == "Databases('sqlite://')"
    assert db.engine.pool._pre_ping is True  # defaults still apply

    with db.transaction() as conn:
        assert conn.scalar(sa.text("select 1")) == 1
        assert db.connection is conn


def test_reconfigure_before_connecting(db: Databases) -> None:
    db.configure("sqlite://")
    db.configure("sqlite://", session_args={"autoflush": False})

    with db.connect():
        assert db.session.autoflush is False


def test_reconfiguring_replaces_the_session_arguments(db: Databases) -> None:
    db.configure("sqlite://")
    with db.connect():
        assert db.session.autoflush is True  # builds the sessionmaker

    db.dispose()
    db.configure("sqlite://", session_args={"autoflush": False})

    with db.connect():
        assert db.session.autoflush is False


def test_reconfigure_after_connecting_raises(db: Databases) -> None:
    db.configure("sqlite://")
    with db.connect():
        pass

    with pytest.raises(DatabaseAlreadyConfiguredError):
        db.configure("sqlite://")

    db.dispose()
    db.configure("sqlite://")  # allowed once the engine is gone


def test_typos_still_look_like_typos(db: Databases) -> None:
    db.configure("sqlite://")

    with pytest.raises(AttributeError):
        _ = db.sesion  # ty: ignore[unresolved-attribute]  # codespell:ignore


# aliases


def test_aliases(aliased: Databases) -> None:
    assert aliased.aliases == ("default", "replica")
    assert "replica" in aliased
    assert aliased["default"] is aliased  # `db.session` is always the default one

    with aliased.connect():
        assert aliased.session.autoflush is True

    with aliased["replica"].connect():
        assert aliased["replica"].session.autoflush is False


def test_an_alias_is_a_database_of_its_own(aliased: Databases) -> None:
    with aliased.transaction() as conn:
        # The replica takes no part in this transaction.
        with pytest.raises(MissingConnectionError):
            _ = aliased["replica"].connection

        with aliased["replica"].connect() as replica:
            assert replica is not conn


def test_unknown_alias(aliased: Databases) -> None:
    with pytest.raises(UnknownDatabaseError, match="'default', 'replica'"):
        aliased["writer"]


def test_configuration_needs_a_default(db: Databases) -> None:
    with pytest.raises(MissingDefaultDatabaseError, match="'replica'"):
        db.configure({"replica": {"url": "sqlite://"}})

    assert db.is_configured is False


def test_reconfigure_after_an_alias_connected_raises(aliased: Databases) -> None:
    assert aliased["replica"].engine is not None

    with pytest.raises(DatabaseAlreadyConfiguredError):
        aliased.configure("sqlite://")


# a database registered rather than configured


@pytest.fixture
def handed_over(db: Databases) -> Databases:
    """A registry given the databases, the default one among them."""
    db.register(
        "default", Database("sqlite://", engine_args={"poolclass": sa.StaticPool})
    )
    db.register(
        "replica", Database("sqlite://", engine_args={"poolclass": sa.StaticPool})
    )
    return db


def test_the_default_alias_answers_with_what_was_registered(
    handed_over: Databases,
) -> None:
    default = handed_over["default"]

    assert default is not handed_over
    assert handed_over.is_configured is True
    assert handed_over.aliases == ("default", "replica")

    with default.transaction():
        assert default.in_transaction() is True


def test_every_database_takes_a_transaction(handed_over: Databases) -> None:
    with handed_over.transactions(rollback=True):
        assert handed_over["default"].in_transaction() is True
        assert handed_over["replica"].in_transaction() is True


def test_a_recording_covers_the_registered_default(handed_over: Databases) -> None:
    with (
        handed_over.recording() as recording,
        handed_over["default"].connect() as connection,
    ):
        connection.execute(sa.text("SELECT 1"))

    assert [statement.database for statement in recording.statements] == ["default"]


def test_disposing_reaches_the_registered_default(handed_over: Databases) -> None:
    assert handed_over["default"].engine is not None

    handed_over.dispose()

    assert handed_over["default"]._engine is None
    assert handed_over["replica"]._engine is None


def test_the_registry_says_it_has_no_database_of_its_own(
    handed_over: Databases,
) -> None:
    with pytest.raises(DatabaseNotConfiguredError, match=r"db\['default'\]"):
        _ = handed_over.session

    with pytest.raises(DatabaseNotConfiguredError, match=r"db\['default'\]"):
        with handed_over.transaction():
            pass


def test_a_registry_without_a_database_says_so_rather_than_breaking(
    db: Databases,
) -> None:
    # The state a database has once it is configured, reached from a method of
    # its own rather than from the caller.
    with pytest.raises(DatabaseNotConfiguredError):
        with db.transaction():
            pass


def test_the_state_a_registry_explains_is_the_state_a_database_has() -> None:
    assert set(vars(Database("sqlite://"))) >= DATABASE_STATE


def test_only_one_of_them_can_be_the_default(handed_over: Databases) -> None:
    with pytest.raises(DefaultAliasError, match="default"):
        handed_over.register("default", Database("sqlite://"))

    with pytest.raises(DefaultAliasError, match="default"):
        handed_over.configure("sqlite://")


def test_a_configured_registry_keeps_the_default_it_built(aliased: Databases) -> None:
    with pytest.raises(DefaultAliasError, match="default"):
        aliased.register("default", Database("sqlite://"))


def test_dispose_covers_every_alias(aliased: Databases) -> None:
    assert aliased.engine is not None
    assert aliased["replica"].engine is not None

    aliased.dispose()

    assert aliased._engine is None
    assert aliased["replica"]._engine is None


# a url, or the parts to build one


def test_configure_from_parts(db: Databases) -> None:
    db.configure(
        drivername="postgresql+psycopg",
        host="db.internal",
        port=6432,
        username="app",
        password="secret",
        database="app",
        query={"sslmode": "require"},
    )

    assert db.url.host == "db.internal"
    assert db.url.port == 6432
    assert db.url.query == {"sslmode": "require"}
    assert "secret" not in repr(db)  # rendered by SQLAlchemy, password hidden


def test_aliases_from_parts(db: Databases) -> None:
    db.configure(
        {
            "default": {"drivername": "sqlite"},
            "replica": {"drivername": "sqlite", "database": ":memory:"},
        }
    )

    assert db.url.drivername == "sqlite"
    assert db["replica"].url.database == ":memory:"


def test_url_and_parts_together(db: Databases) -> None:
    with pytest.raises(ConflictingDatabaseUrlError, match="host"):
        db.configure({"default": {"url": "sqlite://", "host": "db.internal"}})


def test_neither_url_nor_parts(db: Databases) -> None:
    with pytest.raises(MissingDatabaseUrlError):
        db.configure({"default": {"engine_args": {"echo": True}}})

    with pytest.raises(MissingDatabaseUrlError):
        Database()
