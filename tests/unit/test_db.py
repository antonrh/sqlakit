import contextvars
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import anyio
import pytest
import sqlalchemy as sa
import sqlalchemy.event
import sqlalchemy.exc
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column

from sqlakit import (
    DEFAULT_ENGINE_ARGS,
    DEFAULT_SESSION_ARGS,
    Database,
    MissingConnectionError,
    MissingSessionError,
    RetryNotSupportedError,
    TransactionRolledBackError,
)
from sqlakit._base import default_backoff, fix_sqlite_transactions


@pytest.fixture(params=["asyncio", "trio"])
def anyio_backend(request: pytest.FixtureRequest) -> str:
    # Both backends: the synchronous database has no async engine to keep it
    # on asyncio, and its context is a `ContextVar` either way.
    return request.param


@pytest.fixture
def db() -> Database:
    return Database("sqlite://", engine_args={"poolclass": sa.StaticPool})


def test_engine_is_lazy(db: Database) -> None:
    assert db._engine is None
    assert isinstance(db.engine, sa.Engine)
    assert db.engine is db._engine


def test_engine_args_are_passed_to_create_engine() -> None:
    db = Database("sqlite://", engine_args={"echo": True, "pool_recycle": 60})

    assert db.engine.echo is True
    assert db.engine.pool._recycle == 60


def test_default_engine_args_are_applied(db: Database) -> None:
    assert db.engine.pool._pre_ping is True
    assert db.engine.pool._recycle == 1800


def test_engine_args_override_the_defaults() -> None:
    db = Database("sqlite://", engine_args={"pool_pre_ping": False})

    assert db.engine.pool._pre_ping is False


def test_default_session_args_are_applied(db: Database) -> None:
    with db.connect():
        assert db.session.expire_on_commit is False


def test_session_args_override_the_defaults() -> None:
    db = Database("sqlite://", session_args={"expire_on_commit": True})

    with db.connect():
        assert db.session.expire_on_commit is True


def test_defaults_are_not_mutated_by_a_database(db: Database) -> None:
    db.engine_args["echo"] = True
    db.session_args["autoflush"] = False

    assert "echo" not in DEFAULT_ENGINE_ARGS
    assert "autoflush" not in DEFAULT_SESSION_ARGS
    assert "echo" not in Database("sqlite://").engine_args


def test_dispose_resets_engine(db: Database) -> None:
    engine = db.engine
    db.dispose()

    assert db._engine is None
    assert db.engine is not engine


def test_context_manager_disposes(db: Database) -> None:
    with db:
        assert db.engine is not None

    assert db._engine is None


def test_repr(db: Database) -> None:
    assert repr(db) == "Database('sqlite://')"


# connection


def test_connect_executes_query(db: Database) -> None:
    with db.connect() as conn:
        assert conn.scalar(sa.text("select 1")) == 1


def test_transaction_commits(db: Database) -> None:
    with db.transaction() as conn:
        conn.execute(sa.text("create table t (id integer)"))
        conn.execute(sa.text("insert into t values (1)"))

    with db.connect() as conn:
        assert conn.scalar(sa.text("select count(*) from t")) == 1


def test_connection_without_context_raises(db: Database) -> None:
    with pytest.raises(MissingConnectionError):
        _ = db.connection


def test_connection_is_bound_inside_connect(db: Database) -> None:
    with db.connect() as conn:
        assert db.connection is conn
        assert db.connection.scalar(sa.text("select 1")) == 1


def test_connection_is_bound_inside_transaction(db: Database) -> None:
    with db.transaction() as conn:
        assert db.connection is conn
        assert db.connection.in_transaction()


def test_connection_is_unbound_after_exit(db: Database) -> None:
    with db.connect():
        pass

    with pytest.raises(MissingConnectionError):
        _ = db.connection


def test_connection_is_unbound_after_error(db: Database) -> None:
    with pytest.raises(ZeroDivisionError), db.connect():
        1 / 0

    with pytest.raises(MissingConnectionError):
        _ = db.connection


def test_nested_connect_reuses_the_bound_connection(db: Database) -> None:
    # One connection per context: nothing below takes a second one from the
    # pool, and `db.connection` is the same throughout.
    with db.connect() as outer:
        with db.connect() as inner:
            assert inner is outer
            assert db.connection is outer
        assert db.connection is outer


