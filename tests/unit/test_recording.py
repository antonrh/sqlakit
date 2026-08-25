"""What ran, how long it took, and how many times."""

import logging
import sys
from collections.abc import Iterator

import pytest
import sqlalchemy as sa
from rich.console import Console
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

import sqlakit
import sqlakit.asyncio
from sqlakit import Database, Recording, Statement, UnknownDatabaseError
from sqlakit import _recording as recording_module
from sqlakit.orm import ModelMixin
from sqlakit.testing import assert_queries


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
    team: Mapped[Team] = relationship(lazy="select")


@pytest.fixture
def db() -> Iterator[Database]:
    db = Database("sqlite://", engine_args={"poolclass": sa.StaticPool})
    Base.set_db(db)
    with db.transaction() as conn:
        Base.metadata.create_all(conn)
    with db.transaction():
        for index in (1, 2, 3):
            Team(id=index, name=f"team {index}").save()
            Player(id=index, team_id=index).save()
    yield db
    db.dispose()


def test_a_recording_counts_what_ran(db: Database) -> None:
    with db.recording() as sql, db.connect():
        Player.query.count()
        Player.query.order_by("id").all()

    assert sql.count == 2
    assert sql.duration > 0
    assert sql.slowest is not None
    assert "SELECT" in str(sql)


def test_transaction_control_is_not_a_query(db: Database) -> None:
    with db.recording() as sql, db.transaction() as conn:
        conn.execute(sa.text("SELECT 1"))

    # BEGIN reaches a cursor on SQLite and not on PostgreSQL; counting it
    # would make the same test mean different things.
    assert sql.count == 1


def test_a_recording_finds_the_query_that_repeats(db: Database) -> None:
    with db.recording() as sql, db.connect():
        for player in Player.query.order_by("id").all():
            _ = player.team.name

    assert sql.count == 4
    assert len(sql.duplicates) == 1
    assert len(next(iter(sql.duplicates.values()))) == 3
    assert "3 times in all" in str(sql)


def test_recordings_nest(db: Database) -> None:
    with db.recording() as outer, db.connect():
        Player.query.count()
        with db.recording() as inner:
            Player.query.count()

    assert (outer.count, inner.count) == (2, 1)


def test_nothing_is_watched_once_the_block_ends(db: Database) -> None:
    with db.recording() as sql, db.connect():
        Player.query.count()

    with db.connect():
        Player.query.count()

    assert sql.count == 1


def test_a_recording_can_be_read_rather_than_scanned(db: Database) -> None:
    with db.recording() as sql, db.connect():
        Player.query.order_by("id").all()

    assert sql.slowest is not None
    assert sql.pretty.splitlines()[1].startswith("        SELECT")
    assert "\n" in sql.slowest.pretty  # laid out over lines
    assert "\n" not in str(sql)  # the listing stays one line per statement


