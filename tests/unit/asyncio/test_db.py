import contextvars
from collections.abc import AsyncIterator
from contextlib import suppress
from pathlib import Path

import anyio
import pytest
import sqlalchemy as sa
import sqlalchemy.event
import sqlalchemy.exc
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from sqlakit import (
    MissingConnectionError,
    MissingSessionError,
    RetryNotSupportedError,
    TransactionRolledBackError,
)
from sqlakit.asyncio import Database


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str]


@pytest.fixture
def anyio_backend() -> str:
    # SQLAlchemy's async engine is asyncio-only: its drivers need a running
    # asyncio loop, so trio is not an option here.
    return "asyncio"


@pytest.fixture
async def db() -> AsyncIterator[Database]:
    db = Database("sqlite+aiosqlite://", engine_args={"poolclass": sa.StaticPool})
    yield db
    await db.dispose()


@pytest.fixture
async def users_db(db: Database) -> Database:
    async with db.transaction() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return db


@pytest.fixture
def checkouts(db: Database) -> list[object]:
    taken: list[object] = []
    sa.event.listen(db.engine.sync_engine, "checkout", lambda *args: taken.append(args))
    return taken


async def names(connection: AsyncConnection) -> list[str]:
    return list(await connection.scalars(sa.select(User.name).order_by(User.name)))


@pytest.mark.anyio
async def test_engine_is_lazy(db: Database) -> None:
    assert db._engine is None
    assert db.engine is db.engine


@pytest.mark.anyio
async def test_repr(db: Database) -> None:
    assert repr(db) == "Database('sqlite+aiosqlite://')"


@pytest.mark.anyio
async def test_connect_executes_query(db: Database) -> None:
    async with db.connect() as conn:
        assert await conn.scalar(sa.text("select 1")) == 1


@pytest.mark.anyio
async def test_connection_and_session_are_bound(db: Database) -> None:
    async with db.connect() as conn:
        assert db.connection is conn
        assert isinstance(db.session, AsyncSession)
        # An AsyncSession drives the sync connection underneath the async one.
        assert db.session.get_bind() is conn.sync_connection
        assert await db.session.scalar(sa.text("select 1")) == 1


@pytest.mark.anyio
async def test_nothing_is_bound_outside_a_block(db: Database) -> None:
    with pytest.raises(MissingConnectionError):
        _ = db.connection

    with pytest.raises(MissingSessionError):
        _ = db.session


@pytest.mark.anyio
async def test_context_is_unbound_after_exit(db: Database) -> None:
    async with db.connect():
        pass

    with pytest.raises(MissingConnectionError):
        _ = db.connection


@pytest.mark.anyio
async def test_session_does_not_leak_into_another_context(db: Database) -> None:
    async with db.connect():
        ctx = contextvars.Context()

        with pytest.raises(MissingSessionError):
            ctx.run(lambda: db.session)


@pytest.mark.anyio
async def test_session_opened_in_a_child_task_is_shared_and_closed(
    db: Database,
) -> None:
    sessions: list[AsyncSession] = []

    async def child() -> None:
        sessions.append(db.session)

    async with db.connect():
        # A task runs in a copy of the context, so a session opened there must
        # still land in the scope owned by the `connect()` block.
        async with anyio.create_task_group() as task_group:
            task_group.start_soon(child)

        (session,) = sessions

        assert db.session is session

    assert session.get_transaction() is None


# transactions


@pytest.mark.anyio
async def test_transaction_commits(users_db: Database) -> None:
    async with users_db.transaction():
        users_db.session.add(User(name="ada"))

    async with users_db.connect() as conn:
        assert await names(conn) == ["ada"]


@pytest.mark.anyio
async def test_transaction_rolls_back_on_error(users_db: Database) -> None:
    with pytest.raises(ZeroDivisionError):
        async with users_db.transaction():
            users_db.session.add(User(name="ada"))
            await users_db.session.flush()
            1 / 0

    async with users_db.connect() as conn:
        assert await names(conn) == []


@pytest.mark.anyio
async def test_transaction_works_as_a_decorator(users_db: Database) -> None:
    @users_db.transaction()
    async def add(name: str) -> None:
        users_db.session.add(User(name=name))

    await add("ada")
    await add("grace")

    async with users_db.connect() as conn:
        assert await names(conn) == ["ada", "grace"]


@pytest.mark.anyio
async def test_nested_block_reuses_the_connection(users_db: Database) -> None:
    async with users_db.transaction() as conn:
        async with users_db.connect() as inner:
            assert inner is conn

        async with users_db.transaction() as inner:
            assert inner is conn
            assert not users_db.connection.in_nested_transaction()