def test_a_block_takes_its_own_connection_when_told_not_to_join(
    tmp_path: Path,
) -> None:
    db = Database(f"sqlite:///{tmp_path / 'test.db'}")

    with db.transaction(join_nested=False) as connection, db.connect() as inner:
        assert inner is not connection

    db.dispose()


def test_databases_do_not_share_the_connection(db: Database) -> None:
    other = Database("sqlite://")

    with db.connect():
        with pytest.raises(MissingConnectionError):
            _ = other.connection


def test_connection_does_not_leak_into_another_context(db: Database) -> None:
    with db.connect():
        ctx = contextvars.Context()

        with pytest.raises(MissingConnectionError):
            ctx.run(lambda: db.connection)


def test_connection_does_not_leak_into_another_thread(db: Database) -> None:
    with db.connect(), ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(lambda: db.connection)

        with pytest.raises(MissingConnectionError):
            future.result()


# session


def test_session_without_context_raises(db: Database) -> None:
    with pytest.raises(MissingSessionError):
        _ = db.session


def test_session_reuses_the_current_connection(db: Database) -> None:
    with db.connect() as conn:
        assert isinstance(db.session, Session)
        assert db.session.get_bind() is conn
        assert db.session.scalar(sa.text("select 1")) == 1


def test_session_args_are_passed_to_sessionmaker() -> None:
    db = Database(
        "sqlite://",
        session_args={"autoflush": False, "expire_on_commit": False},
    )

    with db.connect():
        assert db.session.autoflush is False
        assert db.session.expire_on_commit is False


def test_session_is_created_once_per_connection(db: Database) -> None:
    with db.connect():
        assert db.session is db.session


def test_session_factory_binds_both(db: Database) -> None:
    with db.session_factory() as session:
        assert db.session is session
        assert db.connection is session.get_bind()
        assert session.scalar(sa.text("select 1")) == 1


def test_session_is_closed_when_the_connection_block_exits(db: Database) -> None:
    with db.connect():
        session = db.session
        session.execute(sa.text("select 1"))

    assert session.get_transaction() is None


def test_session_is_unbound_after_exit(db: Database) -> None:
    with db.session_factory():
        pass

    with pytest.raises(MissingSessionError):
        _ = db.session


def test_session_is_unbound_after_error(db: Database) -> None:
    with pytest.raises(ZeroDivisionError), db.session_factory():
        1 / 0

    with pytest.raises(MissingSessionError):
        _ = db.session


def test_nested_block_gets_its_own_session(db: Database) -> None:
    with db.session_factory() as outer:
        with db.session_factory() as inner:
            assert inner is not outer
            assert db.session is inner
        assert db.session is outer


def test_session_does_not_leak_into_another_context(db: Database) -> None:
    with db.connect():
        ctx = contextvars.Context()

        with pytest.raises(MissingSessionError):
            ctx.run(lambda: db.session)


def test_session_opened_in_a_child_context_is_shared_and_closed(db: Database) -> None:
    with db.connect():
        session = contextvars.copy_context().run(lambda: db.session)

        assert db.session is session

    assert session.get_transaction() is None


@pytest.mark.anyio
async def test_session_opened_in_a_child_task_is_shared_and_closed(
    db: Database,
) -> None:
    sessions: list[Session] = []

    async def child() -> None:
        sessions.append(db.session)

    with db.connect():
        # A task runs in a copy of the context, so a session opened there must
        # still land in the scope owned by the `connect()` block.
        async with anyio.create_task_group() as task_group:
            task_group.start_soon(child)

        (session,) = sessions

        assert db.session is session

    assert session.get_transaction() is None


def test_session_writes_through_the_bound_connection(db: Database) -> None:
    with db.transaction() as conn:
        conn.execute(sa.text("create table t (id integer)"))
        db.session.execute(sa.text("insert into t values (1)"))
        db.session.flush()

        assert conn.scalar(sa.text("select count(*) from t")) == 1


# join_nested / rollback


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str]


@pytest.fixture
def users_db(db: Database) -> Database:
    with db.transaction() as conn:
        Base.metadata.create_all(conn)
    return db


