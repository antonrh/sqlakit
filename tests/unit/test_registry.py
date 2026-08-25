import pytest
import sqlalchemy as sa

from sqlakit import (
    ConflictingDatabaseUrlError,
    Database,
    DatabaseAlreadyConfiguredError,
    DatabaseNotConfiguredError,
    Databases,
    MissingConnectionError,
    MissingDatabaseUrlError,
    MissingDefaultDatabaseError,
    UnknownDatabaseError,
)


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