@pytest.mark.anyio
async def test_a_transaction_inside_connect_runs_on_its_connection(
    users_db: Database, checkouts: list[object]
) -> None:
    async with users_db.connect() as conn:
        async with users_db.transaction() as inner:
            assert inner is conn
            users_db.session.add(User(name="ada"))

        assert len(checkouts) == 1

    async with users_db.connect() as conn:
        assert await names(conn) == ["ada"]


@pytest.mark.anyio
async def test_a_transaction_inside_connect_ends_the_session_transaction(
    users_db: Database, checkouts: list[object]
) -> None:
    # The outer block's session began the transaction, so the nested block
    # commits through it, and both blocks' writes go together.
    async with users_db.connect() as conn:
        users_db.session.add(User(name="ada"))
        await users_db.session.flush()

        async with users_db.transaction() as inner:
            assert inner is conn
            users_db.session.add(User(name="grace"))

        assert len(checkouts) == 1

    async with users_db.connect() as conn:
        assert await names(conn) == ["ada", "grace"]


@pytest.mark.anyio
async def test_a_transaction_inside_session_factory_runs_on_its_connection(
    users_db: Database, checkouts: list[object]
) -> None:
    async with users_db.session_factory():
        async with users_db.transaction():
            users_db.session.add(User(name="ada"))

        assert len(checkouts) == 1

    async with users_db.connect() as conn:
        assert await names(conn) == ["ada"]


@pytest.mark.anyio
async def test_a_transaction_inside_autocommit_opens_its_own_connection(
    users_db: Database, checkouts: list[object]
) -> None:
    # No transaction runs on an `AUTOCOMMIT` connection, so this one needs
    # another connection to have one at all.
    async with users_db.autocommit() as conn:
        async with users_db.transaction() as inner:
            assert inner is not conn

        assert len(checkouts) == 2


@pytest.mark.anyio
async def test_nested_block_gets_its_own_session(users_db: Database) -> None:
    async with users_db.transaction():
        session = users_db.session

        async with users_db.session_factory() as inner:
            assert inner is not session

        assert users_db.session is session


@pytest.mark.anyio
async def test_savepoint_lets_a_nested_block_fail_alone(
    users_db: Database,
) -> None:
    async with users_db.transaction():
        users_db.session.add(User(name="ada"))

        with suppress(ZeroDivisionError):
            async with users_db.transaction(savepoint=True):
                assert users_db.connection.in_nested_transaction()
                users_db.session.add(User(name="grace"))
                await users_db.session.flush()
                1 / 0

    async with users_db.connect() as conn:
        assert await names(conn) == ["ada"]


@pytest.mark.anyio
async def test_rollback_undoes_the_writes(users_db: Database) -> None:
    async with users_db.transaction(rollback=True):
        users_db.session.add(User(name="ada"))
        await users_db.session.commit()

        assert await names(users_db.connection) == ["ada"]

    async with users_db.connect() as conn:
        assert await names(conn) == []


@pytest.mark.anyio
async def test_objects_survive_a_nested_session(users_db: Database) -> None:
    async with users_db.transaction(rollback=True):
        user = User(name="ada")
        users_db.session.add(user)
        await users_db.session.commit()

        async with users_db.session_factory() as inner:
            inner.add(User(name="grace"))
            await inner.commit()

        # Neither expired nor detached by the nested session closing.
        assert user.name == "ada"
        assert user in users_db.session


# autocommit


@pytest.mark.anyio
async def test_autocommit_binds_the_connection(db: Database) -> None:
    async with db.autocommit() as conn:
        assert db.connection is conn
        sync_connection = conn.sync_connection
        assert sync_connection is not None
        options = sync_connection.get_execution_options()
        assert options["isolation_level"] == "AUTOCOMMIT"
        assert not conn.in_transaction()


@pytest.mark.anyio
async def test_autocommit_joins_an_outer_transaction(
    users_db: Database,
) -> None:
    async with users_db.transaction() as outer:
        async with users_db.autocommit() as conn:
            # Inside a transaction there is nothing to commit to but that
            # transaction, so autocommit takes part in it like any other block.
            assert conn is outer

        assert users_db.connection is outer


@pytest.mark.anyio
async def test_dispose_resets_the_engine(db: Database) -> None:
    engine = db.engine
    await db.dispose()

    assert db._engine is None
    assert db.engine is not engine