@pytest.fixture
def checkouts(db: Database) -> list[object]:
    taken: list[object] = []
    sa.event.listen(db.engine, "checkout", lambda *args: taken.append(args))
    return taken


def names(connection: sa.Connection) -> list[str]:
    return list(connection.scalars(sa.select(User.name).order_by(User.name)))


def test_rollback_undoes_the_writes(users_db: Database) -> None:
    with users_db.transaction(rollback=True):
        users_db.session.add(User(name="ada"))
        users_db.session.commit()

        assert names(users_db.connection) == ["ada"]

    with users_db.connect() as conn:
        assert names(conn) == []


def test_writes_are_rolled_back_on_error(users_db: Database) -> None:
    with pytest.raises(ZeroDivisionError), users_db.transaction():
        users_db.session.add(User(name="ada"))
        users_db.session.commit()
        1 / 0

    with users_db.connect() as conn:
        assert names(conn) == []


def test_transaction_works_as_a_decorator(users_db: Database) -> None:
    @users_db.transaction()
    def add(name: str) -> None:
        users_db.session.add(User(name=name))

    add("ada")
    add("grace")

    with users_db.connect() as conn:
        assert names(conn) == ["ada", "grace"]


def test_nested_block_reuses_the_connection(users_db: Database) -> None:
    with users_db.transaction() as conn:
        with users_db.connect() as inner:
            assert inner is conn

        with users_db.transaction() as inner:
            assert inner is conn


def test_a_transaction_inside_connect_runs_on_its_connection(
    users_db: Database, checkouts: list[object]
) -> None:
    with users_db.connect() as conn:
        with users_db.transaction() as inner:
            assert inner is conn
            users_db.session.add(User(name="ada"))

        assert len(checkouts) == 1

    with users_db.connect() as conn:
        assert names(conn) == ["ada"]


def test_a_transaction_inside_connect_ends_the_session_transaction(
    users_db: Database, checkouts: list[object]
) -> None:
    # The outer block's session began the transaction, so the nested block
    # commits through it, and both blocks' writes go together.
    with users_db.connect() as conn:
        users_db.session.add(User(name="ada"))
        users_db.session.flush()

        with users_db.transaction() as inner:
            assert inner is conn
            users_db.session.add(User(name="grace"))

        assert len(checkouts) == 1
        assert (
            users_db.session.scalar(sa.select(sa.func.count()).select_from(User)) == 2
        )

    with users_db.connect() as conn:
        assert names(conn) == ["ada", "grace"]


def test_a_failing_transaction_inside_connect_leaves_the_block_usable(
    users_db: Database,
) -> None:
    with users_db.connect() as conn:
        with suppress(ZeroDivisionError), users_db.transaction():
            users_db.session.add(User(name="ada"))
            1 / 0

        assert conn.scalar(sa.select(sa.func.count()).select_from(User)) == 0


def test_a_transaction_inside_session_factory_runs_on_its_connection(
    users_db: Database, checkouts: list[object]
) -> None:
    with users_db.session_factory():
        with users_db.transaction() as inner:
            assert inner is users_db.connection
            users_db.session.add(User(name="ada"))

        assert len(checkouts) == 1

    with users_db.connect() as conn:
        assert names(conn) == ["ada"]


def test_a_transaction_inside_autocommit_opens_its_own_connection(
    users_db: Database, checkouts: list[object]
) -> None:
    # No transaction runs on an `AUTOCOMMIT` connection, so this one needs
    # another connection to have one at all.
    with users_db.autocommit() as conn:
        with users_db.transaction() as inner:
            assert inner is not conn

        assert len(checkouts) == 2


def test_a_transaction_under_join_nested_false_stays_apart(tmp_path: Path) -> None:
    # Two connections of its own, so the database is a file rather than the
    # one connection a `StaticPool` hands out.
    db = Database(f"sqlite:///{tmp_path / 'test.db'}")

    with db.transaction(join_nested=False) as conn:
        with db.transaction() as inner:
            assert inner is not conn

    db.dispose()


def test_nested_block_in_a_transaction_gets_its_own_session(
    users_db: Database,
) -> None:
    with users_db.transaction():
        session = users_db.session

        with users_db.session_factory() as inner:
            assert inner is not session

        assert users_db.session is session