def test_without_the_formatter_the_sql_is_still_shown(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(recording_module, "sqlparse", None)

    with db.recording() as sql, db.connect():
        Player.query.count()

    assert sql.pretty.count("\n") == 1  # the header, then the one-line SQL
    assert "SELECT" in sql.pretty


def test_the_listings_all_point_at_the_repeat(db: Database) -> None:
    with db.recording() as sql, db.connect():
        for player in Player.query.order_by("id").all():
            _ = player.team.name

    assert "same as" in str(sql)
    assert "same as" in sql.pretty


def test_rich_is_handed_the_same_listing(db: Database) -> None:
    with db.recording() as sql, db.connect():
        for player in Player.query.order_by("id").all():
            _ = player.team.name

    console = Console(width=100, record=True)
    console.print(sql)
    console.print(sql.slowest)
    printed = console.export_text()

    assert "same as" in printed
    assert "SELECT" in printed


def test_echo_prints_the_block_with_no_logger(
    db: Database, capsys: pytest.CaptureFixture[str]
) -> None:
    with db.recording(echo=True), db.connect():
        Player.query.order_by("id").all()

    printed = capsys.readouterr().out

    assert "1 queries in" in printed
    assert "SELECT" in printed


def test_echo_prints_plainly_without_rich(
    db: Database, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setitem(sys.modules, "rich.console", None)

    with db.recording("GET /players", echo=True), db.connect():
        Player.query.order_by("id").all()

    printed = capsys.readouterr().out

    assert printed.startswith("GET /players: 1 queries in")
    assert "SELECT" in printed


def test_rich_is_handed_an_empty_recording_too() -> None:
    console = Console(width=100, record=True)
    console.print(Recording())

    assert console.export_text().strip() == "no queries"


def test_a_recording_can_remember_where_a_query_came_from(db: Database) -> None:
    with db.recording(stacks=True) as sql, db.connect():
        Player.query.count()

    assert sql.statements[0].stack
    assert "test_recording.py" in sql.statements[0].stack[0]


def test_a_recording_logs_what_it_adds_up_to(
    db: Database, caplog: pytest.LogCaptureFixture
) -> None:
    logger = logging.getLogger("sqlakit.test")

    with caplog.at_level(logging.INFO), db.recording("a label", logger=logger):
        with db.connect():
            Player.query.count()

    record = caplog.records[-1]

    assert "a label: 1 queries" in record.message
    assert record.levelno == logging.INFO
    assert getattr(record, "queries") == 1  # noqa: B009


def test_the_level_says_how_bad_it_is(
    db: Database, caplog: pytest.LogCaptureFixture
) -> None:
    logger = logging.getLogger("sqlakit.test")

    with caplog.at_level(logging.INFO), db.recording(logger=logger), db.connect():
        for player in Player.query.order_by("id").all():
            _ = player.team.name

    assert caplog.records[-1].levelno == logging.WARNING
    assert "repeated" in caplog.records[-1].message


def test_the_stats_are_the_fields_a_structured_log_takes(db: Database) -> None:
    with db.recording("label") as sql, db.connect():
        Player.query.count()

    assert sql.stats() == {
        "queries": 1,
        "milliseconds": pytest.approx(sql.milliseconds, abs=0.01),
        "slowest_milliseconds": pytest.approx(sql.milliseconds, abs=0.01),
        "duplicated": 0,
        "databases": ("default",),
        "label": "label",
    }


# what a test asserts


def test_assert_queries_counts(db: Database) -> None:
    with db.connect(), assert_queries(1, using=db):
        Player.query.count()


def test_assert_queries_says_what_ran_when_it_fails(db: Database) -> None:
    with pytest.raises(AssertionError) as caught:
        with db.connect(), assert_queries(1, using=db):
            Player.query.count()
            Player.query.count()

    assert "2 queries, expected 1" in str(caught.value)
    assert "same as" in str(caught.value)


def test_assert_queries_takes_a_ceiling(db: Database) -> None:
    with db.connect(), assert_queries(at_most=2, using=db):
        Player.query.count()

    with pytest.raises(AssertionError, match="at most 1"):
        with db.connect(), assert_queries(at_most=1, using=db):
            Player.query.count()
            Player.query.order_by("id").all()


def test_assert_queries_catches_the_n_plus_one(db: Database) -> None:
    with pytest.raises(AssertionError, match="repeat"):
        with db.connect(), assert_queries(duplicates=False, using=db):
            for player in Player.query.order_by("id").all():
                _ = player.team.name


def test_assert_queries_needs_something_to_assert() -> None:
    with pytest.raises(TypeError, match="something to assert"):
        assert_queries().__enter__()


# more than one database


@pytest.fixture
def registry() -> Iterator[None]:
    sqlakit.db.configure(
        {"default": {"url": "sqlite://"}, "warehouse": {"url": "sqlite://"}}
    )
    yield
    sqlakit.db.dispose()


def test_assert_queries_watches_every_database_by_default(registry: None) -> None:
    with assert_queries(2) as sql:
        with sqlakit.db.connect() as conn:
            conn.execute(sa.text("SELECT 1"))
        with sqlakit.db["warehouse"].connect() as conn:
            conn.execute(sa.text("SELECT 1"))

    assert sql.databases == ("default", "warehouse")
    assert "default" in sql.pretty  # which database ran what


def test_assert_queries_watches_the_database_it_is_given(registry: None) -> None:
    with assert_queries(1, using="warehouse") as sql:
        with sqlakit.db.connect() as conn:
            conn.execute(sa.text("SELECT 1"))
        with sqlakit.db["warehouse"].connect() as conn:
            conn.execute(sa.text("SELECT 1"))

    assert sql.databases == ("warehouse",)

    # The database itself says the same thing as its alias.
    with assert_queries(1, using=sqlakit.db["warehouse"]):
        with sqlakit.db["warehouse"].connect() as conn:
            conn.execute(sa.text("SELECT 1"))

    with pytest.raises(UnknownDatabaseError, match="reporting"):
        with assert_queries(1, using="reporting"):
            pass


def test_a_registry_recording_logs_like_any_other(
    registry: None, caplog: pytest.LogCaptureFixture
) -> None:
    logger = logging.getLogger("sqlakit.test")

    with caplog.at_level(logging.INFO), sqlakit.db.recording("both", logger=logger):
        with sqlakit.db["warehouse"].connect() as conn:
            conn.execute(sa.text("SELECT 1"))

    assert "both: 1 queries" in caplog.records[-1].message


def test_a_transaction_on_every_database_at_once(registry: None) -> None:
    with sqlakit.db.transactions(rollback=True):
        assert sqlakit.db.in_transaction()
        assert sqlakit.db["warehouse"].in_transaction()

    assert not sqlakit.db.in_transaction()
    assert not sqlakit.db["warehouse"].in_transaction()


# what a recording says, without a database to make one


def _statement(sql: str = "SELECT 1", milliseconds: float = 1.0) -> Statement:
    return Statement(sql=sql, parameters=(), duration=milliseconds / 1000)


def test_an_empty_recording_says_so() -> None:
    recording = Recording()

    assert recording.slowest is None
    assert str(recording) == "no queries"
    assert recording.pretty == "no queries"
    assert recording.summary() == "0 queries in 0.0ms"


def test_a_long_statement_is_cut_to_something_readable() -> None:
    recording = Recording(statements=[_statement("SELECT " + "x" * 300)])

    assert "…" in str(recording)
    assert len(str(recording).splitlines()[0]) < 160


def test_a_slow_block_says_how_slow(caplog: pytest.LogCaptureFixture) -> None:
    recording = Recording(statements=[_statement(milliseconds=120)])

    with caplog.at_level(logging.INFO):
        recording.log(logging.getLogger("sqlakit.test"))

    assert "slowest 120.0ms" in caplog.records[-1].message
    assert caplog.records[-1].levelno == logging.WARNING


def test_a_block_that_is_out_of_hand_says_so_loudly(
    caplog: pytest.LogCaptureFixture,
) -> None:
    recording = Recording(statements=[_statement(f"SELECT {n}") for n in range(30)])

    with caplog.at_level(logging.INFO):
        recording.log(logging.getLogger("sqlakit.test"))

    assert caplog.records[-1].levelno == logging.ERROR


def test_assert_queries_says_when_it_has_nothing_to_watch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    unconfigured = property(lambda _self: False)
    monkeypatch.setattr(type(sqlakit.db), "is_configured", unconfigured)
    monkeypatch.setattr(type(sqlakit.asyncio.db), "is_configured", unconfigured)

    with pytest.raises(TypeError, match="no database to watch"):
        assert_queries(1).__enter__()


def test_a_database_asserts_its_own_queries(db: Database) -> None:
    with db.assert_queries(2) as record:
        with db.connect() as conn:
            conn.execute(sa.text("SELECT 1"))
            conn.execute(sa.text("SELECT 2"))

    assert record.count == 2

    with db.assert_queries(at_most=5), db.connect() as conn:
        conn.execute(sa.text("SELECT 1"))

    with db.assert_queries(duplicates=False), db.connect() as conn:
        conn.execute(sa.text("SELECT 1"))
        conn.execute(sa.text("SELECT 2"))


def test_a_database_says_when_its_count_is_wrong(db: Database) -> None:
    with pytest.raises(AssertionError, match="1 queries, expected 9"):
        with db.assert_queries(9), db.connect() as conn:
            conn.execute(sa.text("SELECT 1"))

    with pytest.raises(AssertionError, match="repeat another"):
        with db.assert_queries(duplicates=False), db.connect() as conn:
            conn.execute(sa.text("SELECT 1"))
            conn.execute(sa.text("SELECT 1"))

    with pytest.raises(TypeError, match="something to assert"):
        db.assert_queries().__enter__()


def test_a_failing_block_keeps_its_own_error(db: Database) -> None:
    # The assertion must not stand in front of what actually went wrong.
    boom = ValueError("boom")

    with pytest.raises(ValueError, match="boom"):
        with db.assert_queries(99), db.connect():
            raise boom


def test_a_registry_asserts_across_every_database(registry: None) -> None:
    with sqlakit.db.assert_queries(2) as record:
        with sqlakit.db.connect() as conn:
            conn.execute(sa.text("SELECT 1"))
        with sqlakit.db["warehouse"].connect() as conn:
            conn.execute(sa.text("SELECT 1"))

    assert record.databases == ("default", "warehouse")