@pytest.mark.anyio
async def test_context_manager_disposes(db: Database) -> None:
    async with db:
        assert db.engine is not None

    assert db._engine is None


@pytest.mark.anyio
async def test_transaction_works_as_a_bare_decorator(users_db: Database) -> None:
    @users_db.transaction
    async def add(name: str) -> None:
        users_db.session.add(User(name=name))

    await add("ada")

    async with users_db.connect() as conn:
        assert await names(conn) == ["ada"]


@pytest.mark.anyio
async def test_autocommit_works_as_a_bare_decorator(db: Database) -> None:
    seen: list[bool] = []

    @db.autocommit
    async def check() -> None:
        seen.append(db.in_transaction())

    await check()
    await check()

    assert seen == [False, False]


@pytest.mark.anyio
async def test_commit_on_error_keeps_the_writes(users_db: Database) -> None:
    class Blocked(Exception):
        pass

    with pytest.raises(Blocked):
        async with users_db.transaction(commit_on_error=Blocked):
            users_db.session.add(User(name="ada"))
            raise Blocked

    async with users_db.connect() as conn:
        assert await names(conn) == ["ada"]


# retry


@pytest.mark.anyio
async def test_retry_runs_the_block_again(users_db: Database) -> None:
    attempts: list[int] = []

    @users_db.transaction(retry_on=ValueError, backoff=lambda _: 0)
    async def flaky() -> None:
        attempts.append(len(attempts))
        users_db.session.add(User(name=f"ada-{len(attempts)}"))
        if len(attempts) < 3:
            raise ValueError

    await flaky()

    assert len(attempts) == 3

    async with users_db.connect() as conn:
        assert await names(conn) == ["ada-3"]


@pytest.mark.anyio
async def test_retry_gives_up_after_max_retries(users_db: Database) -> None:
    attempts: list[int] = []

    @users_db.transaction(retry_on=ValueError, max_retries=2, backoff=lambda _: 0)
    async def always_fails() -> None:
        attempts.append(len(attempts))
        raise ValueError

    with pytest.raises(ValueError):
        await always_fails()

    assert len(attempts) == 3


@pytest.mark.anyio
async def test_retry_cannot_be_a_context_manager(db: Database) -> None:
    with pytest.raises(RetryNotSupportedError):
        async with db.transaction(retry_on=ValueError):  # ty: ignore[invalid-context-manager]
            pass


@pytest.mark.anyio
async def test_retry_is_skipped_when_the_block_joins_a_transaction(
    users_db: Database,
) -> None:
    attempts: list[int] = []

    @users_db.transaction(retry_on=ValueError, backoff=lambda _: 0)
    async def flaky() -> None:
        attempts.append(len(attempts))
        raise ValueError

    with pytest.raises(ValueError):
        async with users_db.transaction():
            await flaky()

    assert len(attempts) == 1


@pytest.mark.anyio
async def test_ping(db: Database) -> None:
    assert await db.ping() is True
    assert await Database("sqlite+aiosqlite:////nonexistent/db.sqlite3").ping() is False


@pytest.mark.anyio
async def test_autocommit_commits_its_session(users_db: Database) -> None:
    async with users_db.autocommit():
        users_db.session.add(User(name="ada"))

    async with users_db.connect() as conn:
        assert await names(conn) == ["ada"]


@pytest.mark.anyio
async def test_a_transaction_that_cannot_connect_leaves_nothing_bound() -> None:
    db = Database("sqlite+aiosqlite:////nonexistent/path/db.sqlite3")

    with pytest.raises(sa.exc.OperationalError):
        async with db.transaction():
            pass  # pragma: no cover

    with pytest.raises(MissingConnectionError):
        _ = db.connection


@pytest.mark.anyio
async def test_a_decorated_function_runs_concurrently(tmp_path: Path) -> None:
    # One `Transaction` object serves every call of a decorated function, so
    # two calls overlapping must not unwind each other's blocks.
    db = Database(f"sqlite+aiosqlite:///{tmp_path / 'test.db'}")

    @db.transaction
    async def work(delay: float) -> AsyncConnection:
        connection = db.connection
        await anyio.sleep(delay)
        assert db.connection is connection
        return connection

    async with anyio.create_task_group() as task_group:
        # The first to enter is the first to leave, while the second is inside.
        task_group.start_soon(work, 0.01)
        task_group.start_soon(work, 0.05)

    await db.dispose()