def test_nested_block_sees_the_uncommitted_writes(users_db: Database) -> None:
    with users_db.transaction():
        users_db.session.add(User(name="ada"))
        users_db.session.flush()

        with users_db.connect() as inner:
            assert names(inner) == ["ada"]


def test_nested_commit_does_not_end_the_outer_transaction(users_db: Database) -> None:
    with users_db.transaction(rollback=True):
        with users_db.session_factory() as inner:
            inner.add(User(name="ada"))
            inner.commit()

        with users_db.transaction():
            users_db.session.add(User(name="grace"))
            users_db.session.commit()

        assert names(users_db.connection) == ["ada", "grace"]

    with users_db.connect() as conn:
        assert names(conn) == []


@pytest.mark.parametrize("rollback", [False, True])
def test_a_nested_session_keeps_its_writes(users_db: Database, rollback: bool) -> None:
    # The same either way: a test harness must not be kinder than production.
    with users_db.transaction(rollback=rollback):
        with users_db.session_factory() as inner:
            inner.add(User(name="ada"))
            inner.flush()  # written, never committed

        assert names(users_db.connection) == ["ada"]


@pytest.mark.parametrize("rollback", [False, True])
def test_a_nested_rollback_ends_the_transaction(
    users_db: Database,
    rollback: bool,
) -> None:
    # A session that only takes part in a transaction cannot undo its own work
    # alone. `savepoint=True` is what a block that has to fail alone uses.
    with (
        pytest.raises(TransactionRolledBackError),
        users_db.transaction(rollback=rollback),
    ):
        with users_db.session_factory() as inner:
            inner.add(User(name="ada"))
            inner.flush()
            inner.rollback()


def test_objects_survive_a_joined_session(users_db: Database) -> None:
    with users_db.transaction(rollback=True):
        user = User(name="ada")
        users_db.session.add(user)
        users_db.session.commit()

        with users_db.session_factory() as inner:
            inner.add(User(name="grace"))
            inner.commit()

        # Neither expired nor detached by the nested session closing.
        assert user.name == "ada"
        assert user in users_db.session


def test_a_block_opens_its_own_connection_without_join_nested(tmp_path: Path) -> None:
    # A file, not the in-memory database the other tests use: on a StaticPool
    # every connection is the same one, so nothing could escape anything.
    db = Database(f"sqlite:///{tmp_path / 'test.db'}")
    with db.transaction() as conn:
        Base.metadata.create_all(conn)

    with db.transaction(join_nested=False, rollback=True) as conn:
        db.session.add(User(name="ada"))
        db.session.flush()

        with db.connect() as inner:
            assert inner is not conn
            assert names(inner) == []

    db.dispose()


# autocommit


def test_autocommit_binds_the_connection(db: Database) -> None:
    with db.autocommit() as conn:
        assert db.connection is conn
        assert conn.get_execution_options()["isolation_level"] == "AUTOCOMMIT"
        assert not conn.in_transaction()


def test_autocommit_commits_every_statement(tmp_path: Path) -> None:
    url = f"sqlite:///{tmp_path / 'test.db'}"
    db = Database(url)
    with db.transaction() as conn:
        Base.metadata.create_all(conn)

    with db.autocommit() as conn:
        conn.execute(sa.insert(User).values(name="ada"))

    db.dispose()

    # Nothing ever committed it, so it is only there if each statement did.
    with Database(url).connect() as conn:
        assert names(conn) == ["ada"]


def test_autocommit_inside_a_test_transaction_is_rolled_back(tmp_path: Path) -> None:
    # What lets a test wrap code that autocommits: inside a transaction there is
    # nothing to commit to but that transaction, so it goes with it.
    db = Database(f"sqlite:///{tmp_path / 'test.db'}")
    with db.transaction() as conn:
        Base.metadata.create_all(conn)

    with db.transaction(rollback=True):
        with db.autocommit():
            db.session.add(User(name="ada"))
            db.session.commit()

        assert names(db.connection) == ["ada"]

    with db.connect() as conn:
        assert names(conn) == []

    db.dispose()


def test_autocommit_outside_a_transaction_commits_for_good(tmp_path: Path) -> None:
    db = Database(f"sqlite:///{tmp_path / 'test.db'}")
    with db.transaction() as conn:
        Base.metadata.create_all(conn)

    with db.autocommit() as conn:
        conn.execute(sa.insert(User).values(name="ada"))

    db.dispose()

    with Database(f"sqlite:///{tmp_path / 'test.db'}").connect() as conn:
        assert names(conn) == ["ada"]


def test_autocommit_joins_an_outer_transaction(db: Database) -> None:
    with db.transaction() as outer:
        with db.autocommit() as conn:
            # Inside a transaction there is nothing to commit to but that
            # transaction, so autocommit takes part in it like any other block.
            assert conn is outer

        assert db.connection is outer


def test_autocommit_outside_a_transaction_clears_the_context(db: Database) -> None:
    with db.connect() as opened:
        with db.autocommit() as conn:
            assert conn is not opened

            # Nothing below may join a transaction this conn is not in.
            with db.transaction() as inner:
                assert inner is not conn


def test_transaction_under_autocommit_is_a_real_transaction(tmp_path: Path) -> None:
    db = Database(f"sqlite:///{tmp_path / 'test.db'}")
    with db.transaction() as conn:
        Base.metadata.create_all(conn)

    with db.autocommit():
        with pytest.raises(ZeroDivisionError), db.transaction():
            db.session.add(User(name="ada"))
            db.session.flush()
            1 / 0

    with db.connect() as conn:
        assert names(conn) == []

    db.dispose()


# savepoints


def test_nested_transaction_opens_no_savepoint_by_default(users_db: Database) -> None:
    with users_db.transaction():
        with users_db.transaction():
            assert not users_db.connection.in_nested_transaction()


def test_nested_transaction_does_not_commit_on_its_own(users_db: Database) -> None:
    with pytest.raises(ZeroDivisionError), users_db.transaction():
        with users_db.transaction():
            users_db.session.add(User(name="ada"))
        1 / 0

    with users_db.connect() as conn:
        assert names(conn) == []


def test_savepoint_lets_a_nested_block_fail_alone(users_db: Database) -> None:
    with users_db.transaction():
        users_db.session.add(User(name="ada"))

        with suppress(ZeroDivisionError), users_db.transaction(savepoint=True):
            assert users_db.connection.in_nested_transaction()
            users_db.session.add(User(name="grace"))
            users_db.session.flush()
            1 / 0

    with users_db.connect() as conn:
        assert names(conn) == ["ada"]


def test_savepoint_applies_to_the_blocks_below_it(users_db: Database) -> None:
    with users_db.transaction(savepoint=True):
        with users_db.transaction():
            assert users_db.connection.in_nested_transaction()


def test_commit_on_error_keeps_the_writes(users_db: Database) -> None:
    class Blocked(Exception):
        pass

    with pytest.raises(Blocked), users_db.transaction(commit_on_error=Blocked):
        users_db.session.add(User(name="ada"))
        raise Blocked

    with users_db.connect() as conn:
        assert names(conn) == ["ada"]


def test_commit_on_error_ignores_other_errors(users_db: Database) -> None:
    class Blocked(Exception):
        pass

    with (
        pytest.raises(ZeroDivisionError),
        users_db.transaction(commit_on_error=Blocked),
    ):
        users_db.session.add(User(name="ada"))
        1 / 0

    with users_db.connect() as conn:
        assert names(conn) == []


def test_in_transaction_reports_the_context(db: Database) -> None:
    assert not db.in_transaction()

    with db.connect():
        assert not db.in_transaction()

    with db.transaction():
        assert db.in_transaction()

    with db.autocommit():
        assert not db.in_transaction()


def test_in_session_reports_whether_one_was_opened(db: Database) -> None:
    assert not db.in_session()

    with db.connect() as conn:
        assert not db.in_session()

        conn.execute(sa.text("select 1"))

        assert not db.in_session()

        db.session.execute(sa.text("select 1"))

        assert db.in_session()

    assert not db.in_session()


def test_transaction_works_as_a_bare_decorator(users_db: Database) -> None:
    @users_db.transaction
    def add(name: str) -> None:
        users_db.session.add(User(name=name))

    add("ada")

    with users_db.connect() as conn:
        assert names(conn) == ["ada"]