@pytest.mark.anyio
async def test_a_nested_rollback_ends_the_transaction(users_db: Database) -> None:
    with pytest.raises(TransactionRolledBackError):
        async with users_db.transaction():
            async with users_db.session_factory() as inner:
                inner.add(User(name="ada"))
                await inner.flush()
                await inner.rollback()


@pytest.mark.anyio
async def test_a_nested_session_keeps_its_writes(users_db: Database) -> None:
    async with users_db.transaction():
        async with users_db.session_factory() as inner:
            inner.add(User(name="ada"))
            await inner.flush()  # written, never committed

        assert await names(users_db.connection) == ["ada"]


@pytest.mark.anyio
async def test_a_rolled_back_transaction_does_not_mask_the_error(
    users_db: Database,
) -> None:
    with pytest.raises(ZeroDivisionError):
        async with users_db.transaction():
            async with users_db.session_factory() as inner:
                inner.add(User(name="ada"))
                await inner.flush()
                await inner.rollback()
            1 / 0


@pytest.mark.anyio
async def test_connect_inside_autocommit_stays_on_the_same_connection(
    db: Database,
) -> None:
    async with db.autocommit() as conn:
        async with db.connect() as inner:
            assert inner is conn
            assert db.connection is conn


# `session_factory()` connects on first use; the other blocks connect on entry


@pytest.mark.anyio
async def test_session_factory_creates_no_engine_when_unused() -> None:
    db = Database("sqlite+aiosqlite://")

    async with db.session_factory():
        pass

    assert db._engine is None


@pytest.mark.anyio
async def test_session_factory_checks_nothing_out_on_entry(
    db: Database, checkouts: list[object]
) -> None:
    async with db.session_factory():
        assert checkouts == []

    assert checkouts == []


@pytest.mark.anyio
async def test_a_session_add_alone_checks_nothing_out(
    users_db: Database, checkouts: list[object]
) -> None:
    async with users_db.session_factory() as session:
        session.add(User(name="ada"))
        assert checkouts == []
        await session.flush()
        assert len(checkouts) == 1


@pytest.mark.anyio
async def test_the_first_query_checks_out_one_connection(
    users_db: Database, checkouts: list[object]
) -> None:
    async with users_db.session_factory() as session:
        count = sa.select(sa.func.count()).select_from(User)
        assert await session.scalar(count) == 0
        assert len(checkouts) == 1
        assert await session.scalar(count) == 0
        assert len(checkouts) == 1


@pytest.mark.anyio
async def test_a_lazy_session_commits_what_it_wrote(users_db: Database) -> None:
    async with users_db.session_factory() as session:
        session.add(User(name="ada"))
        await session.commit()

    async with users_db.connect():
        assert await users_db.session.scalar(sa.select(User.name)) == "ada"


@pytest.mark.anyio
async def test_the_connection_raises_until_the_session_uses_one(
    users_db: Database,
) -> None:
    async with users_db.session_factory() as session:
        with pytest.raises(MissingConnectionError, match="session_factory"):
            _ = users_db.connection

        # An empty flush is a no-op, so give the session something to write.
        session.add(User(name="ada"))
        await session.flush()

        assert users_db.connection is not None


@pytest.mark.anyio
async def test_a_nested_connect_shares_the_lazy_connection(
    users_db: Database, checkouts: list[object]
) -> None:
    async with users_db.session_factory() as session:
        async with users_db.connect() as conn:
            # The nested block checks out the connection the session reuses.
            assert len(checkouts) == 1
            assert await conn.scalar(sa.text("select 1")) == 1
        await session.flush()
        assert session.get_bind() is users_db.connection.sync_connection
        assert len(checkouts) == 1


@pytest.mark.anyio
async def test_sql_runs_inside_a_lazy_block(
    db: Database, checkouts: list[object]
) -> None:
    async with db.session_factory():
        statement = sa.text("select 41 + 1")

        assert await db.sql.from_statement(statement).scalars().one() == 42
        assert len(checkouts) == 1


@pytest.mark.anyio
async def test_session_factory_inside_another_block_reuses_the_connection(
    db: Database, checkouts: list[object]
) -> None:
    async with db.transaction() as conn:
        async with db.session_factory() as session:
            assert session.get_bind() is conn.sync_connection
        assert len(checkouts) == 1


@pytest.mark.anyio
async def test_the_other_blocks_connect_on_entry(
    db: Database, checkouts: list[object]
) -> None:
    async with db.connect():
        assert len(checkouts) == 1
    async with db.transaction():
        assert len(checkouts) == 2
    async with db.autocommit():
        assert len(checkouts) == 3