def test_autocommit_works_as_a_bare_decorator(db: Database) -> None:
    seen: list[bool] = []

    @db.autocommit
    def check() -> None:
        seen.append(db.in_transaction())

    check()
    check()

    assert seen == [False, False]


def test_ping(db: Database) -> None:
    assert db.ping() is True
    assert Database("sqlite:////nonexistent/path/db.sqlite3").ping() is False


# retry


def test_retry_runs_the_block_again(users_db: Database) -> None:
    attempts: list[int] = []

    @users_db.transaction(retry_on=ValueError, backoff=lambda _: 0)
    def flaky() -> None:
        attempts.append(len(attempts))
        users_db.session.add(User(name=f"ada-{len(attempts)}"))
        if len(attempts) < 3:
            raise ValueError

    flaky()

    assert len(attempts) == 3

    with users_db.connect() as conn:
        # Only the attempt that made it through is committed.
        assert names(conn) == ["ada-3"]


def test_retry_gives_up_after_max_retries(users_db: Database) -> None:
    attempts: list[int] = []

    @users_db.transaction(retry_on=ValueError, max_retries=2, backoff=lambda _: 0)
    def always_fails() -> None:
        attempts.append(len(attempts))
        raise ValueError

    with pytest.raises(ValueError):
        always_fails()

    assert len(attempts) == 3  # the first attempt plus two retries


def test_retry_ignores_other_errors(users_db: Database) -> None:
    attempts: list[int] = []

    @users_db.transaction(retry_on=ValueError, backoff=lambda _: 0)
    def fails() -> None:
        attempts.append(len(attempts))
        raise ZeroDivisionError

    with pytest.raises(ZeroDivisionError):
        fails()

    assert len(attempts) == 1


def test_retry_takes_a_predicate(users_db: Database) -> None:
    attempts: list[int] = []

    @users_db.transaction(
        retry_on=lambda exc: str(exc) == "again",
        backoff=lambda _: 0,
    )
    def flaky() -> None:
        attempts.append(len(attempts))
        raise ValueError("again" if len(attempts) < 2 else "stop")

    with pytest.raises(ValueError, match="stop"):
        flaky()

    assert len(attempts) == 2


def test_retry_cannot_be_a_context_manager(db: Database) -> None:
    with pytest.raises(RetryNotSupportedError), db.transaction(retry_on=ValueError):  # ty: ignore[invalid-context-manager]
        pass


def test_retry_is_skipped_when_the_block_joins_a_transaction(
    users_db: Database,
) -> None:
    attempts: list[int] = []

    @users_db.transaction(retry_on=ValueError, backoff=lambda _: 0)
    def flaky() -> None:
        attempts.append(len(attempts))
        raise ValueError

    with pytest.raises(ValueError), users_db.transaction():
        flaky()

    # Nothing to restart: the transaction belongs to the block above.
    assert len(attempts) == 1


def test_default_backoff_grows_and_varies() -> None:
    waits = [default_backoff(attempt) for attempt in range(4)]

    assert all(0.05 * 2**n <= wait <= 0.15 * 2**n for n, wait in enumerate(waits))
    assert default_backoff(0) != default_backoff(0)  # jittered


def test_a_transaction_that_cannot_connect_leaves_nothing_bound() -> None:
    db = Database("sqlite:////nonexistent/path/db.sqlite3")

    with pytest.raises(sa.exc.OperationalError), db.transaction():
        pass  # pragma: no cover

    with pytest.raises(MissingConnectionError):
        _ = db.connection


def test_the_sqlite_fix_leaves_other_dialects_alone() -> None:
    engine = cast(
        "sa.Engine", SimpleNamespace(dialect=SimpleNamespace(name="postgresql"))
    )

    fix_sqlite_transactions(engine)  # returns before registering any listener


def test_a_decorated_function_runs_in_parallel_threads(tmp_path: Path) -> None:
    # One `Transaction` object serves every call of a decorated function, so
    # two calls overlapping must not unwind each other's blocks.
    db = Database(f"sqlite:///{tmp_path / 'test.db'}")
    started = threading.Barrier(2)

    @db.transaction
    def work(delay: float) -> None:
        connection = db.connection
        started.wait(timeout=5)
        time.sleep(delay)
        assert db.connection is connection

    with ThreadPoolExecutor(max_workers=2) as pool:
        for future in [pool.submit(work, 0.01), pool.submit(work, 0.05)]:
            future.result()

    db.dispose()


def test_the_engine_is_built_once_under_threads() -> None:
    class Slow(Database):
        def _create_engine(self) -> sa.Engine:
            time.sleep(0.05)  # widen the window between the check and the build
            return super()._create_engine()

    db = Slow("sqlite://")
    start = threading.Barrier(2)

    def engine() -> sa.Engine:
        start.wait(timeout=5)
        return db.engine

    with ThreadPoolExecutor(max_workers=2) as pool:
        first, second = (
            task.result() for task in [pool.submit(engine), pool.submit(engine)]
        )

    assert first is second

    db.dispose()


def test_a_rolled_back_transaction_does_not_mask_the_error(users_db: Database) -> None:
    # The block failed for a reason; that reason is what the caller wants.
    with pytest.raises(ZeroDivisionError), users_db.transaction():
        with users_db.session_factory() as inner:
            inner.add(User(name="ada"))
            inner.flush()
            inner.rollback()
        1 / 0


def test_connect_inside_autocommit_stays_on_the_same_connection(db: Database) -> None:
    with db.autocommit() as conn:
        with db.connect() as inner:
            assert inner is conn
            assert db.connection is conn


# `session_factory()` connects on first use; the other blocks connect on entry


def test_session_factory_creates_no_engine_when_unused() -> None:
    db = Database("sqlite://")

    with db.session_factory():
        pass

    assert db._engine is None


def test_session_factory_checks_nothing_out_on_entry(
    db: Database, checkouts: list[object]
) -> None:
    with db.session_factory():
        assert checkouts == []

    assert checkouts == []


def test_a_session_add_alone_checks_nothing_out(
    users_db: Database, checkouts: list[object]
) -> None:
    with users_db.session_factory() as session:
        session.add(User(name="ada"))
        assert checkouts == []
        session.flush()
        assert len(checkouts) == 1


def test_the_first_query_checks_out_one_connection(
    users_db: Database, checkouts: list[object]
) -> None:
    with users_db.session_factory() as session:
        count = sa.select(sa.func.count()).select_from(User)
        assert session.scalar(count) == 0
        assert len(checkouts) == 1
        assert session.scalar(count) == 0
        assert len(checkouts) == 1


def test_a_lazy_session_commits_what_it_wrote(users_db: Database) -> None:
    with users_db.session_factory() as session:
        session.add(User(name="ada"))
        session.commit()

    with users_db.connect():
        assert users_db.session.scalar(sa.select(User.name)) == "ada"


def test_reading_the_connection_checks_it_out(
    db: Database, checkouts: list[object]
) -> None:
    with db.session_factory():
        assert isinstance(db.connection, sa.Connection)
        assert len(checkouts) == 1


def test_a_nested_connect_shares_the_lazy_connection(
    db: Database, checkouts: list[object]
) -> None:
    with db.session_factory() as session:
        with db.connect() as conn:
            # The nested block checks out the connection the session reuses.
            assert len(checkouts) == 1
            assert conn.scalar(sa.text("select 1")) == 1
        assert session.get_bind() is db.connection
        assert len(checkouts) == 1


def test_a_connect_after_the_session_shares_its_connection(
    users_db: Database, checkouts: list[object]
) -> None:
    with users_db.session_factory() as session:
        session.add(User(name="ada"))
        session.flush()
        with users_db.connect() as conn:
            assert conn is users_db.connection
            assert session.get_bind() is conn
        assert len(checkouts) == 1


def test_session_factory_inside_another_block_reuses_the_connection(
    db: Database, checkouts: list[object]
) -> None:
    with db.transaction() as conn:
        with db.session_factory() as session:
            assert session.get_bind() is conn
        assert len(checkouts) == 1


def test_the_other_blocks_connect_on_entry(
    db: Database, checkouts: list[object]
) -> None:
    with db.connect():
        assert len(checkouts) == 1
    with db.transaction():
        assert len(checkouts) == 2
    with db.autocommit():
        assert len(checkouts) == 3
